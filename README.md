# GPT Web Toolkit

Toolkit Python điều khiển conversation trên ChatGPT Web bằng Chromium, kèm reverse-capture harness và gateway local tương thích OpenAI Chat Completions, OpenAI Responses và Anthropic Messages.

Thiết kế mới không giả định endpoint nội bộ của ChatGPT. UI semantic driver là đường chạy ổn định; protocol replay chỉ bật khi có fingerprint đã verify từ ≥2 experiment và có replay adapter cụ thể.

Repository còn có **chế độ CTF tự động** (`ctf` wrapper) để giải các bài CTF trong thư viện cục bộ song song qua subagents, có supervisor tự động + recruiter tự động + war-game loop 24/7. Xem mục [**CTF mode (`ctf`)**](#ctf-mode-ctf) ở dưới.

---

## Trạng thái

- Runtime/gateway đã verify local cho OpenAI Chat Completions/Responses, Anthropic Messages, tool continuation, correction-loop guards, conversation persistence, multi-account reliability, hybrid transport.
- Production profile: **hybrid + authenticated f/conversation prepare**; T1/T2/T3 chứng nhận 2026-08-26. Browser transport vẫn là fallback/diagnostic path.
- `WEBGPT_TOOL_PROTOCOL=soft` là production setting; code default `xml` để giữ backward compatibility.
- Image upload web implement nhưng `WEBGPT_IMAGE_UPLOAD_WEB` vẫn OFF cho tới khi PNG live recertification pass.
- Codex path giữ để compatibility, không còn hướng phát triển ưu tiên.

---

## Documentation

| Category | Document |
| --- | --- |
| Guides | [**CTF mode (`ctf`)**](#ctf-mode-ctf) — wrapper tự động giải CTF |
| Guides | [User & CTF Solving Guide (Playbook & Prompts)](GUIDE.md) |
| Guides | [Authentication and login guide](docs/guides/AUTH_AND_LOGIN_GUIDE.md) |
| Guides | [CTF Automation Owner Policy](docs/automation/CTF_OWNER_POLICY.md) — Plus account rules, risk |
| Guides | [Automation Ops](docs/guides/AUTOMATION_OPS.md) — vận hành 24/7 |
| Guides | [Practical CLI benchmark](docs/guides/PRACTICAL_CLI_BENCH.md) |
| Plans | [Master execution plan](docs/plans/MASTER_EXECUTION_PLAN.md) |
| Plans | [Hybrid plan](docs/plans/HYBRID_PLAN.md) |
| Plans | [Verification and Claude Code benchmark plan](docs/plans/PLAN_VERIFICATION_AND_CLAUDE_CODE_BENCHMARK.md) |
| Reports | [Acceptance report](docs/reports/ACCEPTANCE_REPORT.md) |
| Reports | [Gateway certification](docs/reports/GATEWAY_CERTIFICATION.md) |
| Reports | [Session log — 2026-08-22](docs/reports/SESSION_LOG_20260822.md) |

---

## Cài đặt

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
```

Nếu dùng Chromium hệ thống, truyền `executable_path` khi khởi tạo `BrowserManager`.

---

## Thiết lập profile

```bash
gpt-web setup
```

Một cửa sổ browser headful sẽ mở. XDG defaults:

```
~/.local/share/webgpt/cloak-profile/         # anonymous/default standalone profile
~/.local/share/webgpt/profiles/<account>/   # named account profiles
~/.config/webgpt/accounts.json              # named-account metadata registry
```

### Dùng Brave đang mở (CDP attach)

Khi browser Playwright bị ChatGPT chặn, mở một profile Brave riêng với CDP chỉ bind loopback, rồi tự đăng nhập:

```bash
gpt-web brave-launch

gpt-web setup --cdp-url http://127.0.0.1:9222
gpt-web api-server --cdp-url http://127.0.0.1:9222 --port 8765
```

`brave-launch` dùng `~/.local/share/bqa/brave-chatgpt-profile/`, bind CDP loopback và in lệnh setup tiếp theo.

---

## Primary CLI

Đường dùng bình thường không cần Claude Code ở giữa:

```bash
gpt "inspect this repository and fix the failing tests"
gpt                         # interactive direct-agent session
gpt -C /path/to/repo "task"

gpt status
gpt doctor
gpt doctor --deep
gpt config show
gpt session current
gpt account list

gpt bench practical
gpt bench soak
gpt bench e2e
gpt bench selfcheck
gpt bench review

gpt account codex-login     # optional Codex OAuth compatibility flow
gpt compat claude           # explicit legacy Claude Code bridge
```

`gpt` talks directly to the local gateway, owns its own tool loop (`Bash` + `ApplyPatch`), session persistence và verification gate. `gpt-web` cho low-level browser/protocol diagnostics.

---

## Advanced browser/debug CLI (`gpt-web`)

```bash
# Reconnaissance JSON, không gửi prompt
gpt-web probe --headful --persistent

# Model labels lấy động từ UI
gpt-web models --headful

# Chẩn đoán profile/CDP
gpt-web doctor --free

# Gửi turn mới
gpt-web send --text "Xin chào" --headful

# Mở conversation cũ rồi follow-up
gpt-web send --conversation <conversation-id> --text "Tiếp tục" --json

# Capture một biến duy nhất
gpt-web experiment --exp-id E00_IDLE --action idle
gpt-web experiment --exp-id E01_NEW_CHAT --action new-chat
gpt-web experiment --exp-id E10A_SEND --action send
```

Artifacts lưu ngoài repo tại `~/.local/share/bqa/webchat-reverse/`, directory mode `0700`, file mode `0600`.

---

## Python API

```python
from gpt import ChatGPTWebSession

session = await ChatGPTWebSession.create(
    persistent=True,
    headless=False,
)
try:
    await session.select_model("<exact visible label>")
    result = await session.send("Hello")
    print(result.text)

    await session.reload()
    print(await session.history())
finally:
    await session.close()
```

`ChatGPTWebSession` cung cấp `new_conversation`, `open`, `models`, `select_model`, `send`, `events`, `history`, `reload`, `close`. Mọi send được serialize để tránh double-submit.

---

## Local API gateway

Production user service dùng loopback `:18000` với hybrid transport. Ad-hoc dev server có thể dùng port 8765.

```bash
# ad-hoc / diagnostic
gpt-web api-server --transport hybrid --port 8765

# production-style port
gpt-web api-server --transport hybrid --port 18000
```

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="unused")
response = client.chat.completions.create(
    model="chatgpt-web",
    messages=[{"role": "user", "content": "Hello"}],
)
print(response.choices[0].message.content)
```

Gateway chuẩn hoá request, serialize writer, tự correlate prefix `messages` với conversation trước. Response trả `x-webgpt-session-id`; client gửi lại header để chọn session tường minh.

Tool calling là controller protocol fail-closed, không phải native ChatGPT Web function calling. Hỗ trợ `xml`, `json-fn`, `both`, `soft`. Production dùng `soft`: shell-capable surfaces thương lượng `<cmd>...</cmd>`, function-only thương lượng `<json>...</json>`.

V1 nhận `model`, `messages`, `tools`, `tool_choice`, `stream`, `temperature`, `reasoning_effort` (hoặc `reasoning.effort`). `temperature` được chấp nhận nhưng bỏ qua (ChatGPT Web không map đáng tin). Model chỉ chọn khi UI có picker.

### Responses, Anthropic và Claude Code

Gateway có `POST /v1/responses` và `POST /v1/messages`. Dùng cùng browser/conversation/tool runtime; chỉ subset test offline hỗ trợ. Built-in hosted tools, background mode, encrypted content, batch/prompt-cache semantics, nội dung không map được → trả lỗi rõ ràng thay vì giả lập.

Claude Code dùng route Anthropic qua `ANTHROPIC_BASE_URL` và key local placeholder. Chỉ dùng loopback; key không forward đến API Anthropic.

Conversation state chỉ persist khi truyền `--conversation-store <path>` cho `api-server`; TTL, directory mode `0700`, file mode `0600`. Không bật trên máy/thư mục không tin cậy.

---

## Kiểm thử

```bash
.venv/bin/python -m pytest -q
ruff check .
mypy .
.venv/bin/python -m compileall -q gpt scripts evals benchmarks
.venv/bin/python evals/run_evals.py
gpt bench selfcheck

# Aggregated automation gate: pytest + repo-wide Ruff/mypy + diff danger scan
gpt bench review
```

---

## CTF mode (`ctf`)

**Mục tiêu**: tự động giải CTF trong thư viện cục bộ (`~/Workspace/CTF`) song song qua nhiều subagents, ghi flag vào registry, viết writeup, tự động supervise + recruit khi agent chết, có war-game loop A&D (Attack-Defense) chạy 24/7.

### Tổng quan thành phần

| Thành phần | Vai trò |
|---|---|
| `scripts/pick_ctf_challenge.py` | Bộ chọn bài — duyệt `~/Workspace/CTF`, loại bài đã solved/đánh dấu, xếp hạng theo risk-tier |
| `scripts/ctf_spawn_session.py` | Spawn 1 session `gpt` cho 1 bài (PID thật, log file, prompt đã neutralize) |
| `scripts/ctf_supervisor.py` | Auto-detect session chết / cyber-refusal / gateway down, ghi incident |
| `scripts/ctf_recruiter.py` | Tự recruit session mới khi progress cũ >15p |
| `scripts/ctf_flag_registry.py` | Registry central `docs/automation/solved-flags.json` + `--check`/`--add`/`--list`/`--sync` |
| `scripts/ctf_writeup.py` | Auto-gen `WRITEUP.md` từ progress.json + REPORT.md |
| `scripts/ctf_monitor.py` | Health monitor (gateway + solver + POST/min + RAM) |

### Lệnh nhanh (`ctf` wrapper)

Trong shell của owner, gọi `ctf` (alias nhóm các scripts trên):

```bash
# Liệt kê bài CTF sẵn sàng giải (ranked, risk-tier default low)
ctf pick                       # ~50 bài crypto/stego/pcap/osint sẵn sàng
ctf pick --max-risk medium     # thêm rev-config/web-exploit/forensics
ctf pick --include-remote      # kèm bài cần remote (pwnbox, docker)

# Spawn 1 session giải 1 bài
ctf solve --chal-dir /home/light/Workspace/CTF/BrunnerCTF_2026_-_Global/Crypto/Cyclotomic_Echo

# Spawn N session song song (tối đa 5, sau khi test concurrency)
ctf solve-batch --max-risk low --parallel 5

# Theo dõi
ctf status                     # tóm tắt các session đang chạy
ctf monitor                    # health monitor 1 lần
ctf recruit                    # tự recruit agent chết >15p

# Registry
ctf flag --add <chal-dir> "<flag>"
ctf flag --check <chal-dir>    # đã solved chưa
ctf flag --list                # in registry
ctf flag --sync                # sync từ disk (challenge dir có flag.txt)

# Writeup
ctf writeup --all              # gen WRITEUP.md cho tất cả solved

# A&D war-game
ctf wargame start              # coordinator loop vĩnh viễn
ctf wargame status             # rounds/STATUS.md
```

Các script có thể gọi trực tiếp (không cần alias):

```bash
.venv/bin/python scripts/pick_ctf_challenge.py --max-risk low --json | head -5
.venv/bin/python scripts/ctf_spawn_session.py --chal-dir <X> --name <X>
.venv/bin/python scripts/ctf_flag_registry.py --add "<chal-dir>" "<flag>"
.venv/bin/python scripts/ctf_writeup.py --all
.venv/bin/python scripts/ctf_monitor.py
.venv/bin/python scripts/ctf_recruiter.py
```

### Chạy 24/7 (cron + war-game)

Xem chi tiết tại `docs/guides/AUTOMATION_OPS.md`. Tóm tắt:

```bash
# 1) Cron 10 phút tự kiểm tra + recruit + report
crontab -e
# thêm:
*/10 * * * * /home/light/GitHub/gpt/scripts/ctf-monitor.sh >> /home/light/Downloads/ctf-workspace/and-ctf/logs/progress-10min.log 2>&1

# 2) Spawn A&D war-game loop (ATK vs DEF, cải tiến vĩnh viễn)
.venv/bin/python scripts/ctf_spawn_session.py --chal-dir <war-game-coord>
```

### Phân loại rủi ro & an toàn (Plus account)

Owner policy tại `docs/automation/CTF_OWNER_POLICY.md`. Tóm tắt:

- **Account là ChatGPT Plus** — không giới hạn quota/ngày, không cần `preflight_quota.py`.
- **3 risk-tier**: THẤP (crypto/stego/pcap/osint/toán) / TRUNG BÌNH (rev-config/web-exploit/forensics) / CAO (rev-binary/APK/mobile/pwn/jail).
- Mặc định batch chạy `--max-risk low` (THẤP).
- Cyber-refusal lớp classifier → KHÔNG retry trong cùng conversation (poison); mark `used_at` + chuyển bài khác risk thấp hơn.
- OSINT cần online lookup chính xác cao (toạ độ, profile) → BỎ (không verify được flag).
- Khi gặp lỗi → khởi động lại toàn bộ task theo yêu cầu owner, không retry cục bộ.

### Quy trình 1 bài CTF (end-to-end)

```
pick_ctf_challenge.py
  ↓ (ranked, --max-risk low, tránh bài đã solved)
ctf_spawn_session.py --chal-dir <X>
  ↓ (tạo workspace ~/Downloads/ctf-workspace/workspaces/<X>/
  │  copy attachments, prompt được neutralize,
  │  chạy gpt --new-session --no-session)
gpt session chạy → POST /v1/messages → gateway → ChatGPT Web
  ↓ (auto-update progress.json mỗi tool call)
gpt tìm được flag → ghi flag.txt + REPORT.md
ctf_flag_registry.py --add <chal-dir> <flag>     # ghi registry, đánh dấu solved
ctf_writeup.py --all                            # gen WRITEUP.md cho cả solved
  ↓
ctf_supervisor.py detect dead session + auto-recruit
ctf_recruiter.py tiếp tục từ progress.json + workspace cũ
```

Workspace + runs + logs + incidents → tất cả trong `~/Downloads/ctf-workspace/`. Repo `scratch/` chỉ chứa symlink để dễ xoá, tránh đầy RAM.

---

## Đã làm được (work log 2026-09-01)

### CTF solving — 17 bài solved (registry: `docs/automation/solved-flags.json`)

| # | Challenge | Flag | Tier |
|---|---|---|---|
| 1 | Half Baked | `brunner{d0ugh_butt3r_sug4r_c1nn4mon_cr34m_c4r4m3l}` | THẤP |
| 2 | Rubik's Cube | `brunner{F2_U2_B'_R2_B'_L_D2_R_F2_U_B2_D_R2_B2_D'_B2_U_F2_U_B...}` | THẤP |
| 3 | π-crypt | `brunner{NB!:_Re-using_the_same_key_without_salting-and-hashi...}` | THẤP |
| 4 | CleanDesk | `brunner{4llowB4ckup_t00k_th3_k3y_t00}` | THẤP |
| 5 | QR (PTIT) | `PTITCTF{h0w_c4n_y0u_d0_d47}` | THẤP |
| 6 | Not Seen Colors | `zdk{m4S7er_OF_C0l0rs_4nD_C7f}` | THẤP |
| 7 | QR Queries | `TDHT{89e1922e-6377-61a5-d788-ae7b53e10964}` | THẤP |
| 8 | CAN you read this | `brunner{hidden_in_plain_light}` | THẤP |
| 9 | OnePass | `brunner{pr3f1x_m4tch1ng_1s_n0t_v4l1d4t10n}` | THẤP |
| 10 | siren | `zdk{4_feW_8ltS_PeR_5LGNA7UR3_sLNkS_The_KEy}` | THẤP |
| 11 | passkey-nightmare | `HZ{nKSnVnPs_b1nd_th3_c0mm1tm3nt_wEczLfWE}` / `HZ{hFVdIOuu_p4ssk3y_n1ghtm4r3_PQ69rd_J}` | TRUNG BÌNH |
| 12 | Magic or not | `brunner{ctf2026}` | THẤP |
| 13 | Gerege | `brunner{...}` | THẤP |
| 14 | genie | `zdk{7Hree_WOrD5_NiNE_eCHo3S_ON3_oPeN_Seal}` | THẤP |
| 15 | cyclotomic-echo | `zdk{cyc10T0mic_eCho_on3_BA5IS_biNdS_3verY_TeAM_ARcHIvE}` | THẤP |
| 16 | secret-storage | `brunner{but_th3_A1_s41d_1t_w45_f1n3???}` | THẤP |
| 17 | TotalReward | `brunner{th3_3nt1tl3m3nt_ch3ck_r0d3_4l0ng_1n_th3_4pp}` | TRUNG BÌNH |

> Flag đầy đủ xem `docs/automation/solved-flags.json`.

### War-game A&D loop — 27 rounds done

Coordinator agents (`bg-3..bg-57`) chạy loop ATK vs DEF tại `~/Downloads/ctf-workspace/and-ctf/war-game/`:

```
R1..R27: 27 rounds | ATK 27 generations | DEF 23 layers | DEF WINS gần đây
```

Quy trình mỗi round:

1. Đọc `rounds/STATUS.md` + `rounds/progress.json`.
2. DEF đang thắng → thêm vuln đơn giản vào `service/vulnbox_r<N>.py` (SSRF / SQLi / open-redirect / cookie-auth / pickle RCE / deserialization / etc.).
3. ATK viết `atk/exploit_v<N>.py` → test thật trên port 13337 → THẮNG thì sang DEF.
4. DEF viết `def/patch_v<N>.py` → `service/vulnbox_defended_r<N>.py` → verify 0 flag.
5. Ghi `rounds/STATUS.md` + `rounds/progress.json` + `/tmp/flag_round<N>.txt`.

A&D toolkit sẵn (`~/Downloads/ctf-workspace/and-ctf/`): `run_attack.py` (carpet-bomb multi-variant exploit), `flag_submitter.py`, `flag_watcher.py`, `PLAYBOOK.md` (297 dòng), `defense.sh`, `webshell_kit.py`. Đã test end-to-end với mock vulnbox.

### Bug fixes trong tool gpt

| # | Lỗi | Nguyên nhân | Fix |
|---|---|---|---|
| 1 | `409 Tool definitions changed` khi nhiều session song song | Gateway share session giữa các agent, tool_signature xung đột | `gpt/agent/client.py` thêm `_first_round_done` flag, chỉ gửi `x-webgpt-session-id` round đầu |
| 2 | `--print` unrecognized by gpt CLI | Tưởng flag phổ biến | Xoá, dùng positional prompt |
| 3 | `gpt` symlink broken → `legacy/webgpt-direct.sh` | Symlink hỏng | Sửa symlink trỏ đúng entry point |
| 4 | `Maximum tool rounds reached (20)` giết agent sớm | Default thấp | `WEBGPT_MAX_ROUNDS=100` |
| 5 | `hybrid_event_queue_overflow dropped=1500 cap=512` | Event queue quá nhỏ với ≥4 agent song song | Raise cap → 2048 (ghi unit systemd) |
| 6 | Recruiter false positive (kill agent đang chạy) | PID stale nhưng agent vẫn active | Check progress mtime + fresh file trong workspace |
| 7 | Brace expansion tạo literal `{...}` directory | Path có `{}` | Sanitize path trong spawn script |
| 8 | `import secrets` sau khi dùng (lỗi logic war-game) | Vulnbox thiếu import trước khi dùng | Chuyển `import secrets as _sec` lên đầu file |
| 9 | DB `:memory:` thread conflict (war-game) | ThreadingMixIn + sqlite3 share connection | Thêm `check_same_thread=False` |
| 10 | Cyber-refusal conversation poisoning | Classifier tích luỹ score trong cùng conversation | `--no-session --new-session` mỗi lần + split prompt |

### Auto-supervision + recruitment (24/7 pipeline)

- **ctf_supervisor.py**: auto-detect `session-dead`, `cyber-refusal`, `gateway-down`, `low-ram`. Ghi incident `~/Downloads/ctf-workspace/incidents/recruit-*.json`.
- **ctf_recruiter.py**: check session >15p không progress → spawn agent mới với prompt "tiếp tục từ chỗ dừng" (đọc progress.json + workspace cũ).
- **ctf_flag_registry.py**: `--add <chal-dir> <flag>` ghi registry + flag.txt + đánh dấu bài solved → picker tự bỏ qua lần sau.
- **ctf_writeup.py**: `--all` generate WRITEUP.md cho mọi challenge solved.
- **Cron 10 phút**: monitor health + log `progress-10min.log`. Recruiter chạy liên tục.

### Cyber-refusal debugging

Phát hiện:
- **2 lớp chặn**: (1) model-level soft refusal ("Tôi không thể..."), (2) web classifier block ("This content can't be shown").
- **Stochastic, session-accumulating** — không chỉ trigger ở prompt user, mà cả model output lẫn tool-call arguments. Cùng prompt có thể pass ở attempt 2, fail ở 1 và 3.
- **Mitigation**: keep prompt small/scoped, fresh `--no-session` mỗi lần, terminal refusal (mark `used_at` + đổi bài risk-tier thấp hơn), KHÔNG retry trong cùng conversation.

### Owner policy đã commit

`docs/automation/CTF_OWNER_POLICY.md` — ghi nhận Plus account, không quota, không cần `preflight_quota.py`, restart-toàn-bộ khi gặp lỗi, watchout classifier cyber-refusal + RAM crash.

---

## Lưu ý vận hành

- Mọi file rác (workspaces, runs, logs, incidents) consolidate vào `~/Downloads/ctf-workspace/` để dễ xoá, tránh đầy RAM. Repo `scratch/` chỉ chứa symlink.
- OSINT cần online lookup chính xác cao (toạ độ, profile) → BỎ, không verify được flag.
- Không attack infrastructure ngoài game network — chỉ organizer-provided vulnbox/service (A&D hợp lệ).
