# T6 — Second Real CTF Solve via Gateway (REDACTED)

**Date:** 2026-08-24
**Test:** Live end-to-end repeatability check: Claude Code CLI → webgpt gateway (`:18000`) → real CTF challenge #2
**Purpose:** confirm T5 (Invoice, attempt-1 PASS) was repeatable, not a one-off fluke
**Result: PASS on attempt 1**

---

## 1. Challenge selection

- Source pool: `docs/reports/ctf-candidates-2026-08-24.json` (172 unused at pick time)
- Selection rule applied: `used_at == null`, local-only, easiest next; onboarding preferred, ≤ 50 pts, different from Invoice
- **Chosen:** `Decompile?` — category Onboarding / tag Reversing, 40 pts, 366 solves, difficulty Beginner
- Path: `<CTF_ROOT>/BrunnerCTF_2026_-_Global/Onboarding/Decompile`
- Workspace copy: `/tmp/t6-ctf/Decompile/` containing only `rev_decompile.zip` (probe stubs `solve.py`, `metadata.json`, `README.md` excluded; all three checked for flag leakage before exclusion — none found)
- Attachment: `rev_decompile.zip` → single ELF binary `rev_decompile/vault` (20.9 KB)

## 2. Run configuration

```
env ANTHROPIC_BASE_URL=http://127.0.0.1:18000 ANTHROPIC_API_KEY=sk-webgpt-local \
    CLAUDE_DEFAULT_MODEL=claude-3-5-sonnet \
    claude --dangerously-skip-permissions --print
cwd = /tmp/t6-ctf/Decompile
prompt = original challenge description verbatim + "Giải bài này. Làm việc trong thư mục hiện tại."
timeout budget = 1500 s
```

## 3. Result

**PASS — attempt 1, single turn set. Attempt 2 unused. No RateLimited encountered (no poll/retry needed).**

Flag produced by model:

```
brunner{Pl4nt3xt_p455w0rd_1s_bu551ng_4079_0x2a_B_750}
```

**Independent verification (strongest criterion — program acceptance):** the untouched binary was re-extracted into a separate clean dir (`/tmp/t6-ctf/verify/`) and driven with the model's five answers over stdin. It accepted all five (`[OK] Correct.` × 5) and printed the badge itself:

```
[*] Onboarding complete. Issuing your badge...
brunner{Pl4nt3xt_p455w0rd_1s_bu551ng_4079_0x2a_B_750}
```

Model answer and program output match byte-for-byte. The model derived every answer statically (`strings` for Q1; `objdump -d -M intel` disassembly of `main` for Q2–Q5, including decoding the double-the-input trick `add rax,rax; cmp rax,0x5dc` → 750). Evidence kept at `/tmp/t6-ctf/verify/vault_badge.out`.

## 4. Model behavior (from gateway trace + CLI transcript)

Session `wgs_…ad1d3`, conversation `6a8c…5c32` (ids truncated — REDACTED).

| Metric | Value |
|---|---|
| Wall-clock | ~116 s (14:58:21 → 15:00:17 UTC) |
| submit_start / submit_completed | 12 / 12 (all committed, zero failover) |
| parsed | 12 (11 × finish_reason=tool_calls with exactly 1 tool call each, 1 × stop) |
| tool_use emitted | 11, all Bash |
| Corrections / failovers / errors | 0 (`tool_correction`, `conversation_failover`, `submit_failed*` absent from session trace) |
| Generation durations | ~4–10 s per state_transition |

Full command chain (in order):

```text
<cmd>ls</cmd>
<cmd>unzip -l rev_decompile.zip</cmd>
<cmd>cd rev_decompile && file vault && ls -la</cmd>
<cmd>ls -la && find . -maxdepth 2 -type f -name vault -ls</cmd>
<cmd>unzip rev_decompile.zip && ls -la rev_decompile && file rev_decompile/vault</cmd>
<cmd>cd rev_decompile && ./vault</cmd>                          -> interactive prompts, no stdin
<cmd>cd rev_decompile && strings vault | grep -iE 'pass|password|...'</cmd>
<cmd>objdump -d -M intel vault | grep -A120 -B20 'badge_budget|QUESTION 5|budget'</cmd>
<cmd>pwd && ls -la</cmd>
<cmd>objdump -d -M intel vault | grep -A120 -B20 '...'</cmd>
<cmd>nm -C vault | grep main && objdump -d -M intel vault | sed -n '/<main>:/,/^$/p'</cmd>
```

**Discover-first policy: YES.** First command was `ls`; archive inspected (`unzip -l`) before extraction; `file` used to identify the binary before execution.

**Fake-success check: NONE.** The model never asserted a flag without showing the supporting evidence (strings hit + per-question asm comparisons). It did not manage to drive `./vault` interactively (no TTY stdin), so it switched to static analysis and computed all five answers from the disassembly instead of pretending the run succeeded. Its final message offered an optional write-up but did not fabricate a verification it had not performed; the flag was independently confirmed correct by running the binary.

Artifacts left in workspace: extracted `rev_decompile/` only (no junk).

## 5. Verdict

- **T6 objective met:** second real CTF challenge solved end-to-end on attempt 1 via the same gateway stack as T5 — repeatability confirmed.
- Both solves were first-attempt, zero infra retries, zero failovers, discover-first respected both times.
- Note: this challenge's intended path (Ghidra decompilation) was approximated with raw `objdump` reading of `main` — sufficient for a Beginner-tier binary; no Ghidra install was attempted or needed.
- Limitations observed: none material.

## 6. Bookkeeping

- Challenge marked used: `scripts/.ctf_used_challenges.json` (+1 → 2 total) and `used_at = 2026-08-24T22:04:30` stamped in `docs/reports/ctf-candidates-2026-08-24.json`.
- Raw evidence at `/tmp/t6-ctf/`: `attempt1.out`, `attempt1.err` (empty), `trace_attempt1.jsonl` (session-scoped gateway events), `prompt.txt`, `verify/` (fresh re-extraction + `vault_badge.out`).
- CLI transcript: `~/.claude/projects/-tmp-t6-ctf-Decompile/f476949e-3a62-46dc-ab78-5ec9d045090d.jsonl`.

*REDACTED: session/conversation ids truncated; account identifiers omitted.*
