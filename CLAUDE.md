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
- Automation loop state, active issues & session logs live in `docs/automation/{LOG,ACTIVE_ISSUES,SOLVE_PLAYBOOK,ROADMAP,DECISIONS,STATE,FAILURES}.md` — read `ACTIVE_ISSUES.md` and `LOG.md` before starting; append session logs to `LOG.md`, log newly discovered bugs to `ACTIVE_ISSUES.md`, and remove/archive resolved bugs once verified.
- **PLAYBOOK-FIRST CTF RULE**: Khi giải bất kỳ challenge nào, agent PHẢI đọc `docs/automation/SOLVE_PLAYBOOK.md` theo category của bài và thử toàn bộ các Flow đã có trước; CHỈ KHI toàn bộ Flow cũ fail mới nghĩ hướng giải mới. Sau khi giải xong một bài, BẮT BUỘC cập nhật flow giải vào đúng category trong `SOLVE_PLAYBOOK.md`.
- **STRICT NO-WRITEUP-SEARCH RULE (No Spoilers, Yes Research)**: TUYỆT ĐỐI KHÔNG tìm kiếm writeup, flag có sẵn hay bài giải trực tiếp của đề thi (Anti-Spoiler). TUY NHIÊN, **ĐƯỢC PHÉP VÀ KHUYẾN KHÍCH** tìm kiếm tài liệu kỹ thuật chuyên sâu (Specification, RFC, whitepaper thuật toán), phân tích tính chất toán học của primitive (ví dụ đặc tả cấu trúc xxHash3, điểm yếu vi sai), tài liệu API/công cụ (Z3, SageMath, fpylll, SAT solvers) để nâng cao kiến thức và tăng tốc độ giải tự lực.
- Config precedence: environ > `.env` > defaults (`gpt/config/settings.py`). Never commit real credentials; `.env` is gitignored by design.
- Runtime data layout (XDG, migrated 2026-08-26): profiles/logs/tmp → `~/.local/share/webgpt/`, account registry → `~/.config/webgpt/accounts.json`, codex review outputs → `docs/reports/codex-reviews/`. NEVER write runtime data under `~/Downloads/webgpt` (owner policy: important data in repo/home config dirs, trash in `~/Downloads`).
- Tool protocol canonical value on the unit is `WEBGPT_TOOL_PROTOCOL=soft` (stealth handshake) — do not flip to other modes without checking DECISIONS.md.
