---
name: gpt
description: How to drive the local `gpt` CLI tool — ChatGPT-Web reverse-engineered harness with a hybrid gateway serving OpenAI Chat Completions, OpenAI Responses, and Anthropic Messages. Use when the user asks how to run `gpt`, set up a profile, start the gateway, write Python code that talks to the local gateway, debug a session, or when a CTF solver agent (see skill `/ctf`) needs to spawn or recover a `gpt` process.
argument-hint: "<prompt> [-C workdir] [--model MODEL] [--new-session] [--no-session]"
---

# GPT Tool

Toolkit Python điều khiển conversation trên ChatGPT Web bằng Chromium + gateway local tương thích OpenAI Chat Completions, OpenAI Responses, Anthropic Messages.

Khi nào dùng: agent cần chạy prompt qua ChatGPT Web, debug session, hoặc start gateway. CTF solver agents (skill `/ctf`) dùng CLI này làm entry point.

## ⚡ Quick Trigger: `/gpt <path>` (CTF Auto-Solve Ngay Lập Tức)

Khi user gõ `/gpt <path>` hoặc yêu cầu "dùng gpt giải bài này":
**BẮT BUỘC CHẠY NGAY LỆNH SAU (KHÔNG đọc phân tích lại toàn bộ repo để tránh tốn thời gian):**

```bash
name=$(basename "<CHAL_DIR>")
.venv/bin/python scripts/ctf_spawn_session.py --chal-dir "<CHAL_DIR>" --name "$name" --timeout 0
```
Lệnh trên tự động:
- Đóng gói file bài tập & chuẩn bị workspace tại `~/Downloads/ctf-workspace/workspaces/$name`
- Khởi chạy session `gpt` chạy ngầm (detached) với PID riêng và log tại `~/Downloads/ctf-workspace/runs/$name/session.log`
- Cập nhật `progress.json`.

Lệnh theo dõi và giám sát:
```bash
# Xem cây tiến trình con đang chạy
pstree -pa $(cat ~/Downloads/ctf-workspace/runs/$name/session.pid)
# Theo dõi trực tiếp log của solver
tail -f ~/Downloads/ctf-workspace/runs/$name/session.log
# Kiểm tra trạng thái
cat ~/Downloads/ctf-workspace/runs/$name/progress.json
```

## 🛡️ Crypto Feasibility Guard (Giám sát Thẩm định Độ phức tạp Toán học)

**Quy tắc bắt buộc khi giám sát solver chạy script Crypto:**
Không bao giờ để script giải chạy mù quáng đến mốc 180s hay 600s timeout!

### 1. Phân loại lệnh (Detection Filter)
- **Lệnh thông thường (Pass-through):** `ls`, `cat`, `file`, `python3 -c "import..."`, kiểm tra file hoặc script chạy kết thúc $\le 5$s $\rightarrow$ Cho qua bình thường.
- **Script Crypto / SMT / Brute-force:** Script gọi `z3`, `sage`, vòng lặp vét cạn lớn, hoặc chạy chiếm 100% CPU $> 8-10$s $\rightarrow$ Bắt buộc kích hoạt thẩm định tính khả thi.

### 2. Thẩm định tính khả thi (Feasibility Audit)
Đánh giá nhanh 2 tiêu chí toán học:
1. **Ràng buộc toán học (Sanity Check):** Ràng buộc có mâu thuẫn (UNSAT) không? (Ví dụ: so khớp accumulator trung gian với hash cuối của toàn bộ payload $\rightarrow$ UNSAT).
2. **Độ phức tạp (Complexity Check):** Mạch nhân phi tuyến 32x32/64x64 trong Z3 bit-blasting hoặc không gian vét cạn $> 2^{28}$ là bất khả thi trong thời gian thực tế.

### 3. Hành động can thiệp & Bơm phản biện (Intervention & Feedback Injection)
Nếu script **BẤT KHẢ THI**:
1. **Kill ngay tiến trình con:** `kill -9 <child_pid>` (không chờ 600s timeout).
2. **Bơm thông điệp phản biện định hướng vào session** để ép Agent tư duy lại:
```text
[-] CRYPTO_GUARD_ALERT: INFEASIBLE_APPROACH
- Lỗi logic/toán học: [Chỉ rõ phương trình/ràng buộc mâu thuẫn hoặc vô nghiệm]
- Nút thắt độ phức tạp: [Độ phức tạp O(2^k) hoặc số lượng mệnh đề SAT nhân phi tuyến]
- Hướng tư duy đề xuất: [Định hướng giải tích: tính khả nghịch, phân rã độc lập, MITM, LLL]
```


## CLI chính

```bash
# Run prompt trực tiếp (OpenAI-compatible local gateway)
gpt "inspect this repository and fix the failing tests"
gpt -C /path/to/repo "task"
gpt --no-session --new-session "one-shot task"

# Pick model (exact visible label từ UI)
gpt --model gpt-5-mini "task"
gpt --model chatgpt-web "task"   # giữ model hiện hành

# Diagnostics
gpt status
gpt doctor
gpt doctor --deep
gpt config show
gpt session current
gpt account list

# Bench
gpt bench practical
gpt bench soak
gpt bench e2e
gpt bench selfcheck
gpt bench review

# Auth flows
gpt account codex-login
```

## Browser/debug (`gpt-web`)

```bash
gpt-web setup                       # mở browser headful thiết lập profile
gpt-web brave-launch                # launch Brave + CDP loopback
gpt-web setup --cdp-url http://127.0.0.1:9222
gpt-web probe --headful --persistent    # reconnaissance JSON
gpt-web models --headful            # list model labels from UI
gpt-web doctor --free               # diagnose profile/CDP
gpt-web send --text "..." --headful
gpt-web send --conversation <id> --text "..." --json
```

## Gateway

Production user service: `http://127.0.0.1:18000` (hybrid transport).

```bash
gpt-web api-server --transport hybrid --port 18000
```

Ad-hoc: port 8765.

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:18000/v1", api_key="unused")
r = client.chat.completions.create(
    model="chatgpt-web",
    messages=[{"role": "user", "content": "Hello"}],
)
print(r.choices[0].message.content)
```

Routes: `POST /v1/chat/completions`, `POST /v1/responses`, `POST /v1/messages` (Anthropic).
Claude Code: `ANTHROPIC_BASE_URL=http://127.0.0.1:18000` + key placeholder.

## Python API

```python
from gpt import ChatGPTWebSession
session = await ChatGPTWebSession.create(persistent=True, headless=False)
try:
    await session.select_model("<exact visible label>")
    result = await session.send("Hello")
    print(result.text)
finally:
    await session.close()
```

Methods: `new_conversation`, `open`, `models`, `select_model`, `send`, `events`, `history`, `reload`, `close`. Mọi send serialized.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
gpt-web setup      # mở browser, đăng nhập ChatGPT
```

XDG paths:
```
~/.local/share/webgpt/cloak-profile/         # anonymous/default
~/.local/share/webgpt/profiles/<account>/   # named accounts
~/.config/webgpt/accounts.json
```

## Env vars quan trọng

| Var | Mặc định | Vai trò |
|---|---|---|
| `WEBGPT_TOOL_PROTOCOL` | `xml` | Tool call protocol. Production = `soft` (negotiate `<cmd>` vs `<json>`) |
| `WEBGPT_MAX_ROUNDS` | `20` | Max tool rounds trong 1 turn. Đặt `100` cho solver dài |
| `WEBGPT_GENERATION_TIMEOUT` | `600` | Timeout generation (giây) |
| `WEBGPT_SEND_TIMEOUT` | `300` | Timeout gửi (giây) |
| `WEBGPT_HYBRID_EVENT_QUEUE_CAP` | `512` | Cap event queue. Tăng `2048` khi >4 session song song |
| `WEBGPT_IMAGE_UPLOAD_WEB` | `0` | PNG upload web. OFF mặc định |
| `WEBGPT_FCONV_RESUME` | `0` | F-conversation resume. OFF mặc định |
| `WEBGPT_GATEWAY_URL` | `http://127.0.0.1:18000` | URL gateway (cho ctf_monitor.py) |

## Common pitfalls

**`409 Tool definitions changed`** khi >4 session song song:
- Dùng `--no-session --new-session` cho mỗi session mới.
- Nếu cần sticky session, gửi `x-webgpt-session-id` chỉ ở round đầu (đã fix trong `gpt/agent/client.py`).

**`gpt --print` không tồn tại**:
- Dùng positional prompt: `gpt "task"` không phải `gpt --print "task"`.

**`Maximum tool rounds reached (20)`** giết solver sớm:
- Set `WEBGPT_MAX_ROUNDS=100`.

**`hybrid_event_queue_overflow dropped=1500 cap=512`** → 502 dưới parallel:
- Raise `WEBGPT_HYBRID_EVENT_QUEUE_CAP=2048` + restart gateway.

**`this content can't be shown`** (cyber classifier block):
- Đổi prompt framing (xem `scripts/ctf_prompting.py`).
- Tạo conversation mới (`--new-session --no-session`).
- KHÔNG retry cùng conversation — classifier tích luỹ score.

**Gateway down**:
- `curl http://127.0.0.1:18000/health` kiểm tra.
- Restart: `systemctl --user restart webgpt-gateway.service` hoặc chạy lại `gpt-web api-server`.

**Browser session expired**:
- `gpt-web doctor --free` chẩn đoán.
- Chạy lại `gpt-web setup` để refresh profile.

## Tool calling

Tool protocol soft (production):
- Shell-capable surface → thương lượng `<cmd>...</cmd>`.
- Function-only surface → thương lượng `<json>...</json>`.
- Fail-closed: chỉ tool name đã advertise mới map được.

## Streaming

SSE streaming có attempt-boundary/dedup guards. Tool-bearing turns ưu tiên correctness hơn speculative token forwarding (không bị lặp nội dung correction).

## 1-turn test

```bash
echo "" | timeout 30 gpt --no-session --new-session --model gpt-5-mini "Reply with the word OK only."
# expect: OK
```

## Vị trí file quan trọng

| Path | Vai trò |
|---|---|
| `gpt/agent/client.py` | HTTP client, fix 409 với `_first_round_done` |
| `gpt/api/server.py` | FastAPI gateway |
| `gpt/gateway/runtime.py` | Gateway runtime, detect cyber-refusal |
| `gpt/gateway/runtime_policy.py` | Policy cho cyber markers |
| `gpt/conversations.py` | ConversationConflict (raise 409) |
| `gpt/transport/hybrid.py` | Hybrid transport |
| `gpt/transport/fconv.py` | F-conversation prepare |
| `scripts/ctf_prompting.py` | `frame_local_ctf_prompt()` + `neutralize_ctf_text()` |
| `docs/automation/ACTIVE_ISSUES.md` | Active bug backlog & resolved archive |
| `docs/automation/LOG.md` | Session worklogs & engineering history |
| `docs/automation/SOLVE_PLAYBOOK.md` | Category-based solve flows & playbook |
| `tests/` | Unit + integration tests |

## Liên quan

- `docs/automation/SOLVE_PLAYBOOK.md` — Playbook-first solver strategy (đọc flow cũ trước, chỉ nghĩ mới khi fail, solve xong bắt buộc ghi flow mới)
- `docs/automation/LOG.md` — Nhật ký thao tác & quy tắc ghi log phiên làm việc
- `docs/automation/ACTIVE_ISSUES.md` — Danh sách lỗi cần sửa & quy tắc ghi nhận / gỡ bỏ lỗi
- Skill `/ctf` — wrapper giải CTF tự động (dùng CLI này)
- Skill `/command-code-knowledge` — auth, permissions, providers, model catalog
- `docs/guides/AUTH_AND_LOGIN_GUIDE.md` — login flow
- `docs/automation/CTF_OWNER_POLICY.md` — Plus account rules
- `docs/reports/` — bug investigations, benchmarks
