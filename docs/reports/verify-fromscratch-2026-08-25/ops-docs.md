# VERIFY OPS & DOCS — 2026-08-25

Phạm vi: memory-bank docs/automation/, scripts, launcher, systemd units, guides, untracked cleanup. READ-ONLY (trừ file báo cáo này).

## 1. ROADMAP ↔ code (đối chiếu 16 dòng status)

### Khớp (done có evidence rõ trong code)
| Row | Evidence |
|---|---|
| C1 env-per-terminal | `gpt/config/settings.py:102-111` — precedence environ > .env > default |
| C2 delta_encoding v1 | `gpt/transport/curl_transport.py:76,634,692` |
| G0 finalize prepare_token | `gpt/transport/token_manager.py:496-515` |
| W-T1 Sentinel TTL cache | `token_manager.py:66-71` (`_DEFAULT_SENTINEL_TTL=480`, margin env) + `:355-382` |
| W-B1B2B4 deadline+semaphore | `gpt/orchestrator/race_solver.py:105,110,152,214` |
| B3-full cooperative cancel | `session_runner.py:28,88,122` SIGTERM grace→SIGKILL killer-watchdog |
| AUTH-FIX | `authenticator.py:81-109` (_SSO_EXCLUSION), `:135` (mfa_input_timeout=60), `:420` (_wait_for_landing) |
| T4-PERSIST TokenBundle | `token_manager.py:293-321` atomic 0o600 + cache_dir |
| T-SENTINEL-WIRE | `WEBGPT_SENTINEL_SDK` flag `token_manager.py:78`; proof/turnstile headers `curl_transport.py:451-456` |
| CF-RESILIENCE | `browser.py:30-31` REFUSE mặc định opt-in =0; `curl_transport.py:36-40` chrome146, `:147-160` re-mint retry |
| P2-affinity | `factory.py:21-28` WEBGPT_WORKER_AFFINITY, `:88-90` affinity LRU map |
| TOOL-PROTO/MULTI | `toolcall.py:37-38` protocols xml/json-fn/both/soft |
| PROMPT-LAB-2 SOFT-COMPACT | `runtime.py:853-857` merged vào `_SOFT_HANDSHAKE_TEXT` |
| LIVE-F4 stream hygiene | `curl_transport.py:81-99` channel filter/dedupe/Thinking-strip + kill-switch; mirror ở `ui.py:124-139` |
| W3-PERSIST | `conversations.py:131-370` coalesced background flush, mark_pending, close() flush |

### LỆCH
1. **P2-server = "todo"** nhưng code đã đủ: leak-fix `_conversation_locks` pop (`gateway/server.py:462-473`), `_response_sessions` OrderedDict evict (`:487-496`), invalidate_sentinel wired (`curl_transport.py:139,160,203,459-472`), health/default hook `_lease_session` (`server.py:366,415`). Mâu thuẫn nội bộ với row WAVE2 (cùng scope, "done").
2. **A3 khai báo 2 lần trái ngược** (dòng 15 "in-progress", dòng 34 "todo"). Code: `failover.py` + `account_health.py` tồn tại VÀ đã wire `gateway/server.py:65-66,333,363,373,499-514,932-936` + `tests/test_failover.py`. Status stale so với code.
3. **W-A1A4A2**: chú thích "còn lại hook resolve_default_account vào server.py (wave 2)" — hook đã có ở `server.py:366`.
4. **E2E-BENCH trùng 2 dòng** (dòng 36 và 38, cùng "in-progress").
5. **GATEWAY-CFG drift**: row nói unit chạy `WEBGPT_TOOL_PROTOCOL=both`, unit hiện tại là `soft` (drift do pivot stealth protocol theo DECISIONS #12/#15 — config đúng hướng mới, ROADMAP chưa cập nhật).
6. **R4-DOUBLING = "todo"**: đúng — không tìm thấy cơ chế chặn SDK retry mid-stream trong transport/api.

## 2. Scripts

| Script | Kết quả |
|---|---|
| webgpt-claude.sh | `bash -n` OK — sống, logic status/restart/wait-gateway chuẩn |
| pick_ctf_challenge.py | `--help` OK (argparse đầy đủ) |
| auto_solver.py | `--help` OK |
| run_claude_e2e_live_test.py, test_live_claude_fanout/action/pty.py | có `__main__` guard — an toàn (--help là no-op) |
| **run_claude_ctf_task.py, run_claude_misc_task.py, solve_ctf_with_files.py** | **CHẾT NHƯ TOOL / NGUY HIỂM**: không argparse, không main-guard, TARGET_DIR hardcode bài CTF cũ (Web_Challenge_2, Misc_Challenge_3), tự bắn `claude -p` live vào :18000 ngay khi thực thi |

**Sự cố minh họa (công bố thẳng thắn):** probe `--help` trên 3 script guard-less đã kích hoạt module-body của chúng ≤30s tới khi timeout giết (stdout block-buffered nên im lặng, exit hiển thị 0 là của `head`). Journal gateway ghi POST /v1/messages lúc 12:51:25/12:51:51/12:54:01 — không quy kết riêng được vì 2 session claude interactive của owner cũng đang chạy. Không còn tiến trình mồ côi. Đây chính là lý do 3 script này phải coi là dead-scratch.

## 3. Launcher
- Symlink `~/.local/bin/gpt` → `/home/light/GitHub/gpt/scripts/webgpt-claude.sh`: **hợp lệ**, target tồn tại.
- pyproject `[project.scripts]` chỉ có `gpt-web = "gpt.debug:main"` (main() tồn tại `gpt/debug.py:903`), comment ghi rõ `gpt` cố tình không khai báo console-script. **Đúng DECISIONS #6**, không trái.

## 4. systemd units (~/.config/systemd/user/webgpt-*)
| Unit | ExecStart | Trạng thái |
|---|---|---|
| webgpt-gateway.service | `.venv/bin/python -m gpt.debug api-server ... --port 18000 --transport browser ...` | OK — module + trace-file `/home/light/Downloads/webgpt/logs/trace.jsonl` tồn tại; env `WEBGPT_TOOL_PROTOCOL=soft` |
| webgpt-watchdog.{service,timer} | `scripts/webgpt-watchdog.sh` | OK, bash -n sạch, timer */5 phút |
| webgpt-auto-review.{service,timer} | `scripts/auto_review.sh` | OK, bash -n sạch, timer 04:17 hằng ngày |

Không unit nào trỏ path đã đổi tên/chết. Unit `webgpt-soak.*` mà SOAK_TEST_PLAN.md mô tả **chưa tồn tại** (khớp trạng thái SOAK "chờ duyệt chạy thật").

## 5. Docs guides — spot-check path
Kiểm tra ~20 tham chiếu trong docs/guides/*.md + automation/*.md + README/GUIDE:
- OK hết: `scripts/{auto_review,webgpt-watchdog}.sh`, `scripts/{soak_runner,e2e_project_benchmark,practical_cli_bench,pick_ctf_challenge}.py`, `docs/reports/auto-review/`, `docs/reports/soak/`, spec superpowers 2026-08-22, `~/Downloads/webgpt/{accounts/accounts.json,logs/watchdog.log,profiles/}`, `/tmp/webgpt-watchdog-fail-count`.
- MISS: `~/.config/systemd/user/webgpt-soak.{service,timer}` (chưa cài).
- STATE.md Tick 34 trỏ `docs/reports/med-ctf-restart-2026-08-25.md` — chưa tồn tại (hợp lý: wave MED chết trắng vì crash máy 09:02).

## 6. Phân loại 97 file untracked — ĐÁNH DẤU CHỈ, CHƯA XOÁ

### Nhóm A — rác/nháp dọn được (đề xuất)
| File | Lý do |
|---|---|
| `docs/reports/2026-08-24-repo-analysis.html` | bản render HTML trùng nội dung `.md` cùng tên (22KB vs 14KB, cùng giờ tạo) |
| `solve_fast.py`, `solve_v2_fast.py` (root repo) | solver one-off timing side-channel cho 2 bài CTF cụ thể (Whale/Misc4) — không thuộc hạ tầng gateway, nên chuyển scratch/ hoặc xoá |
| `scripts/.ctf_used_challenges.json` | runtime state chống-lặp bài CTF — nên thêm vào .gitignore thay vì track |

### Nhóm B — thư mục output tự sinh (candidate .gitignore)
`docs/reports/auto-review/` (auto_review.sh tự quay, KEEP=50), `docs/reports/soak/`.

### Nhóm C — báo cáo verify/probe 2026-08-24 (GIỮ — bằng chứng verify loop)
live-cli-verify round1→7d (10 file), live-sse-probe ×2, soft-framing/sentinel-sdk/inpage-fetch/header-diff/cf-clearance/latency-budget probes, sandbox ×2, prompt-lab ×2, custom-gpt ×2, meta-gpt-tool-format, api-parity-audit, t5/t6-ctf, ctf-picker, ctf-candidates.json, agent-handoff, master-roadmap, SESSION_LOG_20260822, ACCEPTANCE_REPORT, GATEWAY_CERTIFICATION, OPTIMIZATION_ANALYSIS.

### Nhóm D — code/docs thật cần commit (không phải rác)
`gpt/auth/accounts.py`, `gpt/transport/{account_health,challenge,failover,multi_account}.py`, 5 script QA mới + REVIEW_GATE.md + webgpt-{claude,watchdog}.sh + auto_review.sh, ~30 tests/test_*.py, `docs/specs/fastpath-transport-spec.md`, 4 guide mới, toàn bộ `docs/automation/`, `.env.example`.

## Kết luận
- Status lệch: **4 nhóm** (P2-server stale-done, A3 duplicate/trailing code, E2E-BENCH duplicate row, GATEWAY-CFG drift both→soft) + 2 ghi chú stale nhỏ (W-A1A4A2 parenthetical, R4-DOUBLING vẫn đúng todo).
- Script chết như tool: **3** (run_claude_ctf_task, run_claude_misc_task, solve_ctf_with_files) — auto-fire live turn, cần thêm main-guard hoặc đưa ra repo.
- Launcher/systemd/launcher-pyproject: sạch, đúng DECISIONS.
- Guides: 1 miss (soak units chưa cài — đúng giai đoạn).
- Dọn được ngay: 3 file nhóm A + 2 thư mục nhóm B (gitignore).
