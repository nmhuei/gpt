# gpt — WebGPT Local Gateway

ChatGPT Web → Anthropic/OpenAI-compatible reverse gateway. Python ≥3.10, always use `.venv/bin/python` (system python lacks `cloakbrowser`).

## Commands

```bash
# test
.venv/bin/python -m pytest -q            # NOTE: no pytest-timeout plugin — do NOT pass --timeout

# lint / typecheck
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy .

# install (editable + dev deps)
.venv/bin/pip install -e ".[dev]"

# run gateway manually (production runs via systemd --user webgpt-gateway.service)
.venv/bin/python -m gpt.debug api-server --port 18000 --transport hybrid --headless
```

## Repo-specific notes

- Launcher `gpt` in PATH is a symlink to `scripts/webgpt-claude.sh`; it is intentionally NOT a console-script here so reinstalls can't clobber it.
- Gateway unit: `~/.config/systemd/user/webgpt-gateway.service` (watchdog timer every 5 min).
- Automation loop state lives in `docs/automation/{ROADMAP,DECISIONS,STATE,FAILURES}.md` — read before touching orchestration behavior; keep them updated when changing operational semantics.
- Config precedence: environ > `.env` > defaults (`gpt/config/settings.py`). Never commit real credentials; `.env` is gitignored by design.
- Runtime data layout (XDG, migrated 2026-08-26): profiles/logs/tmp → `~/.local/share/webgpt/`, account registry → `~/.config/webgpt/accounts.json`, codex review outputs → `docs/reports/codex-reviews/`. NEVER write runtime data under `~/Downloads/webgpt` (owner policy: important data in repo/home config dirs, trash in `~/Downloads`).
- Tool protocol canonical value on the unit is `WEBGPT_TOOL_PROTOCOL=soft` (stealth handshake) — do not flip to other modes without checking DECISIONS.md.
