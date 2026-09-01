#!/usr/bin/env .venv/bin/python
"""FCONV-NOTOKEN-REPLAY — staged live replay of the authed f/conversation chain.

Ladder (<= 4 requests, exact production order):
  1. sentinel chat-requirements/prepare   {"p": bootstrap-proof}      [HTTP]
  2. PoW solve                            (local SHA3-512, no request)
  3. f/conversation/prepare               15-field body + X-Conduit-Token: no-token
  4. POST f/conversation                  full envelope + conduit token

Steps 1-3 run through the REAL ``CurlCffiTransport._prepare_fconv_turn``
(code-reuse, zero reimplementation); an instrumented subclass prints a
summary of every request/response so the coordinator can judge each hop.
Step 4 reuses the transport's own envelope/payload builders.

Default is DRY-RUN: prints the plan and exits without any network or browser
activity.  Pass --live to actually fire.  The script never sets environment
flags itself — the coordinator must export WEBGPT_FCONV_PREPARE=1 first.

Verdict criteria (docs/automation/ROADMAP.md row FCONV-NOTOKEN-REPLAY):
  prepare 200 + conduit_token but conversation still 403
    => branch DEAD permanently; focus shifts to CODEX-SSE OAuth.
  prepare != 200 => recipe/endpoint problem (compare research expectation).
  conversation streams => branch ALIVE.

Sources: docs/reports/sse-resume-research-2026-08-26.md,
kymuco/chatgpt-web-adapter PR #40/#41, 5yu4n/gptweb2api.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import replace
from typing import Any

LADDER = (
    "sentinel/chat-requirements/prepare  {\"p\": <local bootstrap proof>}",
    "PoW solve (LOCAL, no HTTP)          SHA3-512 seed/difficulty",
    "f/conversation/prepare              15-field body + X-Conduit-Token: no-token",
    "POST f/conversation                 SSE stream with full envelope",
)

# Headers worth printing per request (values redacted where sensitive).
_REQUEST_HEADER_KEYS = (
    "Authorization",
    "Cookie",
    "OAI-Device-Id",
    "OAI-Session-Id",
    "X-OAI-Turn-Trace-Id",
    "X-Conduit-Token",
    "ChatGPT-Account-ID",
)
# Response headers surfaced when available (JSON stage only reads status).
_SUMMARY_CHARS = 200


def _trunc(text: str, limit: int = _SUMMARY_CHARS) -> str:
    text = text.replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + f"...(+{len(text) - limit})"


def _redact(name: str, value: str) -> str:
    if name == "Authorization":
        return value[:14] + "...<redacted>" if len(value) > 14 else "<redacted>"
    if name == "Cookie":
        return f"<{value.count('=')} cookies>"
    return value


def print_step_header(index: int, title: str) -> None:
    print(f"\n=== STEP {index}: {title} ===")


def make_instrumented_transport(base):
    """Subclass the REAL transport so every internal hop gets instrumented.

    Must be a true subclass: ``_prepare_fconv_turn`` resolves ``self._post_json``
    dynamically, so a duck-typed wrapper around an instance would never see
    the prepare-chain hops.
    """

    class InstrumentedFconvTransport(base):
        async def _post_json(self, url, headers, payload, *, timeout):
            print(f"  -> POST {url}")
            for key in _REQUEST_HEADER_KEYS:
                if key in headers:
                    print(f"     {key}: {_redact(key, headers[key])}")
            for key in sorted(headers):
                if key.startswith("OpenAI-Sentinel"):
                    print(f"     {key}: {_redact(key, headers[key])}")
            try:
                body_preview = json.dumps(payload, ensure_ascii=False)
            except (TypeError, ValueError):
                body_preview = repr(payload)
            print(f"     body({_SUMMARY_CHARS}): {_trunc(body_preview)}")
            status, envelope = await super()._post_json(
                url, headers, payload, timeout=timeout
            )
            try:
                resp_preview = json.dumps(envelope, ensure_ascii=False)
            except (TypeError, ValueError):
                resp_preview = repr(envelope)
            print(f"  <- status={status}")
            print(f"     body({_SUMMARY_CHARS}): {_trunc(resp_preview)}")
            return status, envelope

    return InstrumentedFconvTransport


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fconv_replay",
        description=(
            "Staged FCONV-NOTOKEN-REPLAY ladder "
            "(requirements -> PoW -> prepare -> conversation). "
            "DRY-RUN by default; --live fires real requests."
        ),
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="actually run the ladder (default: dry-run plan only)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        metavar="DIR",
        help="CloakBrowser profile dir (default: gateway default profile)",
    )
    parser.add_argument(
        "--cdp-url",
        default=None,
        metavar="URL",
        help="attach over CDP instead of launching a browser",
    )
    parser.add_argument(
        "--prompt",
        default="Reply with exactly: replay-ok",
        help="user prompt for the conversation POST",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="ID",
        help="model id for the prepare body / conversation payload",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        metavar="UUID",
        help="continue an existing conversation instead of starting fresh",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        metavar="SEC",
        help="per-request timeout in seconds (default: 45)",
    )
    return parser


def print_plan(args: argparse.Namespace) -> None:
    flag_on = _flag_enabled()
    print("[plan] FCONV-NOTOKEN-REPLAY ladder:")
    for index, step in enumerate(LADDER, start=1):
        print(f"  {index}. {step}")
    print(f"[plan] WEBGPT_FCONV_PREPARE={'ON' if flag_on else 'OFF'}"
          " (script NEVER sets it; coordinator must export it for --live)")
    print("[plan] verdict criteria (ROADMAP row FCONV-NOTOKEN-REPLAY):")
    print("  - prepare 200 + conduit but conversation 403 -> branch DEAD,"
          " pivot CODEX-SSE OAuth")
    print("  - prepare != 200 -> endpoint/recipe problem (research expects 200)")
    print("  - conversation streams -> branch ALIVE")
    print("[plan] dry-run complete; pass --live to fire (no env is set here).")


def _flag_enabled() -> bool:
    import os

    return os.environ.get("WEBGPT_FCONV_PREPARE", "").strip().casefold() in {
        "1", "true", "yes", "on"
    }


async def run_live(args: argparse.Namespace) -> int:
    from gpt.transport.browser import BrowserManager
    from gpt.transport.curl_transport import CurlCffiTransport
    from gpt.transport.token_manager import TokenManager
    from gpt.types import ModelInfo, SendRequest

    request = SendRequest(
        text=args.prompt,
        conversation_id=args.conversation_id,
        model=ModelInfo(id=args.model, label=args.model) if args.model else None,
    )

    # Setup mirrors HybridTransport.start: persistent headless profile ->
    # page -> chatgpt.com -> TokenManager.  No auto-login side effects.
    print_step_header(0, "setup browser + credentials")
    kwargs: dict[str, Any] = {"headless": True, "persistent": True}
    if args.profile:
        kwargs["profile_dir"] = args.profile
    if args.cdp_url:
        kwargs["cdp_url"] = args.cdp_url
    browser = BrowserManager(**kwargs)
    try:
        await browser.start()
        page = await browser.new_page()
        await page.goto(
            "https://chatgpt.com", wait_until="domcontentloaded", timeout=45_000
        )
        token_manager = TokenManager(page)
        bundle = await token_manager.refresh_if_needed()
        if bundle.is_local_mock:
            print("[FAIL] credential snapshot resolved to the local mock;"
                  " refusing to fire a meaningless ladder.", file=sys.stderr)
            return 2
        if not bundle.oai_device_id:
            # TokenBundle is a frozen dataclass -> rebuild via replace().
            # ChatGPT's oai-device-id is just a lowercase UUID4 string; the
            # frontend normally mints one into localStorage, but a profile
            # that never rendered the app (fresh login, no page JS) can lack
            # it — _build_headers then raises AuthRequired before step 1.
            generated = str(uuid.uuid4())
            bundle = replace(bundle, oai_device_id=generated)
            print(f"[WARN] device-id không có trong profile — dùng UUID mới"
                  f" ({generated}) (server có thể gán lại)")
        print(f"  ok: device={bundle.oai_device_id}"
              f" bearer={_redact('Authorization', 'Bearer ' + (bundle.access_token or ''))}")

        Instrumented = make_instrumented_transport(CurlCffiTransport)
        transport = Instrumented(token_manager)

        # Steps 1-3: the REAL prepare chain, instrumented per hop.
        print_step_header(1, "sentinel chat-requirements/prepare (+fallback)")
        print_step_header(2, "PoW solve (local, logged by step output above)")
        print_step_header(3, "f/conversation/prepare + X-Conduit-Token: no-token")
        try:
            sentinel, session_id, trace_id, conduit_token = (
                await transport._prepare_fconv_turn(bundle, request)
            )
        except Exception as exc:  # early stop: nothing further is meaningful
            print(f"[FAIL] prepare chain raised after its steps: {exc!r}")
            print("[VERDICT] INCOMPLETE — prepare chain failed; see step"
                  " output. Do not conclude branch death from a transport"
                  " error alone.")
            return 2
        if conduit_token:
            print(f"  conduit_token: present ({len(conduit_token)} chars)")
        else:
            print("  conduit_token: NONE (prepare step did not yield one)")
        if not conduit_token:
            print("[VERDICT] PREPARE-FAIL — no conduit token despite the"
                  " no-token marker; compare step 3 status against the"
                  " research expectation (200). Branch unproven, retry once"
                  " from another profile/IP before concluding.")
            return 1

        # Step 4: conversation POST via the transport's own builders.
        print_step_header(4, "POST f/conversation (SSE)")
        headers = transport._build_headers(
            bundle,
            sentinel,
            codex=False,
            fconv=True,
            turn_session_id=session_id,
            turn_trace_id=trace_id,
            conduit_token=conduit_token,
        )
        payload = await transport._maybe_build_multimodal_payload(
            bundle, request, enabled=True
        )
        response = await transport._post_conversation(headers, payload, request)
        status = getattr(response, "status_code", None)
        content_type = ""
        try:
            content_type = response.headers.get("content-type", "")
        except Exception:
            pass
        print(f"  <- status={status} content-type={content_type}")
        preview = b""
        if isinstance(status, int) and status < 400:
            try:
                # curl_cffi async Response exposes aiter_content/aiter_lines
                # (no aiter_bytes) — mirror CurlCffiTransport._response_chunks.
                aiter = getattr(response, "aiter_content", None)
                if aiter is None:
                    aiter = getattr(response, "aiter_lines", None)
                if aiter is None:
                    raise AttributeError(
                        "curl_cffi response exposes no streaming iterator"
                    )
                async for chunk in aiter():
                    preview += (
                        f"{chunk}\n\n".encode() if isinstance(chunk, str) else chunk
                    )
                    if len(preview) >= _SUMMARY_CHARS:
                        break
            finally:
                await transport._close_quietly(response)
            print(f"     sse({_SUMMARY_CHARS}): {_trunc(preview.decode('utf-8', 'replace'))}")
            print("[VERDICT] ALIVE — conversation streamed; branch survives"
                  " with the no-token marker handshake.")
            return 0
        await transport._close_quietly(response)
        print("[VERDICT] BRANCH-DEAD — prepare returned 200 + conduit_token"
              " but the conversation POST was refused; per ROADMAP criteria"
              " close the f/conversation branch permanently and focus"
              " CODEX-SSE OAuth. (If reputation-suspected, retry ONCE from a"
              " different profile/IP before finalizing.)")
        return 1
    finally:
        stop = getattr(browser, "stop", None)
        if callable(stop):
            await stop()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.live:
        print_plan(args)
        return 0
    if not _flag_enabled():
        # Guard BEFORE heavy imports so the refusal is instant and
        # side-effect-free; the script itself never sets environment flags.
        print("[FAIL] WEBGPT_FCONV_PREPARE is OFF; the ladder would not run."
              " Export WEBGPT_FCONV_PREPARE=1 yourself (this script never"
              " sets env) and re-run with --live.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run_live(args))
    except KeyboardInterrupt:
        print("\n[abort] interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
