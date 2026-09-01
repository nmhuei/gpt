# Codex13 Fix B — parser masker + correction telemetry (2026-08-26)

Scope: findings #3 and #4 from `codex13-today-fixes-2026-08-26.md`. Both were
verified against code + reproduction BEFORE any change; both confirmed.

## Finding 3 — CONFIRMED (Medium): unmatched backtick in soft `<cmd>` body

Reproduction (pre-fix): `parse_tool_calls('<cmd>printf "`"</cmd>', protocol="soft")
raised `MalformedToolCall("Soft <cmd> tool call tags are incomplete.")`.
`_mask_markdown_code()` treated the lone backtick as an inline-code opener,
blanked everything to end-of-text including `</cmd>`, so open/close tag counts
mismatched. Balanced-backtick bodies (`echo \`pwd\``) kept working — only
unmatched ones broke. Regression of the codex12-#2 masked-scan merge for valid
soft emission.

Fix (`gpt/utils/toolcall.py`):

- New `_SOFT_TAG_REGION_RE` (:537) + `_soft_tag_regions()` (:540): paired
  `<cmd>…</cmd>`/`<json>…</json>` regions found on the RAW text.
- `_mask_markdown_code(text, shield_spans=None)` (:273): when the scan reaches
  a shielded region with NO active fence/inline state, the region is copied
  verbatim and skipped; body backticks can no longer open spans that swallow
  the closing tag.
- Soft branch of `parse_tool_calls` (:1249) now scans a shielded mask; markup
  protocols keep the unshielded call at :1233.

Immunity preserved: regions reached while already inside a fence or inline
span are still masked through, so fenced / inline-quoted convention echoes
stay blocked (codex12 #2 contract). Unclosed fences stay fail-safe; incomplete
tags stay fail-closed (MalformedToolCall).

New tests in `tests/test_stealth_protocol.py`:
`test_unmatched_backtick_in_cmd_body_still_parses`,
`test_unmatched_backtick_body_does_not_unshield_fenced_echo`.

Note: fenced echo text remains in visible prose for mixed legit+echo replies;
that is pre-existing behavior (excision covers matched spans only), not touched.

## Finding 4 — CONFIRMED (Low): correction_count overstated on anti-repeat abort

`runtime.execute_raw_on_session` incremented `correction_count`
(`protocol_correction_count` too) before building the SHA-256 digest; the
"Correction loop not converging" abort raised without ever calling
`session.send`, so terminal telemetry (`persistent_correction_repeat`,
`submit_failed_before_commit_unknown`) reported one correction that was never
sent — contradicting the documented "real correction spend" contract.

Fix (`gpt/gateway/runtime.py` :2184): decrement both counters inside the abort
branch before the trace emit. Intermediate attempt events are unchanged; every
terminal path now reports actual sends.

Test updated in `tests/test_correction_tighten.py`
(`test_identical_correction_prompt_gets_escalation_hint_then_raises`):
exact assertions — 2 sends, `correction_count == 2` on both terminal events.

## Verification

- Targeted scenario script (A–J): unmatched backtick parses; balanced parses;
  fenced echo, inline-quote echo (cmd+json), unclosed fence blocked;
  placeholder excision intact; stray fence/backtick inside body parses;
  incomplete tags raise.
- Clusters: stealth + agent_loop + tighten + transpiler + all runtime/parser-
  adjacent suites = 380 passed; api_server/session/conversations/fault_injection
  = 67 passed. Total 447 green, 0 failed.
- ruff/mypy: no new findings from these edits (remaining reports pre-exist in
  regions owned by other workstreams).
- Constraints honored: only toolcall.py / runtime.py counter+masker regions /
  the two test files touched; no commit, no service restart.
