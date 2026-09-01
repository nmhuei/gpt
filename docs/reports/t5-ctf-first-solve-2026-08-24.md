# T5 — First Real CTF Solve via Gateway (REDACTED)

**Date:** 2026-08-24
**Test:** Live end-to-end: Claude Code CLI → webgpt gateway (`:18000`) → real CTF challenge
**Gateway config:** the winning VERIFY-R7d configuration (stealth protocol, multi-turn parity confirmed)
**Result: PASS on attempt 1**

---

## 1. Challenge selection

- Source pool: `docs/reports/ctf-candidates-2026-08-24.json` (173 candidates, all `used_at == null` at pick time)
- Selection rule applied: easiest unused + local-only; prefer onboarding/crypto with lowest points
- **Chosen:** `Invoice` — category Onboarding (forensics/malware), 35 pts, 302 solves, difficulty Beginner
- Path: `<CTF_ROOT>/BrunnerCTF_2026_-_Global/Onboarding/Invoice`
- Workspace copy: `/tmp/t5-ctf/Invoice/` (cleaned of probe artifacts `solve.py` stub + `metadata.json`; verified no flag leakage in either before removal)
- Attachment: `forensics_invoice.zip` containing `Invoice #1337.docm` (macro-enabled Word doc)

## 2. Run configuration

```
env ANTHROPIC_BASE_URL=http://127.0.0.1:18000 ANTHROPIC_API_KEY=sk-webgpt-local \
    CLAUDE_DEFAULT_MODEL=claude-3-5-sonnet \
    claude --dangerously-skip-permissions --print
cwd = /tmp/t5-ctf/Invoice
prompt = original challenge description verbatim + "Giải bài này. Làm việc trong thư mục hiện tại."
timeout budget = 1500 s (not needed)
```

## 3. Result

**PASS — attempt 1, first turn set.**

Flag produced by model:

```
brunner{1_w0nt_p4y_th3m_4_d1me}
```

**Independent verification (criterion b — flag format grep + raw evidence):** raw-string search over `word/vbaProject.bin` inside the untouched zip confirms both fragments `brunner{` and `1_w0nt_p4y_th3m_4_d1me}` exist verbatim in the VBA project (flag is split across concatenated string literals in a `Document_open()` auto-run macro). Model's answer matches exactly.

Model's final message also correctly characterized the challenge class (VBA macro forensics; extract with olevba rather than executing the document).

## 4. Model behavior (from gateway trace + CLI transcript)

Session `wgs_…78b`, conversation `6a8c…77f0` (ids truncated — REDACTED). Protocol: `anthropic_messages`; backend model label in payload: mapped alias of requested `claude-3-5-sonnet`.

| Metric | Value |
|---|---|
| Wall-clock | ~115 s (14:44:04 → 14:45:59 UTC) |
| Gateway generation span | ~67 s |
| submit_start / submit_completed | 10 / 10 (all committed, zero failover) |
| request_start / request_completed / parsed | 9 / 9+ / 9 (8 × finish_reason=tool_calls, 1 × stop) |
| tool_use emitted | 9, all Bash, every generation closed cleanly (khép kín) |
| Corrections / failovers / errors | 0 (`tool_correction`, `conversation_failover`, `submit_failed*` all absent) |

Full command chain (in order):

```text
<cmd>ls</cmd>
<cmd>unzip -l forensics_invoice.zip</cmd>
<cmd>unzip -o forensics_invoice.zip && file "forensics_invoice/Invoice #1337.docm"</cmd>
<cmd>unzip -l "forensics_invoice/Invoice #1337.docm"</cmd>
<cmd>olevba "forensics_invoice/Invoice #1337.docm"</cmd>            -> not installed
<cmd>sudo apt install -y oletools</cmd>                             -> no tty for sudo
<cmd>python3 -m pip install --user oletools</cmd>                   -> externally-managed-environment
<cmd>python3 -m venv .venv && . .venv/bin/activate && pip install oletools</cmd>
<cmd>. .venv/bin/activate && olevba "forensics_invoice/Invoice #1337.docm"</cmd>
```

**Discover-first policy: YES.** First action was `ls` of the workspace, then archive inspection before any analysis. Notable resilience: when `olevba` was missing it degraded gracefully through three install strategies (apt → pip --user → local venv) without leaving the task, then completed extraction. Artifacts created in workspace: extracted `forensics_invoice/` dir + `.venv` (tooling only, no junk).

## 5. Verdict

- **T5 objective met:** the gateway stack that passed multi-step parity (VERIFY-R7d) solves a real, never-before-attempted CTF challenge end-to-end on the first attempt.
- No infrastructure retries were needed; attempt 2 unused per protocol.
- Limitations observed: none material. The only friction (missing olevba) was resolved autonomously by the model.

## 6. Bookkeeping

- Challenge marked used: `scripts/.ctf_used_challenges.json` (+1; picker now reports 172 remaining) and `used_at` stamped in `docs/reports/ctf-candidates-2026-08-24.json`.
- Raw evidence kept at `/tmp/t5-ctf/`: `attempt1.out`, `attempt1.err` (empty), `trace_attempt1.jsonl`, `verify/` (independent extraction).
- CLI transcript: `~/.claude/projects/-tmp-t5-ctf-Invoice/9d5ef5ce-f2c9-41c1-b650-58012cd8f5b8.jsonl`.

*REDACTED: session/conversation ids truncated; account identifiers omitted.*
