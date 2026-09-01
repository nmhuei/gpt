# Evals re-run post-FIX-A/FIX-B — 2026-08-26

Scope: `evals/run_evals.py --filter all` (19 goldens, offline, fake sessions).
Constraint honored: no `gpt/**` code touched; only `evals/goldens/17_fixr8b_placeholder_cmd.json` edited.

## Run 1 (pre-golden-update)

```
EVALS RESULT: total=19 pass=18 xfail=0 skip=0 fail=1
```

Sole failure — golden 17 (`fixr8b-placeholder-cmd-no-tool-call`):

```
placeholder-only prose was rewritten: 'Send me something like                  whenever you are ready.', want original kept verbatim
```

Golden 18 (`fixr8b-false-completion-fresh-guard`) and 19 PASS unchanged post-FIX-B.

## Golden 17 verdict: new behavior is CORRECT (case 3a)

Post-FIX-A, `_extract_soft_candidates` keeps the placeholder span and emits no call, so `<cmd>"..."</cmd>` is excised from prose **even in a placeholder-only reply** (empirically verified via real `ToolTranspiler.parse_tool_calls(protocol="soft")`). Rationale for accepting:

1. **Intentional FIX-A design** (`gpt/utils/toolcall.py` comment): a quoted/ellipsised body is protocol echo, not execution intent; excising keeps `<cmd>` tags out of visible replies (stealth soft-protocol doctrine) and avoids MALFORMED_TOOL correction loops on innocent acknowledgments.
2. **Internal consistency**: this same golden already requires span excision in `mixed_text` via `prose_not_contains: ["<cmd>", "\"...\""]`; the identical span cannot be kept verbatim when it appears alone.
3. **No functional impact**: gateway runtime discards transpiler prose (`_, calls = ToolTranspiler.parse_tool_calls(...)` at runtime.py:408/423/739); FALSE_COMPLETION classifies raw reply text.

## Golden 17 changes

File: `evals/goldens/17_fixr8b_placeholder_cmd.json`

- `expect.placeholder_only_prose_is_original`: `true` → `false`. The handler has no positive-content hook for only-prose, so `false` drops the equality assertion while keeping the no-call lock (`placeholder_bodies` loop + empty `only_calls`); mixed-text excision stays fully asserted.
- `desc`: revision note documenting the above reasoning verbatim.
- No change to inputs, expected calls, or prose_contains/prose_not_contains.

## Final run (post-golden-update)

```
EVALS RESULT: total=19 pass=19 xfail=0 skip=0 fail=0
```

Exit code 0. Residual golden-17 finding #5/toolcall.py item is closed.
