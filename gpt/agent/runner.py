from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from gpt.core.settings import DEFAULT_BASE_URL, DEFAULT_MODEL
from gpt.tools.registry import ToolRegistry

from .client import GatewayClient
from .events import AgentEvent, AgentResult
from .session import SessionStore
from .verify import VerificationGuard

DEFAULT_SYSTEM_PROMPT = """You are an autonomous coding/tool agent operating in the user's workspace.
Inspect before editing. Use Bash for inspection, search, git, builds and tests.
Use ApplyPatch for ordinary file edits instead of fragile shell quoting.
Never claim a command ran unless the controller returned its real result.
Preserve public APIs and existing behavior unless the task explicitly changes them.
Work autonomously until the task is actually verified or a concrete blocker is proven.
"""


@dataclass(frozen=True, slots=True)
class AgentRunnerConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = "sk-webgpt-local"
    model: str = DEFAULT_MODEL
    max_tokens: int = 8192
    max_rounds: int = 20
    timeout_seconds: float = 180.0
    overall_timeout_seconds: float | None = None
    max_tool_output_chars: int = 12_000
    verify: str = "auto"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    persist_session: bool = False


class AgentRunner:
    """Production direct-agent loop shared by CLI, benches, and tests."""

    def __init__(
        self,
        *,
        workspace: Path,
        config: AgentRunnerConfig | None = None,
        http_client: httpx.Client | None = None,
        event_callback: Callable[[AgentEvent], None] | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        if not self.workspace.is_dir():
            raise ValueError(
                f"Workspace does not exist or is not a directory: {self.workspace}"
            )
        self.config = config or AgentRunnerConfig()
        self.event_callback = event_callback

        from gpt.tools.process import ProcessRunner

        self.tools = ToolRegistry(
            self.workspace,
            process_runner=ProcessRunner(
                default_timeout_seconds=self.config.timeout_seconds,
                max_output_chars=self.config.max_tool_output_chars,
            ),
        )
        self.verifier = VerificationGuard(mode=self.config.verify)
        self.session_store = session_store
        if self.config.persist_session and self.session_store is None:
            self.session_store = SessionStore.default()
        remembered = (
            self.session_store.get(self.workspace)
            if self.config.persist_session and self.session_store is not None
            else None
        )
        self.client = GatewayClient(
            base_url=self.config.base_url,
            api_key=self.config.api_key,
            timeout_seconds=self.config.timeout_seconds,
            client=http_client,
            event_callback=event_callback,
            session_id=session_id or remembered,
        )

    @property
    def session_id(self) -> str | None:
        return self.client.session_id

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> AgentRunner:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _emit(self, kind: str, round_index: int, **data: Any) -> None:
        if self.event_callback is not None:
            self.event_callback(
                AgentEvent(kind=kind, round_index=round_index, data=data)
            )

    @staticmethod
    def _text(blocks: Any) -> str:
        if isinstance(blocks, str):
            return blocks
        if not isinstance(blocks, list):
            return ""
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    @staticmethod
    def _tool_uses(blocks: Any) -> list[dict[str, Any]]:
        if not isinstance(blocks, list):
            return []
        return [
            block
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    def _remember_session(self) -> None:
        if (
            self.config.persist_session
            and self.session_store is not None
            and self.client.session_id
        ):
            self.session_store.remember(self.workspace, self.client.session_id)

    def run(self, prompt: str) -> AgentResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be non-empty.")

        started = time.monotonic()
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        tool_count = 0
        last_text = ""
        last_stop_reason: str | None = None

        for round_index in range(1, self.config.max_rounds + 1):
            if (
                self.config.overall_timeout_seconds is not None
                and time.monotonic() - started >= self.config.overall_timeout_seconds
            ):
                return AgentResult(
                    success=False,
                    text=last_text,
                    rounds=round_index - 1,
                    tool_calls=tool_count,
                    session_id=self.session_id,
                    stop_reason=last_stop_reason,
                    elapsed_seconds=time.monotonic() - started,
                    error=(
                        "Agent wall-clock timeout reached "
                        f"({self.config.overall_timeout_seconds:g}s)."
                    ),
                    verification_gate_count=self.verifier.gate_count,
                )
            try:
                payload = self.client.complete(
                    model=self.config.model,
                    max_tokens=self.config.max_tokens,
                    system_prompt=self.config.system_prompt,
                    messages=messages,
                    tools=self.tools.schemas,
                    round_index=round_index,
                )
                self._remember_session()
            except Exception as exc:
                return AgentResult(
                    success=False,
                    text=last_text,
                    rounds=round_index,
                    tool_calls=tool_count,
                    session_id=self.session_id,
                    stop_reason=last_stop_reason,
                    elapsed_seconds=time.monotonic() - started,
                    error=f"{type(exc).__name__}: {exc}",
                    verification_gate_count=self.verifier.gate_count,
                )

            blocks = payload.get("content") or []
            last_stop_reason = payload.get("stop_reason")
            last_text = self._text(blocks)
            calls = self._tool_uses(blocks)
            self._emit(
                "assistant",
                round_index,
                stop_reason=last_stop_reason,
                text=last_text,
                tool_calls=len(calls),
            )
            messages.append({"role": "assistant", "content": blocks})

            if not calls:
                if not self.verifier.final_allowed():
                    message = self.verifier.rejection_message()
                    self._emit(
                        "verification_gate",
                        round_index,
                        mode=self.verifier.mode,
                        gate_count=self.verifier.gate_count,
                    )
                    messages.append({"role": "user", "content": message})
                    continue
                return AgentResult(
                    success=True,
                    text=last_text,
                    rounds=round_index,
                    tool_calls=tool_count,
                    session_id=self.session_id,
                    stop_reason=last_stop_reason,
                    elapsed_seconds=time.monotonic() - started,
                    verification_gate_count=self.verifier.gate_count,
                )

            results: list[dict[str, Any]] = []
            for call in calls:
                tool_count += 1
                name = call.get("name")
                tool_input = call.get("input")
                self._emit(
                    "tool_start",
                    round_index,
                    tool=name,
                    input=tool_input,
                )
                result = self.tools.execute(str(name), tool_input)
                self.verifier.observe(str(name), tool_input, result)
                self._emit(
                    "tool_end",
                    round_index,
                    tool=name,
                    exit_code=result.exit_code,
                    status=result.status,
                    duration_ms=result.duration_ms,
                    timed_out=result.timed_out,
                    changed_files=list(result.changed_files),
                )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id"),
                        "content": result.to_model_text(),
                        "is_error": result.is_error,
                    }
                )
            messages.append({"role": "user", "content": results})

        return AgentResult(
            success=False,
            text=last_text,
            rounds=self.config.max_rounds,
            tool_calls=tool_count,
            session_id=self.session_id,
            stop_reason=last_stop_reason,
            elapsed_seconds=time.monotonic() - started,
            error=f"Maximum tool rounds reached ({self.config.max_rounds}).",
            verification_gate_count=self.verifier.gate_count,
        )


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AgentRunner",
    "AgentRunnerConfig",
]
