# MIGRATE-SCRIPTS-DOCS — 2026-08-26

Phạm vi: `scripts/*`, `tests/conftest.py`, docs/conventions theo phân công
(audit: `docs/reports/path-audit-scripts-unit-2026-08-26.md`). KHÔNG đụng
`gpt/**`, systemd unit, `.env` gốc (coordinator phụ trách).

Layout chốt: runtime root `$WEBGPT_RUNTIME_ROOT` default `~/.local/share/webgpt`
· registry `~/.config/webgpt/accounts.json` · profiles `~/.local/share/webgpt/profiles`
· codex-reviews → `docs/reports/codex-reviews/` · scratch vứt được → `~/Downloads`.

## Checklist

### 1. scripts/*.py hardcode Downloads

- [x] `preflight_quota.py` — bỏ hằng `DEFAULT_PROFILE_DIR`; thay bằng
      `default_profile_dir()` resolve lazy: `$WEBGPT_RUNTIME_ROOT` >
      `~/.local/share/webgpt/cloak-profile` (mirror `gpt.config.settings`
      bản mới). `PROFILE_DIR` env vẫn thắng tại nhánh resolve chính.
- [x] `verify_hybrid_flip.py:529` — workdir t2/t3 về
      `$WEBGPT_RUNTIME_ROOT/tmp/verify_hybrid_flip_<stamp>`; docstring cập nhật.
- [x] `run_practical_bench.py:57` — `RUN_ROOT_PARENT` → `bench_run_parent()`:
      `$WEBGPT_BENCH_RUN_ROOT` > `~/.local/share/webgpt/bench-run`; 2 call-site
      + 2 dòng docstring cập nhật.
- [x] `soak_lite_mock.py:247` — default `--out-dir` =
      `$WEBGPT_RUNTIME_ROOT/soak-lite` (default XDG); docstring :29 cập nhật.
- [x] `e2e_project_benchmark.py:427` + `practical_cli_bench.py:1080` — sandbox
      `tempfile.mkdtemp` không còn rơi vào tmpfs: helper `make_sandbox()` tạo
      dưới `~/Downloads/e2e-project-bench-scratch` /
      `~/Downloads/practical-cli-bench-scratch` (`$WEBGPT_SCRATCH_ROOT`
      override). Vẫn tự rmtree sau run trừ `--keep-workdir`.

### 2. pick_ctf_challenge.py sidecar

- [x] Đã ở cạnh script (`scripts/.ctf_used_challenges.json`, gitignored) đúng
      như đề xuất giữ nguyên → **không đổi**. `DEFAULT_ROOT = ~/Workspace/CTF`
      là input source, không phải rác → giữ.

### 3. CTF TARGET_DIR hardcode

- [x] `solve_ctf_with_files.py`, `run_claude_misc_task.py`,
      `run_claude_ctf_task.py` — hết hardcode `/home/light/Workspace/CTF/...`:
      resolve `--target` > `$WEBGPT_CTF_TARGET_DIR` > default workspace vứt được
      `~/Downloads/ctf-workspaces/<task>`; thêm check tồn tại → FATAL exit 2
      thay vì crash subprocess. `CLAUDE_BIN` hết hardcode:
      `$WEBGPT_CLAUDE_BIN` > `shutil.which("claude")` > `~/.local/bin/claude`.
      Log output về `<repo>/scratch/*` relative (gitignored), hết absolute path.

### 4. Shell

- [x] 10 script họ verify-*/run-* (`verify-opencode-live`,
      `verify-process-lifecycle`, `verify-soak-restart`,
      `verify-opencode-microgates`, `run-claude-code-benchmark`,
      `verify-free-anonymous`, `verify-claude-microgates`,
      `manual-verify-claude`, `run-pcap-certification`, `run-opencode-smoke`) —
      fallback đổi một lượt thành
      `` ${WEBGPT_RUNTIME_ROOT:-${HOME}/.local/share/webgpt} ``.
- [x] `webgpt-watchdog.sh:9` — `LOG_DIR` qua `${RUNTIME_ROOT}/logs` cùng pattern;
      `STATE_FILE` /tmp giữ nguyên (audit #23: vô hại khi reboot).
- [x] `run-live-case.sh:27` — echo evidence trỏ về `docs/reports/`.

### 5. tests/conftest.py

- [x] Scrub thêm 4 biến: `WEBGPT_RUNTIME_ROOT`, `PROFILE_DIR`,
      `WEBGPT_PROMPT_DEBUG_DIR`, `WEBGPT_CODEX_AUTH_JSON`.

### 6. Docs

- [x] `.env.example` — `WEBGPT_ACCOUNTS_FILE=/home/light/.config/webgpt/accounts.json`,
      `WEBGPT_PROFILES_ROOT=/home/light/.local/share/webgpt/profiles`;
      `PROFILE_DIR` chuyển commented-out kèm ghi chú default XDG.
- [x] `docs/guides/AUTOMATION_OPS.md:86,97` — watchdog.log về
      `~/.local/share/webgpt/logs/watchdog.log`.
- [x] `docs/guides/AUTH_AND_LOGIN_GUIDE.md:24,52` — profiles +
      registry theo XDG mới.

### 7. Test / lint

- [x] Targeted pytest (KHÔNG full suite):
      `test_preflight_quota test_pick_ctf_challenge test_picker_needs_remote
      test_practical_cli_bench test_e2e_benchmark_harness test_runtime_paths
      test_config_settings test_debug_login` → **122 passed**.
- [x] `bash -n` toàn bộ shell đã sửa → OK; `py_compile` 10 file Python → OK.
- [x] Ruff: 0 finding MỚI so với baseline HEAD trên mọi file đụng tới
      (so sánh `git show HEAD:` cho file tracked). 3 file CTF được viết lại
      sạch hẳn (I001/F401/F541 cũ đã xử lý); các finding còn lại trong
      soak_lite/practical_cli_bench/e2e/verify_hybrid_flip là pre-existing,
      nằm ngoài dòng đã sửa.

## Grep sót 'Downloads' trong phạm vi

`~/Downloads/webgpt*` runtime-path cũ: **0** hit còn lại trong scripts/,
tests/conftest.py, .env.example, AUTOMATION_OPS.md, AUTH_AND_LOGIN_GUIDE.md.

Các hit "Downloads" còn lại đều là **default scratch mới được chốt**
(chuẩn chủ sách — dữ liệu lớn vứt được) hoặc chú thích "never ~/Downloads":

| File | Nội dung |
|---|---|
| `run_claude_ctf_task.py`, `solve_ctf_with_files.py`, `run_claude_misc_task.py` | `~/Downloads/ctf-workspaces/<task>` (default workspace CTF vứt được) |
| `e2e_project_benchmark.py`, `practical_cli_bench.py` | `~/Downloads/<tên>-bench-scratch` (sandbox build project) |
| `verify_hybrid_flip.py`, `run_practical_bench.py`, `soak_lite_mock.py` | chú thích "never ~/Downloads" |

## Cần coordinator quyết thêm

1. **Registry path lệch 2 nguồn**: audit mục 2/5 ghi
   `~/.config/webgpt/accounts/accounts.json` (subdir `accounts/`, khớp plan mv
   từ `data/webgpt/accounts/`), message chốt của bạn ghi
   `~/.config/webgpt/accounts.json` (flat). Docs + `.env.example` hiện theo
   dạng **flat** — nếu unit/`.env` thật đang dùng subdir thì cần đồng bộ lại
   một bên trước khi start gateway.
2. `docs/automation/{STATE,ROADMAP}.md` vẫn quy ước codex-review output
   `~/Downloads/webgpt/codex-reviews/` (audit mục 4) — thuộc state automation
   loop, chưa đụng; cần cập nhật sang `docs/reports/codex-reviews/` theo tick
   của coordinator.
3. `CLAUDE.md` chưa có dòng quy ước layout XDG (audit đề xuất) — để coordinator
   quyết vì là file config checked-in.
