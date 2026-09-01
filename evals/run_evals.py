#!/usr/bin/env python3
"""Offline golden evals for WebGPT gateway behavior.

Runs mock-offline golden cases through the repo's REAL render/parse functions
(no business logic is mocked):

- promptcompat -> gpt.promptcompat.render_messages / enforce_prompt_budget
                  (+ gpt.gateway.runtime._with_soft_handshake for the soft
                  handshake composition, exactly as the runtime does it)
- toolcall     -> gpt.toolcall.ToolTranspiler.parse_tool_calls
- adapters     -> gpt.api.protocol_adapters.parse_anthropic_request
- gateway      -> gpt.gateway.runtime._tool_correction_issue (multi-tool cap)
- correction_budget -> gpt.gateway.runtime CORRECTION-TIGHTEN behavior driven
                 through the REAL CompletionRuntime.execute_raw_on_session
                 loop (fake web session replaying canned replies, fake store,
                 in-memory RuntimeTraceBus): protocol-shaped sub-budget cap,
                 anti-repeat escalation, failure-event metadata; plus direct
                 _tool_correction_issue/_max_tool_calls_per_turn classification
- codex_sse    -> gpt.transport.curl_transport codex/responses branch:
                 _build_headers / _build_codex_payload / _stream_codex_sse
                 (+ _codex_sse_enabled), driven by a fake session/response
                 object only — never any real network traffic
- fixr8b       -> FALSE_COMPLETION livelock regression lock (debug-r9 report
                 2026-08-25): soft <cmd> placeholder bodies emit no tool call,
                 FALSE_COMPLETION is guarded by _fresh_tool_conversation, and
                 correction prompts carry the verbatim original task via
                 _original_task_context over the FULL transcript

Usage:
    .venv/bin/python evals/run_evals.py --filter all
    .venv/bin/python evals/run_evals.py --filter soft-cmd

Exit code is non-zero iff at least one case FAILS.  XFAIL (expected failure,
e.g. a case gated on an unmerged fix) and SKIP do not fail the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gpt.state import MalformedToolCall, RateLimited  # noqa: E402
from gpt.toolcall import ToolTranspiler  # noqa: E402
from gpt.utils.promptcompat import enforce_prompt_budget, render_messages  # noqa: E402

GOLDENS_DIR = Path(__file__).resolve().parent / "goldens"

# Environment knobs that change gateway behavior; cleared before every case so
# each golden runs from a deterministic baseline (case input.env re-applies).
VOLATILE_ENV = (
    "WEBGPT_TOOL_PROTOCOL",
    "WEBGPT_PROMPT_BUDGET_CHARS",
    "WEBGPT_IMAGE_PLACEHOLDER",
    "WEBGPT_MAX_TOOL_CALLS_PER_TURN",
    "WEBGPT_CODEX_SSE",
    # correction_budget cases: operator env must not leak into the budget or
    # dump prompts to disk while driving execute_raw_on_session offline.
    "WEBGPT_MAX_CORRECTIONS",
    "WEBGPT_MAX_PROMPT_CHARS",
    "WEBGPT_PROMPT_DEBUG_DIR",
)


def _check_patterns(where: str, expect: dict[str, Any], failures: list[str]) -> None:
    for needle in expect.get("contains", []):
        if needle not in where:
            failures.append(f"missing expected substring: {needle!r}")
    for needle in expect.get("not_contains", []):
        if needle in where:
            failures.append(f"forbidden substring present: {needle!r}")
    for pattern in expect.get("regex", []):
        if not re.search(pattern, where):
            failures.append(f"pattern not found: {pattern!r}")


def _resolve_block(block: dict[str, Any]) -> dict[str, Any]:
    """Expand fixture shorthand (base64 payload generators) into real values."""
    source = block.get("source")
    if isinstance(source, dict) and isinstance(source.get("data_repeat"), dict):
        repeat = source["data_repeat"]
        resolved = dict(source)
        resolved["data"] = str(repeat.get("char", "A")) * int(repeat.get("count", 1))
        resolved.pop("data_repeat", None)
        return {**block, "source": resolved}
    return block


# ---------------------------------------------------------------------------
# promptcompat: full render path (+ optional budget enforcement + handshake)
# ---------------------------------------------------------------------------

def h_promptcompat(case: dict[str, Any]) -> list[str]:
    inp = case["input"]
    messages = []
    for message in inp["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            content = [_resolve_block(b) for b in content]
        messages.append({**message, "content": content})

    # Deterministic history-filler generator for budget-trim cases: N
    # (user, assistant) pairs inserted before the final message.
    if inp.get("filler_pairs"):
        fillers = []
        for i in range(1, int(inp["filler_pairs"]) + 1):
            fillers.append(
                {
                    "role": "user",
                    "content": f"FILLER_U{i} " + "u" * int(inp.get("filler_user_chars", 480)),
                }
            )
            fillers.append(
                {
                    "role": "assistant",
                    "content": f"FILLER_A{i} " + "a" * int(inp.get("filler_assistant_chars", 480)),
                }
            )
        messages = messages[:-1] + fillers + messages[-1:]

    rendered = render_messages(
        messages,
        initial=bool(inp.get("initial")),
        tools=inp.get("tools") or [],
        tool_choice=inp.get("tool_choice", "auto"),
        tool_protocol=inp.get("tool_protocol"),
    )
    failures: list[str] = []
    target = rendered
    if "budget_chars" in inp:
        budget = int(inp["budget_chars"])
        trimmed = enforce_prompt_budget(rendered, budget_chars=budget)
        if len(trimmed) > budget:
            failures.append(f"trimmed prompt is {len(trimmed)} chars > budget {budget}")
        if case["expect"].get("idempotent"):
            twice = enforce_prompt_budget(trimmed, budget_chars=budget)
            if twice != trimmed:
                failures.append("budget trim is not idempotent")
        target = trimmed

    _check_patterns(target, case["expect"], failures)

    sentinel = case["expect"].get("handshake_sentinel")
    if sentinel is not None:
        from gpt.gateway.runtime import _with_soft_handshake

        before = rendered.count(sentinel)
        after = _with_soft_handshake(rendered).count(sentinel)
        want_before = case["expect"]["handshake_count_before_append"]
        want_after = case["expect"]["handshake_count_after_append"]
        if before != want_before:
            failures.append(f"handshake sentinel seen {before}x before append, want {want_before}")
        if after != want_after:
            failures.append(f"handshake sentinel seen {after}x after append, want {want_after}")
    return failures


# ---------------------------------------------------------------------------
# toolcall: real parser under the configured protocol
# ---------------------------------------------------------------------------

def h_toolcall(case: dict[str, Any]) -> list[str]:
    inp = case["input"]
    allowed = set(inp["allowed_tools"]) if inp.get("allowed_tools") else None
    prose, calls = ToolTranspiler.parse_tool_calls(
        inp["text"],
        allowed_tools=allowed,
        tool_definitions=inp.get("tools") or [],
        protocol=inp.get("protocol"),
    )
    failures: list[str] = []
    expected_calls = case["expect"].get("calls")
    if expected_calls is not None:
        if len(calls) != len(expected_calls):
            failures.append(f"expected {len(expected_calls)} tool calls, got {len(calls)}: {calls!r}")
        for i, want in enumerate(expected_calls):
            if i >= len(calls):
                break
            got = calls[i]
            got_args = json.loads(got["function"]["arguments"])
            got_name = got["function"]["name"]
            if got_name != want["name"]:
                failures.append(f"call#{i}: name {got_name!r}, want {want['name']!r}")
            for key, value in want.get("arguments", {}).items():
                if got_args.get(key) != value:
                    failures.append(f"call#{i}: arg {key!r}={got_args.get(key)!r}, want {value!r}")
    if case["expect"].get("prose_is_null") and prose is not None:
        failures.append(f"expected prose=None, got {prose!r}")
    if case["expect"].get("prose_present") and not prose:
        failures.append(f"expected non-empty prose alongside calls, got {prose!r}")
    return failures


# ---------------------------------------------------------------------------
# adapters: Anthropic request adaptation + downstream render
# ---------------------------------------------------------------------------

def h_adapters(case: dict[str, Any]) -> list[str]:
    from gpt.api.protocol_adapters import parse_anthropic_request

    inp = case["input"]
    adapted = parse_anthropic_request(inp["request"])
    request = adapted.request
    failures: list[str] = []

    names = [tool["function"]["name"] for tool in request.tools]
    want_names = case["expect"].get("openai_tool_names")
    if want_names is not None and names != want_names:
        failures.append(f"adapted tool names {names!r}, want {want_names!r}")

    tool_messages = [m for m in request.messages if m.get("role") == "tool"]
    for want in case["expect"].get("tool_results", []):
        matches = [m for m in tool_messages if m.get("tool_call_id") == want["tool_call_id"]]
        if not matches:
            failures.append(f"no adapted tool result for {want['tool_call_id']!r}")
            continue
        got_error = bool(matches[0].get("is_error"))
        if got_error != bool(want["is_error"]):
            failures.append(
                f"tool result {want['tool_call_id']!r}: is_error={got_error}, want {bool(want['is_error'])}"
            )

    render_cfg = inp.get("render") or {}
    rendered = render_messages(
        request.messages,
        initial=bool(render_cfg.get("initial")),
        tools=request.tools,
        tool_choice=request.tool_choice,
        tool_protocol=render_cfg.get("tool_protocol"),
    )
    _check_patterns(rendered, case["expect"], failures)
    return failures


# ---------------------------------------------------------------------------
# gateway: multi-tool per-turn cap classification
# ---------------------------------------------------------------------------

def h_gateway(case: dict[str, Any]) -> list[str]:
    from gpt.gateway.runtime import _tool_correction_issue

    inp = case["input"]
    failures: list[str] = []

    def classify(text: str) -> tuple[str, str] | None:
        return _tool_correction_issue(
            text,
            tail=inp.get("tail") or [],
            messages=inp["messages"],
            tools=inp["tools"],
            tool_choice=inp.get("tool_choice", "auto"),
        )

    issue_under = classify(inp["under_cap_text"])
    want_under = case["expect"]["under_cap_issue"]
    got_under = None if issue_under is None else issue_under[0]
    if got_under != want_under:
        failures.append(f"under-cap classification {got_under!r} ({issue_under!r}), want {want_under!r}")

    issue_over = classify(inp["over_cap_text"])
    got_reason = issue_over[0] if issue_over else None
    want_reason = case["expect"]["over_cap_reason"]
    if got_reason != want_reason:
        failures.append(f"over-cap classification {got_reason!r} ({issue_over!r}), want {want_reason!r}")
    return failures


# ---------------------------------------------------------------------------
# codex_sse: authenticated /backend-api/codex/responses branch (curl_transport)
# ---------------------------------------------------------------------------

def _fake_bundle(spec: dict[str, Any]) -> Any:
    """TokenBundle stand-in carrying only the attributes _build_headers reads."""
    return types.SimpleNamespace(
        access_token=spec.get("access_token"),
        cf_clearance=spec.get("cf_clearance"),
        cookies=dict(spec.get("cookies") or {}),
        oai_device_id=spec.get("oai_device_id"),
        is_local_mock=False,
    )


def _fake_sentinel(spec: dict[str, Any]) -> Any:
    return types.SimpleNamespace(
        requirements_token=spec.get("requirements_token"),
        proof_token=spec.get("proof_token"),
        turnstile_token=spec.get("turnstile_token"),
    )


def _fake_codex_request(inp: dict[str, Any]) -> Any:
    from gpt.utils.types import ModelInfo, SendRequest

    model_id = inp.get("model_id")
    label = inp.get("model_label") or model_id or ""
    model = ModelInfo(id=model_id, label=label) if (model_id or label) else None
    return SendRequest(
        text=inp.get("text", ""),
        conversation_id=inp.get("conversation_id"),
        model=model,
    )


class _FakeSSEResponse:
    """curl_cffi response stand-in: replay canned chunks via aiter_bytes."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self) -> Any:
        for chunk in self._chunks:
            yield chunk.encode("utf-8")


def h_codex_sse(case: dict[str, Any]) -> list[str]:
    """Drive the real curl_transport codex branch with fake objects only."""
    from gpt.transport.curl_transport import CurlCffiTransport

    inp = case["input"]
    expect = case["expect"]
    failures: list[str] = []
    transport = CurlCffiTransport(token_manager=cast(Any, object()), session=object())
    op = inp["op"]

    if op == "headers":
        want_flag = expect.get("flag_enabled")
        if want_flag is not None:
            enabled = CurlCffiTransport._codex_sse_enabled()
            if enabled != bool(want_flag):
                failures.append(
                    f"_codex_sse_enabled()={enabled!r}, want {bool(want_flag)!r}"
                )
        bundle = _fake_bundle(inp["bundle"])
        sentinel = _fake_sentinel(inp.get("sentinel") or {})
        headers = CurlCffiTransport._build_headers(
            bundle, sentinel, codex=bool(inp["codex"])
        )
        for name, value in (expect.get("headers_eq") or {}).items():
            got = headers.get(name)
            if got != value:
                failures.append(f"header {name!r}={got!r}, want {value!r}")
        for name in expect.get("headers_absent", []):
            if name in headers:
                failures.append(
                    f"forbidden header {name!r} present with value {headers[name]!r}"
                )
        blob = json.dumps(headers, sort_keys=True)
        for needle in expect.get("headers_contains", []):
            if needle not in blob:
                failures.append(f"header envelope missing substring: {needle!r}")

    elif op == "payload":
        payload = transport._build_codex_payload(_fake_codex_request(inp))
        for key, value in (expect.get("fields_eq") or {}).items():
            got = payload.get(key)
            if got != value:
                failures.append(f"payload[{key!r}]={got!r}, want {value!r}")
        for key in ("store", "stream"):
            if key in expect and payload.get(key) is not expect[key]:
                # strict identity check so truthy values like 1 cannot sneak by
                failures.append(
                    f"payload[{key!r}]={payload.get(key)!r}, want strict {expect[key]!r}"
                )
        if expect.get("no_conversation_id") and "conversation_id" in payload:
            failures.append(
                f"payload leaked conversation_id={payload['conversation_id']!r}"
            )
        instructions = expect.get("instructions_lines")
        if instructions is not None:
            want = "\n\n".join(instructions)
            if payload["instructions"] != want:
                failures.append(
                    f"instructions {payload['instructions']!r}, want {want!r}"
                )
        items = expect.get("items")
        if items is not None:
            got_items = payload["input"]
            if len(got_items) != len(items):
                failures.append(
                    f"{len(got_items)} input items, want {len(items)}: {got_items!r}"
                )
            for want_item in items:
                index = int(want_item["index"])
                if index >= len(got_items):
                    break
                got_item = got_items[index]
                if got_item.get("role") != want_item["role"]:
                    failures.append(
                        f"item#{index} role {got_item.get('role')!r}, "
                        f"want {want_item['role']!r}"
                    )
                content = got_item.get("content") or [{}]
                got_type = content[0].get("type")
                got_text = content[0].get("text")
                if got_type != want_item["content_type"]:
                    failures.append(
                        f"item#{index} content type {got_type!r}, "
                        f"want {want_item['content_type']!r}"
                    )
                if got_text != want_item["text"]:
                    failures.append(
                        f"item#{index} text {got_text!r}, want {want_item['text']!r}"
                    )

    elif op == "stream":
        seen: list[tuple[str, str]] = []

        async def on_delta(delta: str, turn_id: str) -> None:
            seen.append((delta, turn_id))

        result = asyncio.run(
            transport._stream_codex_sse(
                _FakeSSEResponse(inp["chunks"]),
                _fake_codex_request(inp),
                on_delta=on_delta,
            )
        )
        if result.text != expect["text"]:
            failures.append(f"text {result.text!r}, want {expect['text']!r}")
        if result.turn_id != expect["turn_id"]:
            failures.append(f"turn_id {result.turn_id!r}, want {expect['turn_id']!r}")
        if result.status != expect["status"]:
            failures.append(f"status {result.status!r}, want {expect['status']!r}")
        got_deltas = [delta for delta, _ in seen]
        if got_deltas != expect["deltas"]:
            failures.append(f"deltas {got_deltas!r}, want {expect['deltas']!r}")
        want_cb_turn = expect.get("callback_turn_ids_all")
        if want_cb_turn is not None and any(turn != want_cb_turn for _, turn in seen):
            failures.append(
                f"some on_delta callbacks carried turn ids other than {want_cb_turn!r}: {seen!r}"
            )

    else:
        failures.append(f"unknown codex_sse op {op!r}")
    return failures


# ---------------------------------------------------------------------------
# correction_budget: CORRECTION-TIGHTEN behavior on the real runtime loop
# ---------------------------------------------------------------------------

class _FakeCorrectionStore:
    """ConversationStore stand-in: pending bookkeeping is a no-op."""

    def mark_pending(self, *args: Any, **kwargs: Any) -> None:
        return None

    def clear_pending(self, *args: Any, **kwargs: Any) -> None:
        return None


class _FakeWebTurnSession:
    """ChatGPTWebSession stand-in replaying canned assistant replies.

    Implements only the surface ``execute_raw_on_session`` touches offline:
    the ``conversation_id`` attribute read by position_session and the async
    ``send()`` used for every generation (original + correction rounds).
    Turn ids are deterministic ("turn-01", "turn-02", ...) so golden cases can
    pin the failure-event metadata exactly.

    ``raise_on_send`` simulates a late failure: the Nth send records its
    prompt and then raises before returning any content, which drives the
    runtime's generic except path (submit_failed_before_commit_unknown)
    exactly like a web stream dying mid-turn.
    """

    conversation_id = None

    _RAISABLE = {  # noqa: RUF012
        "RuntimeError": RuntimeError,
        "TimeoutError": TimeoutError,
        "ConnectionError": ConnectionError,
        "OSError": OSError,
        # STOP-REASON-REFUSAL negative lock: RateLimited is the infra quota
        # class (429/529 upstream) -- it must propagate RAW through the
        # runtime loop, never be reclassified as a model refusal.
        "RateLimited": RateLimited,
    }

    def __init__(
        self,
        replies: list[str],
        raise_on_send: dict[str, Any] | None = None,
    ) -> None:
        self._replies = list(replies)
        self._raise_on_send = raise_on_send
        self.sent_prompts: list[str] = []

    async def send(self, prompt: str, timeout_seconds: float | None = None) -> Any:
        self.sent_prompts.append(prompt)
        if self._raise_on_send is not None and len(self.sent_prompts) == int(
            self._raise_on_send.get("on_send", 1)
        ):
            name = str(self._raise_on_send.get("type", "RuntimeError"))
            exc_class = self._RAISABLE.get(name)
            if exc_class is None:
                raise ValueError(f"unsupported raise type {name!r} in golden fixture")
            raise exc_class(str(self._raise_on_send.get("message", "injected failure")))
        index = min(len(self.sent_prompts) - 1, len(self._replies) - 1)
        return types.SimpleNamespace(
            text=self._replies[index],
            turn_id=f"turn-{len(self.sent_prompts):02d}",
            status="completed",
            # runtime.execute_raw_on_session reads result.conversation_id after
            # an ACCEPTED reply (post-loop bootstrap stamping); canned offline
            # turns never open a real web conversation.
            conversation_id=None,
        )


def _event_matches(event: Any, spec: dict[str, Any]) -> bool:
    """True when a trace event's kind + metadata cover every spec entry."""
    if event.kind != spec.get("kind"):
        return False
    metadata = event.metadata or {}
    return all(metadata.get(key) == value for key, value in spec.items() if key != "kind")


def h_correction_budget(case: dict[str, Any]) -> list[str]:
    """Drive CORRECTION-TIGHTEN through real runtime code paths.

    op="runtime": full CompletionRuntime.execute_raw_on_session with a scripted
    fake web session; expectations assert the raised MalformedToolCall text,
    how many generations were actually sent, trace-event metadata (kind +
    metadata subset match), and per-send prompt content.
    op="classify": direct _tool_correction_issue / _max_tool_calls_per_turn
    classification under the case's env (multi-tool batching cap).
    """
    inp = case["input"]
    expect = case["expect"]
    failures: list[str] = []

    if inp.get("op") == "classify":
        from gpt.gateway.runtime import _max_tool_calls_per_turn, _tool_correction_issue

        want_limit = expect.get("limit_resolved")
        if want_limit is not None and _max_tool_calls_per_turn() != want_limit:
            failures.append(
                f"_max_tool_calls_per_turn()={_max_tool_calls_per_turn()!r}, want {want_limit!r}"
            )

        accepted_under: list[dict[str, Any]] = []
        issue_under = _tool_correction_issue(
            inp["under_cap_text"],
            tail=inp.get("tail") or [],
            messages=inp["messages"],
            tools=inp["tools"],
            tool_choice=inp.get("tool_choice", "auto"),
            accepted_calls_out=accepted_under,
        )
        got_under = None if issue_under is None else issue_under[0]
        if got_under != expect.get("under_cap_issue"):
            failures.append(
                f"under-cap classification {got_under!r} ({issue_under!r}), "
                f"want {expect.get('under_cap_issue')!r}"
            )
        want_accepted_under = expect.get("under_cap_accepted_calls")
        if want_accepted_under is not None and len(accepted_under) != int(want_accepted_under):
            failures.append(
                f"under-cap accepted {len(accepted_under)} calls, want {want_accepted_under}"
            )

        accepted_over: list[dict[str, Any]] = []
        issue_over = _tool_correction_issue(
            inp["over_cap_text"],
            tail=inp.get("tail") or [],
            messages=inp["messages"],
            tools=inp["tools"],
            tool_choice=inp.get("tool_choice", "auto"),
            accepted_calls_out=accepted_over,
        )
        got_reason = issue_over[0] if issue_over else None
        if got_reason != expect.get("over_cap_reason"):
            failures.append(
                f"over-cap classification {got_reason!r} ({issue_over!r}), "
                f"want {expect.get('over_cap_reason')!r}"
            )
        detail_over = issue_over[1] if issue_over else ""
        for needle in expect.get("over_cap_detail_contains") or []:
            if needle not in detail_over:
                failures.append(f"over-cap detail missing {needle!r}: {detail_over!r}")
        want_accepted_over = expect.get("over_cap_accepted_calls")
        if want_accepted_over is not None and len(accepted_over) != int(want_accepted_over):
            failures.append(
                f"over-cap accepted_calls_out filled {len(accepted_over)} calls, "
                f"want {want_accepted_over} (must be pre-verdict)"
            )
        return failures

    # op == "runtime": drive the real correction loop end to end.
    from gpt.conversations import ConversationRecord
    from gpt.gateway.runtime import CompletionRuntime
    from gpt.utils.tracing import RuntimeTraceBus

    session = _FakeWebTurnSession(inp["replies"], raise_on_send=inp.get("raise_on_send"))
    bus = RuntimeTraceBus()
    runtime = CompletionRuntime(
        conversations=cast(Any, _FakeCorrectionStore()),
        lease_session=cast(Any, lambda: None),
        trace=bus,
    )
    raised_text = ""
    raised_error_type = ""
    raised_error_text = ""
    raised_exc: BaseException | None = None
    try:
        asyncio.run(
            runtime.execute_raw_on_session(
                cast(Any, session),
                ConversationRecord(),
                tail=inp["tail"],
                messages=inp["messages"],
                model=inp.get("model", "chatgpt-web"),
                ui_model=None,
                tools=inp["tools"],
                tool_choice=inp.get("tool_choice", "auto"),
            )
        )
    except MalformedToolCall as exc:
        raised_exc = exc
        raised_text = str(exc)
    except Exception as exc:
        raised_exc = exc
        raised_error_type = type(exc).__name__
        raised_error_text = str(exc)

    # A case may expect a non-MalformedToolCall exception (e.g. a late web-side
    # failure injected via input.raise_on_send); when raises_error_type is set
    # the generic except path is asserted instead of being reported as a bug.
    want_error_type = expect.get("raises_error_type")
    if want_error_type is not None:
        if raised_error_type != want_error_type:
            failures.append(
                f"exception type {raised_error_type or '(none)'!r}, want {want_error_type!r}"
            )
    elif raised_error_text:
        failures.append(f"unexpected exception from runtime loop: {raised_error_type}: {raised_error_text}")
    # STOP-REASON-REFUSAL: exact class matters because ModelRefusalError
    # SUBCLASSES MalformedToolCall -- an isinstance check cannot tell them
    # apart, and only the subclass may take the stop_reason:"refusal" path at
    # the Anthropic boundary while the plain class keeps 502.  Exact-type pin
    # covers both directions (refusal cases AND the malformed negative).
    want_class = expect.get("raises_exception_class")
    if want_class is not None:
        got_class = type(raised_exc).__name__ if raised_exc is not None else "(none)"
        if got_class != want_class:
            failures.append(f"exception class {got_class!r}, want {want_class!r}")
    for needle in expect.get("raises_error_contains") or []:
        if needle not in raised_error_text:
            failures.append(f"error message missing {needle!r}: {raised_error_text!r}")
    raises_expected = bool(expect.get("raises_contains"))
    if raises_expected and not raised_text:
        failures.append(
            f"expected MalformedToolCall containing {expect['raises_contains']!r}, none raised"
        )
    for needle in expect.get("raises_contains") or []:
        if needle not in raised_text:
            failures.append(f"raise message missing {needle!r}: {raised_text!r}")

    want_sends = expect.get("send_count")
    if want_sends is not None and len(session.sent_prompts) != int(want_sends):
        failures.append(
            f"{len(session.sent_prompts)} generations sent, want {want_sends}"
        )

    events = bus.snapshot()
    for spec in expect.get("events") or []:
        if any(_event_matches(event, spec) for event in events):
            continue
        seen = [
            {"kind": event.kind, "metadata": event.metadata}
            for event in events
            if event.kind == spec.get("kind")
        ]
        failures.append(f"no trace event matching {spec!r}; same-kind events seen: {seen}")

    for check in expect.get("sent_prompt_checks") or []:
        index = int(check["index"])
        if index >= len(session.sent_prompts):
            failures.append(
                f"sent prompt #{index} missing ({len(session.sent_prompts)} sends total)"
            )
            continue
        blob = session.sent_prompts[index]
        for needle in check.get("contains") or []:
            if needle not in blob:
                failures.append(f"sent prompt #{index} missing substring {needle!r}")
        for needle in check.get("not_contains") or []:
            if needle in blob:
                failures.append(f"sent prompt #{index} must not contain {needle!r}")
        prior = check.get("starts_with_prior_rstrip")
        if prior is not None:
            base = session.sent_prompts[int(prior)].rstrip()
            if not blob.startswith(base + "\n\n"):
                failures.append(
                    f"sent prompt #{index} is not base #{prior}.rstrip() + escalation suffix"
                )
    return failures


# ---------------------------------------------------------------------------
# fixr8b: anti false-completion-livelock regression lock (debug-r9, 2026-08-25)
# ---------------------------------------------------------------------------

def h_fixr8b(case: dict[str, Any]) -> list[str]:
    """Lock FIX-R8B behavior through the repo's real functions only.

    op="placeholder_cmd": real ToolTranspiler.parse_tool_calls(protocol="soft")
        plus the real gpt.utils.toolcall._is_placeholder_command -- a quoted or
        ellipsised <cmd> body must emit NO tool call; with a real call present
        the placeholder span is excised from the prose while surrounding prose
        survives; a placeholder-only reply keeps its prose verbatim.
    op="fresh_guard": real gpt.gateway.runtime._tool_correction_issue over four
        scenarios -- FALSE_COMPLETION fires on fresh tool conversations and is
        suppressed once a real assistant tool call / tool result exists.
    op="original_context": drives the REAL CompletionRuntime.execute_raw_on_session
        loop (same offline driver as correction_budget) with a bootstrapped tail
        that holds only the tool result; the emitted correction prompt must embed
        the verbatim original task extracted from the FULL transcript.
    """
    inp = case["input"]
    expect = case["expect"]
    failures: list[str] = []
    op = inp["op"]

    if op == "placeholder_cmd":
        from gpt.utils.toolcall import _is_placeholder_command

        for body in inp.get("placeholder_bodies") or []:
            if not _is_placeholder_command(body):
                failures.append(f"_is_placeholder_command({body!r})=False, want True")
        for body in inp.get("executable_bodies") or []:
            if _is_placeholder_command(body):
                failures.append(f"_is_placeholder_command({body!r})=True, want False")

        def _soft_parse(text: str) -> tuple[str | None, list[dict[str, Any]]]:
            return ToolTranspiler.parse_tool_calls(
                text,
                allowed_tools=set(inp["allowed_tools"]),
                tool_definitions=inp["tools"],
                protocol=inp.get("protocol"),
            )

        prose, calls = _soft_parse(inp["mixed_text"])
        expected_calls = expect.get("calls") or []
        if len(calls) != len(expected_calls):
            failures.append(
                f"mixed text produced {len(calls)} tool call(s), "
                f"want {len(expected_calls)}: {calls!r}"
            )
        for i, want in enumerate(expected_calls):
            if i >= len(calls):
                break
            got_args = json.loads(calls[i]["function"]["arguments"])
            got_name = calls[i]["function"]["name"]
            if got_name != want["name"]:
                failures.append(f"call#{i}: name {got_name!r}, want {want['name']!r}")
            for key, value in want.get("arguments", {}).items():
                if got_args.get(key) != value:
                    failures.append(f"call#{i}: arg {key!r}={got_args.get(key)!r}, want {value!r}")
        for needle in expect.get("prose_contains") or []:
            if needle not in (prose or ""):
                failures.append(f"trimmed prose missing {needle!r}: {prose!r}")
        for needle in expect.get("prose_not_contains") or []:
            if needle in (prose or ""):
                failures.append(f"trimmed prose still contains {needle!r}: {prose!r}")

        only_text = inp["placeholder_only_text"]
        only_prose, only_calls = _soft_parse(only_text)
        if only_calls:
            failures.append(
                f"placeholder-only reply emitted {len(only_calls)} tool call(s): {only_calls!r}"
            )
        if expect.get("placeholder_only_prose_is_original") and only_prose != only_text:
            failures.append(
                f"placeholder-only prose was rewritten: {only_prose!r}, "
                f"want original kept verbatim"
            )
        # Positive content hooks for the placeholder-only reply (FIX-A excision
        # lock): the surrounding prose must survive while every trace of the
        # quoted tag span disappears.
        for needle in expect.get("placeholder_only_prose_contains") or []:
            if needle not in (only_prose or ""):
                failures.append(
                    f"placeholder-only prose missing {needle!r}: {only_prose!r}"
                )
        for needle in expect.get("placeholder_only_prose_not_contains") or []:
            if needle in (only_prose or ""):
                failures.append(
                    f"placeholder-only prose still contains {needle!r}: {only_prose!r}"
                )
        return failures

    if op == "fresh_guard":
        from gpt.gateway.runtime import _tool_correction_issue

        for scenario in inp["scenarios"]:
            verdict = _tool_correction_issue(
                scenario["reply_text"],
                tail=scenario.get("tail") or [],
                messages=scenario["messages"],
                tools=inp["tools"],
                tool_choice=inp.get("tool_choice", "auto"),
            )
            label = scenario.get("label") or scenario["reply_text"][:40]
            got = verdict[0] if verdict else None
            want = scenario["want_reason"]
            if got != want:
                failures.append(
                    f"[{label}] classification {got!r} ({verdict!r}), want {want!r}"
                )
                continue
            detail = verdict[1] if verdict else ""
            for needle in scenario.get("detail_contains") or []:
                if needle not in detail:
                    failures.append(f"[{label}] detail missing {needle!r}: {detail!r}")
        return failures

    if op == "original_context":
        sub_case = {
            "input": {"op": "runtime", **inp["runtime_input"]},
            "expect": expect,
        }
        return h_correction_budget(sub_case)

    failures.append(f"unknown fixr8b op {op!r}")
    return failures


HANDLERS: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "promptcompat": h_promptcompat,
    "toolcall": h_toolcall,
    "adapters": h_adapters,
    "gateway": h_gateway,
    "correction_budget": h_correction_budget,
    "codex_sse": h_codex_sse,
    "fixr8b": h_fixr8b,
}


_FIXT2_CANARY_TEXT = "Quick check first.\n<cmd>pwd</cmd>"


def fixt2_merged() -> bool:
    """Dynamic probe: does the soft parse path already accept prose+<cmd>?

    FIX-T2 makes the soft protocol pass allow_prose=True.  Until merged, any
    prose around <cmd> raises MalformedToolCall; the probe detects which world
    we are in so golden case (d) can be reported XFAIL instead of FAIL.
    """
    try:
        ToolTranspiler.parse_tool_calls(
            _FIXT2_CANARY_TEXT,
            allowed_tools={"Bash"},
            tool_definitions=[
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "description": "run shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
            protocol="soft",
        )
        return True
    except MalformedToolCall:
        return False


def run_case(case: dict[str, Any]) -> tuple[str, str]:
    """Run one golden case; returns (status, note)."""
    env_backup = dict(os.environ)
    try:
        for key in VOLATILE_ENV:
            os.environ.pop(key, None)
        for key, value in (case.get("input") or {}).get("env", {}).items():
            os.environ[key] = str(value)

        handler = HANDLERS.get(case["module"])
        if handler is None:
            return "SKIP", f"unknown module {case['module']!r}"
        if case.get("requires_fix") == "FIX-T2" and not fixt2_merged():
            return (
                "XFAIL",
                "FIX-T2 not merged: soft parse still rejects prose+<cmd> "
                "(allow_prose=False at gpt/utils/toolcall.py soft branch)",
            )
        failures = handler(case)
        if failures:
            return "FAIL", "; ".join(failures)
        return "PASS", ""
    finally:
        os.environ.clear()
        os.environ.update(env_backup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline gateway golden evals.")
    parser.add_argument(
        "--filter",
        default="all",
        help="'all' or a substring of the case id to run (default: all)",
    )
    args = parser.parse_args()

    files = sorted(GOLDENS_DIR.glob("*.json"))
    if not files:
        print(f"No golden cases found under {GOLDENS_DIR}", file=sys.stderr)
        return 2

    counts = {"PASS": 0, "FAIL": 0, "XFAIL": 0, "SKIP": 0}
    exit_code = 0
    for path in files:
        with open(path, encoding="utf-8") as handle:
            case = json.load(handle)
        if args.filter != "all" and args.filter.casefold() not in case["id"].casefold():
            continue
        status, note = run_case(case)
        counts[status] += 1
        line = f"[{status}] {case['id']} — {case['desc']}"
        if note:
            line += f"\n    {note}"
        print(line)
        if status == "FAIL":
            exit_code = 1

    total = sum(counts.values())
    print(
        f"EVALS RESULT: total={total} pass={counts['PASS']} xfail={counts['XFAIL']} "
        f"skip={counts['SKIP']} fail={counts['FAIL']}"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
