# Script layout

The normal user interface is the `gpt` CLI. Top-level scripts are limited to
operational entrypoints that are still owned by systemd/automation or are
stable compatibility surfaces.

## Root operational entrypoints

- `auto_review.sh` — systemd daily review wrapper.
- `ctf_monitor.py` — lightweight CTF/gateway observability monitor used by the current CTF workflow.
- `webgpt-watchdog.sh` — systemd health watchdog.
- `verify.sh` — local aggregate verification.
- `review_gate.py` — deterministic review gate.
- `preflight_quota.py` — quota preflight used by automation.
- `pick_ctf_challenge.py` — CTF picker used by automation.
- `practical_cli_bench.py` — practical benchmark engine.
- `run_practical_bench.py` — direct-agent practical benchmark runner.
- `webgpt-claude.sh` — legacy Claude Code compatibility launcher.

Prefer the unified commands where available:

```bash
gpt bench practical
gpt bench soak
gpt bench e2e
gpt bench selfcheck
gpt bench review
gpt account codex-login
gpt compat claude
```

## Grouped harnesses

- `bench/` — benchmark, soak and performance/certification workloads.
- `cert/` — live protocol probes and certification microgates.
- `auth/` — interactive credential minting helpers.
- `legacy/` — older Claude-specific/CTF harnesses kept for reproducibility.

Files in grouped directories are not the primary UX. They remain executable for
reproducibility and historical reports, while active guides should prefer
`gpt` commands.
