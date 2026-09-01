# PATH-AUDIT-CORE — Bản đồ tham chiếu đường dẫn ngoài repo trong `gpt/**`

Ngày: 2026-08-26 · Phạm vi: code core `gpt/**/*.py` (+ đối chiếu env/unit/script đang chạy) · READ-ONLY audit, phục vụ migrate XDG một lần: registry/config → `~/.config/webgpt/`, dữ liệu lớn → `~/.local/share/webgpt/`.

## 1. Tổng quan

Tổng ~45 hit grep (`Path.home`, `expanduser`, `Downloads`, `*_ROOT`, `cache_dir`, tmp…) trong `gpt/**`, chia 3 nhóm:

| Nhóm | Số site | Xử lý |
|---|---|---|
| A. Hardcode `~/Downloads/webgpt` làm default dữ liệu WebGPT | **6** | **Sửa code** (đổi default) — env chỉ là belt-and-suspenders |
| B. Home của tool ngoài (CloakBrowser, codex, Claude Code host) | **7** | Giữ nguyên, đúng chỗ sẵn |
| C. `expanduser()` trung tính trên path do caller cấp | ~24 | Không đụng |

Trong nhóm A: **4/6 đã có env override**, 2/6 không có (mcp bridge output dir, ephemeral tmp). Trace-file / conversation-store / manual-verification **không có env, chỉ CLI flag** — bắt buộc sửa unit.

## 2. Bảng chi tiết nhóm A — điểm cần sửa

### A1. `gpt/utils/runtime_paths.py:13-15` — DEFAULT_RUNTIME_ROOT (trung tâm)
- **Giá trị**: `Path(os.environ.get("WEBGPT_RUNTIME_ROOT", "~/Downloads/webgpt")).expanduser()`
- **Ai dùng**: tạo toàn bộ layout `runs/{claude,opencode,smoke}`, `benchmarks/pcap`, `reverse`, `captures`, `failed-runs`, `successful-runs`, `tmp` (`RUNTIME_SUBDIRECTORIES` :17-27); `ensure_runtime_layout()` gọi ở `debug.py:1001`; `assert_runtime_path()` (:51-64) **ép** conversation-store / trace-file / prompt-debug-dir phải nằm dưới root này (`debug.py:1002-1006`) — vi phạm thì raise ValueError lúc start.
- **Cũng là default của**: `--conversation-store` (`debug.py:1331` → `<root>/tmp/conversations.json`), `manual-verification.jsonl` (`debug.py:1433,1442`).
- **Env**: có — `WEBGPT_RUNTIME_ROOT`. **Đề xuất**: sửa default thành `~/.local/share/webgpt` (1 dòng). Vẫn nên set env trong unit cho tường minh.
- ⚠️ Hệ quả quan trọng: sau khi đổi default, mọi path trong unit phải nằm dưới `~/.local/share/webgpt` nếu không sẽ bị `assert_runtime_path` từ chối lúc khởi động.

### A2. `gpt/auth/accounts.py:19-22` — account registry + profiles root
- **Hằng số**: `DEFAULT_WEBGPT_ROOT = ~/Downloads/webgpt`; `DEFAULT_ACCOUNTS_ROOT = …/accounts`; `DEFAULT_PROFILES_ROOT = …/profiles`; `DEFAULT_ACCOUNT_REGISTRY = …/accounts/accounts.json`.
- **Ai ghi/đọc**: `AccountStore.__init__` (:56-74) tạo layout 0700, đọc/ghi registry JSON + backup `.bak.*`; profile dir từng account lưu trong record và được lease cho browser.
- **Env**: `WEBGPT_ACCOUNTS_FILE` (:62), `WEBGPT_PROFILES_ROOT` (:67) — đã có.
- **Đề xuất XDG**: registry là config → `~/.config/webgpt/accounts/accounts.json` (backup `.bak` nằm cạnh, tự theo); profiles là data → `~/.local/share/webgpt/profiles`. **Cần sửa code** vì 2 hằng gốc hardcode Downloads (env hiện tại trong `.env` cũng đang trỏ Downloads).

### A3. `gpt/config/settings.py:8` — DEFAULT_PROFILE_DIR
- **Giá trị**: `~/Downloads/webgpt/cloak-profile`.
- **Ai dùng**: `AppConfig.profile_dir` (:26) → `load_config()` → `debug.py:52 _configured_profile_dir()` (default `--profile` cho login/api-server) → `auth/authenticator.py:123` và `transport/browser.py:49` (default param khi không ai cấp).
- **Env**: `PROFILE_DIR` qua `load_config` (:122-123, precedence environ > `.env`). Lưu ý `.env` được đọc theo `Path.cwd()` (:106) — unit có `WorkingDirectory=/home/light/GitHub/gpt` nên vẫn ăn.
- **Đề xuất**: `~/.local/share/webgpt/cloak-profile`. **Sửa code** đổi default; đồng thời sửa giá trị trong `.env` (đang trỏ `/home/light/Downloads/webgpt/cloak-profile` — thư mục này KHÔNG tồn tại nữa, lần start tới sẽ tự tạo profile rỗng → đúng kịch bản mất login đã xảy ra).

### A4. `gpt/utils/profile.py:9-14` — bộ hằng số trùng lặp (không có single source of truth!)
- **Hằng số**: `DEFAULT_WEBGPT_DIR`, `…/PROFILE_DIR` (tên `profile/`, khác với `cloak-profile` ở A3), `BRAVE_PROFILE_DIR`, `CLOAK_PROFILE_DIR`, `ARTIFACTS_DIR` (`reverse/`), `TMP_DIR` (`tmp/`).
- **Ai dùng**: `ensure_profile_dir` (:18-30, chmod 0700) — gọi từ authenticator/browser; `create_ephemeral_profile` (:33-46, `mkdtemp("webgpt-anon-")` dưới TMP_DIR).
- **Env**: **không có**. **Đề xuất**: đổi `DEFAULT_WEBGPT_DIR` → `~/.local/share/webgpt` (1 dòng kéo theo cả khối). Về lâu dài nên gộp về `runtime_paths.py` để hết 4 nơi định nghĩa rời rạc (A1/A2/A3/A4).

### A5. `gpt/mcp/bridge.py:21` — DEFAULT_OUTPUT_DIR (tool_outputs)
- Dump output tool dài/nhạy cảm (`{tool}_{ts}.log`, :56-57). Constructor injectable nhưng `MCPBridge` default `OutputSanitizer()` (:81). **Không có env** → **cần sửa code** → `~/.local/share/webgpt/tool_outputs`.

### A6. `gpt/mcp/installer.py:19` — DEFAULT_LOGS_DIR
- Viết `cloudflared.log` (:91). Param `logs_dir` có sẵn nhưng default hardcode → **cần sửa code** → `~/.local/share/webgpt/logs`. (Lưu ý phụ: `installer.py:18` còn hardcode absolute path sang repo khác `/home/light/GitHub/botquanganh_mcp` — ngoài scope nhưng nên biết.)

## 3. Nhóm B — giữ nguyên (không phải dữ liệu WebGPT)

| Site | Đường dẫn | Lý do giữ |
|---|---|---|
| `auth/accounts.py:398`, `debug.py:348,351` | `~/.cloakbrowser/**` | Binary CloakBrowser do package đó quản |
| `transport/codex_auth.py:86` | `~/.codex/auth.json` | Config của codex CLI (có env `WEBGPT_CODEX_AUTH_JSON`) |
| `orchestrator/session_runner.py:198,215,285` | `~/.local/bin/claude`, `~/.claude.json`, `~/.claude/projects` | Host dirs của Claude Code itself |

## 4. Nhóm C — expanduser trung tính (~24 site)

`model_registry.py:59` · `conversations.py:147` · `utils/tracing.py:50` · `utils/verification.py:108,120` · `reverse/replay.py:41` · `drivers/ui.py:857` · `debug.py:119,332,345,599,789,797,878` · `transport/browser.py:104` · `accounts.py:70,71,222,280,298,314,318,490` · `gateway/runtime.py:1361`. Toàn bộ chỉ nạp user-supplied path — không tự sinh đường dẫn ngoài repo. Riêng `gateway/runtime.py:1358` có env `WEBGPT_PROMPT_DEBUG_DIR` (opt-in, default TẮT).

## 5. Trạng thái cấu hình ĐANG CHẠY trỏ sai chỗ (phải sửa cùng lúc)

1. **systemd unit** `~/.config/systemd/user/webgpt-gateway.service`: `Environment=WEBGPT_PROMPT_DEBUG_DIR=/home/light/Downloads/webgpt/logs/prompt-debug` và `ExecStart … --trace-file /home/light/Downloads/webgpt/logs/trace.jsonl` → đổi cả hai sang `~/.local/share/webgpt/logs/…`.
2. **`.env:14`**: `PROFILE_DIR=/home/light/Downloads/webgpt/cloak-profile` → `~/.local/share/webgpt/cloak-profile`; thêm `WEBGPT_ACCOUNTS_FILE=/home/light/.config/webgpt/accounts/accounts.json` + `WEBGPT_PROFILES_ROOT=/home/light/.local/share/webgpt/profiles`.
3. **`.env.example:14,22-23`**: cập nhật mẫu tương ứng (`PROFILE_DIR=./cloak-profile` → path XDG comment mẫu).
4. **`scripts/webgpt-watchdog.sh:9-10`** (unit `webgpt-watchdog.timer` chạy mỗi 5 phút, đang ACTIVE): `LOG_DIR="$HOME/Downloads/webgpt/logs"` → `$HOME/.local/share/webgpt/logs` — nếu không sửa, watchdog sẽ tái tạo `~/Downloads/webgpt` ngay sau migrate.
5. **10 script verify/benchmark** có fallback `${WEBGPT_RUNTIME_ROOT:-${HOME}/Downloads/webgpt}`: `run-claude-code-benchmark.sh:14`, `run-opencode-smoke.sh:10`, `verify-opencode-live.sh:6`, `verify-process-lifecycle.sh:6`, `manual-verify-claude.sh:6`, `verify-soak-restart.sh:6`, `run-pcap-certification.sh:6`, `verify-claude-microgates.sh:6`, `verify-opencode-microgates.sh:6`, `verify-free-anonymous.sh:6` → đổi fallback thành `$HOME/.local/share/webgpt` (chi tiết thuộc PATH-AUDIT-SCRIPTS-UNIT nhưng liệt kê để coordinator không sót).
6. **Token cache** `webgpt-token-cache.json` (`token_manager.py:98,462-463`): cache_dir chính là **browser profile dir** (`hybrid.py:291-305`) → nằm trong `profiles/<account>/`, tự di theo profiles, không cần hành động riêng (chứa access token — tuyệt đối move nguyên vẹn, không copy dở).

## 6. Phân loại `data/webgpt/` hiện tại

| Thư mục | Dung lượng | Do đâu tạo | Đi về đâu | Kết luận |
|---|---|---|---|---|
| `profiles/` (chứa `personal/` + token cache) | 42M | AccountStore lease + CloakBrowser persistent context | `~/.local/share/webgpt/profiles/` | **KEEP — MOVE.** Login state, quý nhất |
| `accounts/` (accounts.json + .bak.1) | 12K | AccountStore registry | `~/.config/webgpt/accounts/` | **KEEP — MOVE** |
| `logs/` (trace.jsonl, prompt-debug/*.json|.txt, watchdog.log) | 1.1M | unit `--trace-file` + WEBGPT_PROMPT_DEBUG_DIR + watchdog script | `~/.local/share/webgpt/logs/` | MOVE; prompt-debug cũ có thể xoá (đã có cap `WEBGPT_DEBUG_MAX_FILES`=500) |
| `tmp/` (conversations.json + .lock + codex13fix-b-parser-check.py) | 4M | ConversationStore persist (`debug.py:1331`) + flock | `~/.local/share/webgpt/tmp/` | MOVE cặp conversations.json(.lock); **file `.py` lạc đề = RÁC** (xoá, nội dung đáng giữ thì đưa vào docs/reports) |
| `codex-reviews/` | 16K | codex exec cross-check loop ghi ra runtime root (STATE.md:199,214,430) | archive → `docs/reports/` rồi xoá | RÁC/evidence — không phải dữ liệu runtime |
| `runs/{claude,opencode,smoke}` · `benchmarks/pcap` · `captures` · `reverse` · `failed-runs` · `successful-runs` | 0 (rỗng) | `ensure_runtime_layout()` tự dựng scaffold | — | **RÁC — XOÁ**, tự tái tạo tại root mới |

Đã có sẵn `~/.local/share/webgpt/manual-verification.jsonl` (từ lần chạy trước) — giữ lại. `~/.config/webgpt/` chưa tồn tại.

## 7. Spec migrate đề xuất (thứ tự thực hiện)

```bash
# 0. Gateway đang DỪNG (theo STATE.md:584) — giữ nguyên trạng thái tới bước 6.
# 1. Tạo layout XDG
mkdir -p ~/.config/webgpt/accounts ~/.local/share/webgpt/{logs,prompt-debug,tmp}
# 2. Move data (mv giữ nguyên inode/permission 0700)
mv ~/GitHub/gpt/data/webgpt/profiles      ~/.local/share/webgpt/
mv ~/GitHub/gpt/data/webgpt/accounts/accounts.json*  ~/.config/webgpt/accounts/
mv ~/GitHub/gpt/data/webgpt/logs/trace.jsonl         ~/.local/share/webgpt/logs/
mv ~/GitHub/gpt/data/webgpt/tmp/conversations.json*  ~/.local/share/webgpt/tmp/
# 3. Rác: rm -rf data/webgpt (scaffold rỗng + codex-reviews + tmp/*.py);
#    muốn giữ review md thì cp sang docs/reports/ trước.
```

4. **Sửa code (6 dòng)**: `runtime_paths.py:14` → `"~/.local/share/webgpt"`; `utils/profile.py:9` → `~/.local/share/webgpt`; `settings.py:8` → `~/.local/share/webgpt/cloak-profile`; `accounts.py:19-22` → registry `~/.config/webgpt/accounts/accounts.json` + profiles `~/.local/share/webgpt/profiles`; `mcp/bridge.py:21`; `mcp/installer.py:19`.
5. **Sửa env/unit**: `.env`, `.env.example`, unit `webgpt-gateway.service` (2 dòng), `scripts/webgpt-watchdog.sh` (1 dòng), 10 script fallback (nhóm SCRIPTS-UNIT).
6. `systemctl --user daemon-reload && systemctl --user start webgpt-gateway.service` → kiểm tra `/health` + `trace.jsonl` ghi đúng chỗ mới + `ls ~/Downloads/webgpt` phải KHÔNG tái xuất hiện.
7. Chạy 1 tick smoke (T1) rồi mới refire batch automation.

Rủi ro chính nếu bỏ sót: watchdog timer (mỗi 5') và bất kỳ script verify nào sẽ tái tạo `~/Downloads/webgpt` rỗng; `AccountStore._warn_if_registry_missing` sẽ cảnh báo "registry missing but profiles exist" nếu move lệch — coi log này là dấu hiệu migrate sai ngay.
