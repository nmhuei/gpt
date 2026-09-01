# PATH-AUDIT-SCRIPTS-UNIT — 2026-08-26

Phạm vi: `scripts/*`, systemd user units, tests fixtures, docs/conventions, `data/webgpt` provenance.
Bối cảnh: migrate toàn bộ runtime data ra khỏi `~/Downloads` về layout XDG
(`~/.config/webgpt` = registry/config · `~/.local/share/webgpt` = profiles/logs/runs/captures).
Gateway đang DỪNG có chủ đích — không start lại cho tới checklist xong.

Tổng: **~41 điểm** cần đụng tới (12 file `.py`, 11 file `.sh`, 2 unit, conftest, 6 docs/convention).

---

## 1. scripts/*.py — hardcoded paths

| # | File:line | Path hiện tại | Đề xuất |
|---|---|---|---|
| 1 | `scripts/preflight_quota.py:75` | `DEFAULT_PROFILE_DIR = Path.home()/"Downloads"/"webgpt"/"cloak-profile"` | default theo `WEBGPT_PROFILES_ROOT` nếu set, else `~/.local/share/webgpt/cloak-profile`; vẫn nhận `PROFILE_DIR` env (đã hỗ trợ tại :356) |
| 2 | `scripts/verify_hybrid_flip.py:529` (+docstring :14) | `workdir = Path.home()/"Downloads"/f"verify_hybrid_flip_{stamp}"` | dùng `runtime_path()`/`WEBGPT_RUNTIME_ROOT` → `<root>/tmp/verify_hybrid_flip_<stamp>` |
| 3 | `scripts/run_practical_bench.py:57` (+docstring :13,:20) | `RUN_ROOT_PARENT = Path.home()/"Downloads"/"bench-run"` | env `WEBGPT_BENCH_RUN_ROOT`, default `~/.local/share/webgpt/bench-run` |
| 4 | `scripts/pick_ctf_challenge.py:66` | `DEFAULT_ROOT = ~/Workspace/CTF` | input source, không phải rác — giữ, thêm env override `WEBGPT_CTF_ROOT` (đã có `--root`) |
| 5 | `scripts/pick_ctf_challenge.py:67` | sidecar state `scripts/.ctf_used_challenges.json` | state trong scripts/ — nên `~/.local/state/webgpt/ctf-used.json` (env `WEBGPT_CTF_USED_FILE`); hiện đã gitignore nên ưu tiên thấp |
| 6 | `scripts/solve_ctf_with_files.py:19,113` | `TARGET_DIR=/home/light/Workspace/CTF/...` hardcode; output `scratch/ctf_solve_output.md` | `--target` arg / env; scratch giữ repo (đã gitignore) hoặc in ra stdout |
| 7 | `scripts/run_claude_misc_task.py:18,88` | như trên + `scratch/misc_challenge_3_output.md` | như trên |
| 8 | `scripts/run_claude_ctf_task.py:19-20` | `TARGET_DIR`, `CLAUDE_BIN` hardcode | arg/env (`WEBGPT_CLAUDE_BIN`) |
| 9 | `scripts/e2e_project_benchmark.py:427` | `tempfile.mkdtemp(prefix="e2e-taskmanager-")` → /tmp (tmpfs=RAM) | sandbox có thể lớn — tôn trọng `TMPDIR` do caller set (= runtime-root tmp), hoặc mkdtemp dưới `runtime_path("tmp")`. `:40` CLAUDE_BIN fallback OK |
| 10 | `scripts/practical_cli_bench.py:1080` | `tempfile.mkdtemp(prefix="practical-cli-bench-")` → /tmp | như trên. `:59` CLAUDE_BIN fallback OK |
| 11 | `scripts/soak_lite_mock.py:247` | default out `Path.home()/"Downloads"/"soak-lite"` | `~/.local/share/webgpt/soak-lite` (env override) |
| 12 | `scripts/codex_oauth_login.py:303` | `~/.codex/auth.json` | OK — chuẩn home-config kiểu XDG, đã có env `WEBGPT_CODEX_AUTH_JSON`; KHÔNG đổi |

Shell scripts (cùng họ, đều đã env-driven `WEBGPT_RUNTIME_ROOT`, chỉ fallback literal sai):

| # | File:line | Nội dung |
|---|---|---|
| 13–21 | `verify-opencode-live.sh:6`, `verify-process-lifecycle.sh:6`, `verify-soak-restart.sh:6`, `verify-opencode-microgates.sh:6`, `run-claude-code-benchmark.sh:14`, `verify-free-anonymous.sh:6`, `verify-claude-microgates.sh:6`, `manual-verify-claude.sh:6`, `run-pcap-certification.sh:6`, `run-opencode-smoke.sh:6` | `RUNTIME_ROOT="${WEBGPT_RUNTIME_ROOT:-${HOME}/Downloads/webgpt}"` — sửa fallback thành `~/.local/share/webgpt` (pattern `TMPDIR=$RUNTIME_ROOT/tmp` của chúng là đúng, giữ nguyên) |
| 22 | `scripts/webgpt-watchdog.sh:9` | `LOG_DIR="$HOME/Downloads/webgpt/logs"` → `~/.local/share/webgpt/logs` |
| 23 | `scripts/webgpt-watchdog.sh:8` | `STATE_FILE=/tmp/webgpt-watchdog-fail-count` — file đếm vài byte, chấp nhận được ở /tmp (mất trạng thái khi reboot = vô hại); có thể chuyển `$XDG_STATE_HOME/webgpt` nếu muốn |
| 24 | `scripts/run-live-case.sh:27` | text echo nhắc save evidence dưới `~/Downloads/webgpt/` → sửa thành `docs/reports/` |

Sạch (không cần sửa): `fconv_replay.py` (toàn bộ qua argparse, không hardcode), `auto_solver.py`, `review_gate.py`, `benchmark_*.py`, `stress_test_10_turns_session.py`, `reverse-chatgpt-lifecycle.py`, `wait-for-anonymous-ready.py`, các `test_live_*`.

---

## 2. Systemd unit `~/.config/systemd/user/webgpt-gateway.service`

Nguyên văn các dòng liên quan:

```
Environment=WEBGPT_TOOL_PROTOCOL=soft
Environment=WEBGPT_PROMPT_DEBUG_DIR=/home/light/Downloads/webgpt/logs/prompt-debug   ← SỬA
ExecStart=... --trace-file /home/light/Downloads/webgpt/logs/trace.jsonl ...          ← SỬA
```

Cần sửa/thêm:

| Env | Giá trị mới |
|---|---|
| `WEBGPT_PROMPT_DEBUG_DIR` | `/home/light/.local/share/webgpt/logs/prompt-debug` |
| `--trace-file` (ExecStart) | `/home/light/.local/share/webgpt/logs/trace.jsonl` |
| `WEBGPT_RUNTIME_ROOT` (THÊM) | `/home/light/.local/share/webgpt` |
| `WEBGPT_ACCOUNTS_FILE` (THÊM) | `/home/light/.config/webgpt/accounts/accounts.json` |
| `WEBGPT_PROFILES_ROOT` (THÊM) | `/home/light/.local/share/webgpt/profiles` |
| `PROFILE_DIR` (THÊM, khuyến nghị) | `/home/light/.local/share/webgpt/cloak-profile` — vì `.env` máy thật đang có `PROFILE_DIR=/home/light/Downloads/webgpt/cloak-profile`; environ > `.env` nên unit-level ghi đè được mà không cần sửa `.env` |

Unit khác: `webgpt-watchdog.service` chạy `scripts/webgpt-watchdog.sh` → sửa script là đủ (mục 1 #22). `webgpt-auto-review.service` viết vào `docs/reports/auto-review/` (repo, đã gitignore) — OK.

LƯU Ý QUAN TRỌNG (core-scope nhưng ảnh hưởng migrate): `gpt/utils/runtime_paths.py:13-15` đọc `WEBGPT_RUNTIME_ROOT` lúc **import-time** vào hằng module `DEFAULT_RUNTIME_ROOT`. Mọi process khởi động trước khi env được sửa sẽ cứ bám root cũ — đây là lý buộc phải sửa unit/env TRƯỚC khi start gateway.

---

## 3. tests/conftest.py — scrub env

Hiện scrub: `ANTHROPIC_BASE_URL/API_KEY`, `CLAUDE_DEFAULT_MODEL`, `CLAUDE_CODE_MAX_*`,
`WEBGPT_DEFAULT_ACCOUNT`, `WEBGPT_ACCOUNTS_FILE`, `WEBGPT_PROFILES_ROOT`.

**Chưa đủ cho layout mới.** Cần bổ sung vào autouse fixture:

- `WEBGPT_RUNTIME_ROOT` — nguy nhất: đọc import-time trong runtime_paths.py; một export lạ trong shell là test chạm thư mục thật.
- `PROFILE_DIR` — preflight_quota.py và gpt.config.settings cùng đọc key này.
- `WEBGPT_PROMPT_DEBUG_DIR` — tránh test dump vào logs thật.
- `WEBGPT_CODEX_AUTH_JSON` — tránh test đụng auth thật.

(`tests/test_runtime_paths.py` đã tự dùng `tmp_path` param nên an toàn; `test_debug_login.py` scrub CHATGPT_* riêng — OK.)

---

## 4. Docs & conventions

| File:dòng | Nội dung cần cập nhật |
|---|---|
| `.env.example:22-23` | `WEBGPT_ACCOUNTS_FILE=/home/light/Downloads/webgpt/accounts/accounts.json`, `WEBGPT_PROFILES_ROOT=.../profiles` → `~/.config/webgpt/accounts/accounts.json`, `~/.local/share/webgpt/profiles` |
| `.env.example:14` | `PROFILE_DIR=./cloak-profile` — stale, nên ghi rõ `~/.local/share/webgpt/cloak-profile` |
| `docs/guides/AUTOMATION_OPS.md:86,97` | `tail ~/Downloads/webgpt/logs/watchdog.log` → `~/.local/share/webgpt/logs/watchdog.log` |
| `docs/guides/AUTH_AND_LOGIN_GUIDE.md:24,52` | profiles + accounts.json "under `~/Downloads/webgpt/`" → XDG |
| `docs/plans/MASTER_EXECUTION_PLAN.md` (:114,120,137,257,1407,1992…) | nhiều ref `~/Downloads/webgpt/...` — tài liệu kế hoạch lịch sử: thêm 1 dòng chú thích đầu file "layout đã chuyển XDG 2026-08-26" thay vì sửa từng dòng |
| `CLAUDE.md` | không nhắc Downloads — nên thêm 1 dòng quy ước layout XDG mới để agent sau khỏi tái tạo |
| `docs/automation/STATE.md:199,214,430`, `docs/automation/ROADMAP.md:87` | quy ước output codex review `~/Downloads/webgpt/codex-reviews/...` (thực tế file đang nằm ở `data/webgpt/codex-reviews/` — lạc quỹ đạo cả 2 nơi) |

**Quy ước automation loop đề xuất (thay thế):**

- Codex review output → `docs/reports/codex-reviews/` (repo, track Git — trùng convention reports hiện có; các review là artifact giá trị, mất thì tiếc). Cron bước (0b) + memory index cần cập nhật theo.
- Scratch policy mới: rác tạm (sandbox, run throw-away) → `TMPDIR`/`~/Downloads`, xoá được bất cứ lúc nào; mọi thứ muốn giữ (report, evidence, review) → repo `docs/reports/**`. Không bao giờ ghi dữ liệu quan trọng vào `~/Downloads` hay `/tmp` nữa.

---

## 5. `data/webgpt` — provenance & đích đến

Cấu trúc con khớp **chính xác** `RUNTIME_SUBDIRECTORIES` trong `gpt/utils/runtime_paths.py:17-27`
(`runs/{claude,opencode,smoke}`, `benchmarks/pcap`, `reverse`, `captures`, `failed-runs`,
`successful-runs`, `tmp`) ⇒ sinh bởi `ensure_runtime_layout()` khi `WEBGPT_RUNTIME_ROOT`
từng trỏ vào repo (STATE.md:584 xác nhận: data được mv từ ~/Downloads/webgpt vào
`data/webgpt`, symlink đã bị owner gỡ). Bằng chứng phụ:

- `data/webgpt/tmp/conversations.json` + `.lock` — default conversation store của `gpt/debug.py:1331` = `RUNTIME_ROOT/tmp/conversations.json`.
- `benchmarks/pcap` ← `run-pcap-certification.sh` ghi `$RUNTIME_ROOT/benchmarks/pcap`.
- `logs/{prompt-debug,trace.jsonl,watchdog.log}` ← gateway debug dir + watchdog (bản cũ trước khi mv).
- `accounts/`, `profiles/personal` (42 MB, chứa session cookies) ← registry env.
- `codex-reviews/codex{12,13,14}-*.md` ← coordinator tick codex cross-check.

Đích đến khi migrate:

| Nguồn (data/webgpt/) | Đích |
|---|---|
| `accounts/accounts.json*` | `~/.config/webgpt/accounts/` (chmod 600/700) |
| `profiles/` | `~/.local/share/webgpt/profiles/` |
| `logs/`, `runs/`, `benchmarks/`, `captures/`, `reverse/`, `successful-runs/`, `failed-runs/`, `tmp/` | `~/.local/share/webgpt/<tên-gốc>/` |
| `codex-reviews/*.md` | `docs/reports/codex-reviews/` (repo) |
| còn lại của `data/` | xoá; giữ entry `data/` trong `.gitignore` thêm 1 thời gian |

`.gitignore` hiện đã chặn đúng `data/`, `scratch/`, `.ctf_used_challenges.json`, `docs/reports/auto-review/` — không cần thêm gì.

---

## 6. Checklist migrate (thứ tự an toàn — tránh gateway tự tái tạo rác)

1. [ ] Gateway GIỮ stopped (đang vậy). Không start lại trước bước 9.
2. [ ] Sửa `~/.config/systemd/user/webgpt-gateway.service`: 2 giá trị Downloads + thêm 4 env (mục 2). `systemctl --user daemon-reload`.
3. [ ] Sửa `.env.example` (+ kiểm tra `.env` thật: `PROFILE_DIR` hoặc xoá dòng đó vì unit đã set).
4. [ ] `mkdir -p ~/.config/webgpt/accounts ~/.local/share/webgpt/{logs,profiles,tmp}`; `chmod 700` các thư mục này.
5. [ ] `mv` data/webgpt theo bảng mục 5 (mv giữ nguyên inode/cookie profile — đừng cp -r rồi bỏ sót Singleton*).
6. [ ] `mkdir docs/reports/codex-reviews && mv data/webgpt/codex-reviews/*.md` vào đó; cập nhật STATE/ROADMAP/memory về quy ước mới.
7. [ ] Patch scripts theo bảng mục 1 (ưu tiên: preflight_quota, verify_hybrid_flip, run_practical_bench, soak_lite_mock, watchdog.sh; họ verify-*.sh sửa fallback 1 lượt; các run_claude_*/solve_ctf_* chuyển sang arg/env).
8. [ ] Bổ sung 4 biến scrub vào `tests/conftest.py` (mục 3).
9. [ ] Chạy test đích (KHÔNG full suite): `pytest tests/test_runtime_paths.py tests/test_config_settings.py tests/test_account_default.py tests/test_debug_login.py -q` + `ruff check scripts/`.
10. [ ] Cập nhật docs mục 4 (AUTOMATION_OPS, AUTH_AND_LOGIN_GUIDE, CLAUDE.md 1 dòng, MASTER plan chú thích).
11. [ ] `systemctl --user start webgpt-gateway` → curl health OK → kiểm tra `ls ~/Downloads/webgpt` KHÔNG được tái sinh; tail trace/log ở vị trí XDG mới.
12. [ ] Refire: preflight_quota → T1 → fconv replay bước 4 (script đã sạch path, chỉ còn bug oai-device-id thuộc tick khác).
13. [ ] Sau vài ngày ổn định: xoá hẳn `data/` + rác `~/Downloads/webgpt*`, `~/Downloads/bench-run`, `~/Downloads/verify_hybrid_flip_*`, `~/Downloads/soak-lite`.

**Rủi ro lớn nhất:** lệch env giữa unit / `.env` / shell khiến gateway boot với root cũ-mới trộn lẫn (đặc biệt `PROFILE_DIR` trong `.env` còn trỏ Downloads, và `DEFAULT_RUNTIME_ROOT` đọc env lúc import) → tự login profile anon mới, churn quota, và tái tạo `~/Downloads/webgpt`. Khử bằng bước 2+3 làm TRƯỚC khi start và bước 11 kiểm tra ngược.
