# Golden Expand 2026-08-26

QA lock-in of four new stream/runtime behaviors into `evals/goldens/`. Scope:
`evals/**` + this report only. No changes to `gpt/**`, `tests/**`; nothing
committed or restarted.

## Result

```
EVALS RESULT: total=24 pass=24 xfail=0 skip=0 fail=0
```

Baseline before expand: total=19 pass=18 fail=1 (see "Golden 14 revision").

## Added goldens (5)

| File | Locks |
|---|---|
| `20_content_freshness_noop_false_completion.json` | Content-based freshness at runtime level: transcript whose only committed tool activity is a no-op `<cmd>true</cmd>` invoke stays fresh (`_fresh_tool_conversation` / `_single_noop_invoke`), so an action-claim prose reply drives FALSE_COMPLETION through the real `execute_raw_on_session` loop (correction → escalation → `persistent_correction_repeat`). |
| `21_content_freshness_real_work_accepted.json` | Converse half: a real Write call/result in the transcript makes the same action-claim prose a legitimate summary — accepted on first generation, zero corrections, `submit_completed(correction_count=0)`. |
| `22_identical_multi_tool_escalation_once.json` | Identical over-cap MULTI_TOOL reply ×3: controller escalation hint appended exactly once (first duplicate), then fail-fast `correction_budget_exhausted protocol_shaped 2/2` even with `WEBGPT_MAX_CORRECTIONS=5`. Telemetry pins `correction_count=2` = corrections actually sent (aborting round excluded, Codex13 #4). MULTI_TOOL deliberately dodges the LIVE-R3 identical-hard-reason guard. |
| `23_late_fail_precontent_error_event.json` | Late failure before any content: fake session raises mid-send after recording its prompt → generic except path emits `submit_failed_before_commit_unknown{error_type, correction_count=0, turn_id=null}` and re-raises verbatim. Runtime-level simulation via new harness `input.raise_on_send`. |
| `24_placeholder_only_excision.json` | FIX-A placeholder excision (extends golden 17): quoted `<cmd>"..."</cmd>` in a placeholder-only reply emits no tool call, keeps all surrounding words, and leaves no trace of `<cmd>`/`</cmd>`/`"..."` in the returned prose — via new positive hooks `placeholder_only_prose_contains/_not_contains`. |

## Harness extensions (`evals/run_evals.py`, backward compatible)

1. `_FakeWebTurnSession(raise_on_send={on_send,type,message})` — Nth send records
   its prompt then raises a whitelisted builtin exception (late-fail driver).
2. `h_correction_budget` — `expect.raises_error_type` / `raises_error_contains`
   assert non-MalformedToolCall exceptions instead of flagging them unexpected.
3. `h_fixr8b op=placeholder_cmd` — positive only-prose assertions
   (`placeholder_only_prose_contains`, `placeholder_only_prose_not_contains`),
   closing the gap golden 17 documented ("no positive-content hook").

## Golden 14 revision (required by actual behavior)

Baseline run failed on `correction-anti-repeat-escalation`: runtime emits
`persistent_correction_repeat.correction_count=2`, golden expected 3.
Verified against `gpt/gateway/runtime.py` (Codex13 #4): the aborting third
round undoes its pre-check increment BEFORE emitting, per the task's stated
semantic — telemetry counts corrections actually sent, not attempts. Updated
expectation to 2 with rationale appended to the golden's desc.

## Behavior 3 note

Late-fail pre-content error IS reachable at runtime level (generic except path
in `execute_raw_on_session`), so it was locked rather than skipped; server-tier
plumbing was out of scope.

## Incident observed during the run

At session start every `parse_tool_calls` call hung: working-tree
`gpt/utils/toolcall.py::_mask_markdown_code` had a broken double-loop merge
(second `while index < len(text)` header left sequential after the shield-skip
loop → spin on any input). The owner fixed it mid-session (single merged loop);
all numbers above are from the post-fix tree. No repo file outside `evals/**`
was modified by this task.
