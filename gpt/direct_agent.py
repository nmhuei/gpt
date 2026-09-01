"""Compatibility surface for the original direct-agent module.

The production implementation now lives in :mod:`gpt.agent`.  Existing imports
and the `gpt-direct` console script remain stable while new code shares the
same AgentRunner used by the unified `gpt` CLI and benchmark harness.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from gpt.agent.events import AgentEvent
from gpt.agent.events import AgentResult as DirectAgentResult
from gpt.agent.runner import (
    DEFAULT_SYSTEM_PROMPT,
    AgentRunner,
    AgentRunnerConfig,
)
from gpt.core.settings import DEFAULT_BASE_URL, DEFAULT_MODEL

DEFAULT_MAX_ROUNDS = 20
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MAX_TOOL_OUTPUT_CHARS = 12_000


@dataclass(frozen=True, slots=True)
class DirectAgentConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = "sk-webgpt-local"
    model: str = DEFAULT_MODEL
    max_tokens: int = 8192
    max_rounds: int = DEFAULT_MAX_ROUNDS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_tool_output_chars: int = DEFAULT_MAX_TOOL_OUTPUT_CHARS
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Keep historical behavior: DirectAgent was not a completion guardrail.
    verify: str = "off"

    def to_runner(self) -> AgentRunnerConfig:
        return AgentRunnerConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            max_tokens=self.max_tokens,
            max_rounds=self.max_rounds,
            timeout_seconds=self.timeout_seconds,
            max_tool_output_chars=self.max_tool_output_chars,
            system_prompt=self.system_prompt,
            verify=self.verify,
            persist_session=False,
        )


class DirectAgent:
    def __init__(
        self,
        *,
        workspace: Path,
        config: DirectAgentConfig | None = None,
        client: httpx.Client | None = None,
        event_callback: Callable[[AgentEvent], None] | None = None,
    ) -> None:
        self.config = config or DirectAgentConfig()
        self._runner = AgentRunner(
            workspace=workspace,
            config=self.config.to_runner(),
            http_client=client,
            event_callback=event_callback,
        )

    @property
    def workspace(self) -> Path:
        return self._runner.workspace

    @property
    def session_id(self) -> str | None:
        return self._runner.session_id

    def run(self, prompt: str) -> DirectAgentResult:
        return self._runner.run(prompt)

    def close(self) -> None:
        self._runner.close()

    def __enter__(self) -> DirectAgent:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _print_event(event: AgentEvent) -> None:
    if event.kind == "response":
        print(
            f"[round {event.round_index}] gateway "
            f"{event.data.get('status_code')} {event.data.get('elapsed_seconds')}s "
            f"session={event.data.get('session_id')}",
            file=sys.stderr,
            flush=True,
        )
    elif event.kind == "assistant":
        calls = int(event.data.get("tool_calls") or 0)
        if calls:
            print(
                f"[round {event.round_index}] assistant requested {calls} tool call(s)",
                file=sys.stderr,
                flush=True,
            )
        elif event.data.get("text"):
            print(
                f"[round {event.round_index}] assistant finished",
                file=sys.stderr,
                flush=True,
            )
    elif event.kind == "tool_start":
        if event.data.get("tool") == "Bash":
            value = event.data.get("input") or {}
            print(
                f"[round {event.round_index}] $ {value.get('command', '')}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"[round {event.round_index}] {event.data.get('tool')}",
                file=sys.stderr,
                flush=True,
            )
    elif event.kind == "tool_end":
        print(
            f"[round {event.round_index}] exit={event.data.get('exit_code')} "
            f"({event.data.get('duration_ms')}ms)",
            file=sys.stderr,
            flush=True,
        )
    elif event.kind == "verification_gate":
        print(
            f"[round {event.round_index}] verification required",
            file=sys.stderr,
            flush=True,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpt-direct",
        description="Compatibility CLI for the WebGPT direct local agent.",
    )
    parser.add_argument("prompt", nargs="?", help="Task prompt. If omitted, read from stdin.")
    parser.add_argument("--workspace", default=".")
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "ANTHROPIC_BASE_URL",
            os.environ.get("WEBGPT_GATEWAY_URL", DEFAULT_BASE_URL),
        ),
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get(
            "ANTHROPIC_API_KEY",
            os.environ.get("WEBGPT_API_KEY", "sk-webgpt-local"),
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("WEBGPT_DIRECT_MODEL", DEFAULT_MODEL),
    )
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--max-tokens", type=int, default=8192)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--verify",
        choices=["auto", "quick", "full", "off"],
        default="off",
    )
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    prompt = args.prompt if args.prompt is not None else sys.stdin.read()
    if not prompt.strip():
        print("gpt-direct: prompt is empty", file=sys.stderr)
        return 2
    if args.max_rounds < 1 or args.timeout <= 0:
        print("gpt-direct: invalid --max-rounds/--timeout", file=sys.stderr)
        return 2

    config = DirectAgentConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        max_tokens=args.max_tokens,
        max_rounds=args.max_rounds,
        timeout_seconds=args.timeout,
        verify=args.verify,
    )
    callback = None if args.quiet else _print_event
    with DirectAgent(
        workspace=Path(args.workspace),
        config=config,
        event_callback=callback,
    ) as agent:
        result = agent.run(prompt)

    if args.json_output:
        print(
            json.dumps(
                {
                    "success": result.success,
                    "text": result.text,
                    "rounds": result.rounds,
                    "tool_calls": result.tool_calls,
                    "session_id": result.session_id,
                    "stop_reason": result.stop_reason,
                    "elapsed_seconds": round(result.elapsed_seconds, 3),
                    "error": result.error,
                    "verification_gate_count": result.verification_gate_count,
                },
                ensure_ascii=False,
            )
        )
    else:
        if result.text:
            print(result.text)
        if result.error:
            print(f"gpt-direct: {result.error}", file=sys.stderr)
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
