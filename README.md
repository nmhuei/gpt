# GPT Web Toolkit

Toolkit Python điều khiển một conversation trên ChatGPT Web bằng Chromium, kèm reverse-capture harness và gateway local tương thích một phần với OpenAI Chat Completions, OpenAI Responses và Anthropic Messages.

Thiết kế mới không giả định endpoint nội bộ của ChatGPT. UI semantic driver là đường chạy ổn định; protocol replay chỉ được bật khi có fingerprint đã được xác minh từ ít nhất hai experiment và có replay adapter cụ thể.

## Trạng thái

- Hoạt động offline/tested: redaction, artifact permissions, trace normalization/diff, stream-revision contract, state machine, protocol→UI fallback, Chat Completions/Responses/Messages translation, request normalization, tool continuation và conversation persistence opt-in.
- Cần live browser để xác minh theo account/UI hiện tại: login persistence, selectors, model picker, send/stream/reload.
- Chưa bật mặc định: protocol replay. Repo chưa có evidence ledger đủ để hardcode một request contract an toàn.
- Ngoài V1: upload, voice, image generation, Deep Research, GPTs và multi-agent scheduler.

Đây là chủ ý an toàn của [`PLAN_REV.md`](docs/plans/PLAN_REV.md): nhận được text nhưng conversation không persist không được xem là protocol replay thành công.

## Documentation

| Category | Document |
| --- | --- |
| Plans | [Hybrid plan](docs/plans/HYBRID_PLAN.md) |
| Plans | [Master execution plan](docs/plans/MASTER_EXECUTION_PLAN.md) |
| Plans | [Improvement roadmap](docs/plans/PLAN_IMPROVEMENT_ROADMAP.md) |
| Plans | [Plan revision](docs/plans/PLAN_REV.md) |
| Plans | [Verification and Claude Code benchmark plan](docs/plans/PLAN_VERIFICATION_AND_CLAUDE_CODE_BENCHMARK.md) |
| Plans | [WebGPT OpenAI gateway plan](docs/plans/PLAN_WEBGPT_OPENAI_GATEWAY.md) |
| Reports | [Acceptance report](docs/reports/ACCEPTANCE_REPORT.md) |
| Reports | [Gateway certification](docs/reports/GATEWAY_CERTIFICATION.md) |
| Reports | [Optimization analysis](docs/reports/OPTIMIZATION_ANALYSIS.md) |
| Reports | [Session log — 2026-08-22](docs/reports/SESSION_LOG_20260822.md) |
| Guides | [Authentication and login guide](docs/guides/AUTH_AND_LOGIN_GUIDE.md) |
| Guides | [DS2API compatibility notes](docs/guides/DS2API_COMPAT_NOTES.md) |
| Guides | [**User & CTF Solving Guide (Playbook & Prompts)**](GUIDE.md) |

## Cài đặt

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
playwright install chromium
```

Nếu dùng Chromium hệ thống, truyền `executable_path` khi khởi tạo `BrowserManager`. Mặc định toolkit dùng browser do Playwright quản lý.

## Thiết lập profile

```bash
gpt-web setup
```

Một cửa sổ Chromium headful sẽ mở để thiết lập profile. Profile mặc định:

```text
~/.local/share/bqa/chatgpt-profile/
```

### Dùng Brave đang mở (CDP attach)

Khi browser do Playwright mở bị ChatGPT chặn, hãy mở **một profile Brave riêng** với CDP chỉ bind loopback, rồi tự đăng nhập trong cửa sổ đó. Gateway chỉ attach vào browser đang chạy; khi gateway dừng, Brave và profile vẫn giữ nguyên.

```bash
gpt-web brave-launch

gpt-web setup --cdp-url http://127.0.0.1:9222
gpt-web api-server --cdp-url http://127.0.0.1:9222 --port 8765
```

`brave-launch` dùng `~/.local/share/bqa/brave-chatgpt-profile/`, bind CDP vào
loopback và in đúng lệnh setup tiếp theo. Không dùng remote CDP: CLI từ chối
endpoint không phải loopback. Không copy/export cookie hay token; session vẫn do
profile Brave quản lý.

## CLI

```bash
# Reconnaissance JSON, không gửi prompt
gpt-web probe --headful --persistent

# Model labels lấy động từ UI
gpt-web models --headful

# Chẩn đoán profile/CDP; thêm --browser chỉ kiểm tra composer/auth, không gửi prompt
gpt-web doctor --free

# Gửi turn mới
gpt-web send --text "Xin chào" --headful

# Mở conversation cũ rồi follow-up
gpt-web send --conversation <conversation-id> --text "Tiếp tục" --json

# Capture một biến duy nhất
gpt-web experiment --exp-id E00_IDLE --action idle
gpt-web experiment --exp-id E01_NEW_CHAT --action new-chat
gpt-web experiment --exp-id E02_MODEL_OPEN --action model-open
gpt-web experiment --exp-id E10A_SEND --action send
```

Artifacts được lưu ngoài repo tại `~/.local/share/bqa/webchat-reverse/`, directory mode `0700`, file mode `0600`. Header/body nhạy cảm được redact trước khi ghi artifact.

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

## Local API gateway

```bash
gpt-web api-server --headful --port 8765
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

Gateway chuẩn hoá request trước khi thực thi, serialize writer và tự correlate prefix của mảng `messages` với ChatGPT Web conversation trước đó. Response trả header `x-webgpt-session-id`; client có thể gửi lại header này để chọn session tường minh, nhưng vòng lặp OpenAI chuẩn gửi full history vẫn hoạt động mà không cần extension.

Tool calling là controller protocol qua sentinel fail-closed, không phải native ChatGPT Web function calling. Chỉ block `<WEBGPT_TOOL_CALL>` hợp lệ, có tool name đã advertise mới được map; JSON/prose thường không bao giờ được thực thi. `role=tool` phải có ID khớp pending assistant call. Vì DOM render của ChatGPT có thể rewrite nội dung đang sinh và SSE không thể retract, gateway buffer response đến khi hoàn tất rồi phát thành các chunk OpenAI-style xác định; đây là correctness-first streaming, không phải token streaming thời gian thực.

V1 nhận `model`, `messages`, `tools`, `tool_choice`, `stream`, `temperature`, `reasoning_effort` (hoặc `reasoning.effort`). `temperature` được chấp nhận nhưng bỏ qua vì ChatGPT Web không có mapping đáng tin cậy. Model chỉ được chọn khi UI chứng minh có picker; khi không có picker, `chatgpt-web` luôn có nghĩa là giữ model hiện hành.

### Responses, Anthropic và Claude Code

Gateway cũng có các route local `POST /v1/responses` và `POST /v1/messages`.
Chúng dùng cùng browser/conversation/tool runtime với Chat Completions; chỉ subset
đã test offline được hỗ trợ. Built-in hosted tools, background mode, encrypted
content, batch/prompt-cache semantics và nội dung không map được sẽ trả lỗi rõ
ràng thay vì bị giả lập.

Claude Code có thể dùng route Anthropic qua `ANTHROPIC_BASE_URL` và một key local
placeholder. Chỉ dùng cấu hình này trên loopback; key không được forward đến API
Anthropic. `scripts/run-claude-code-benchmark.sh` bắt buộc
`WEBGPT_ACCOUNT_MODE=free_anonymous`, có fake smoke test trước browser benchmark
và không bao giờ dùng Plus. Smoke test local đã xác nhận Claude Code 2.1.233 gọi
`/api/hello` và `/v1/messages?beta=true`; benchmark browser Free vẫn cần evidence
PASS riêng trong acceptance report.

Conversation state chỉ persist khi người vận hành chủ động truyền `--conversation-store <path>` cho `api-server`; cache này có TTL, directory mode `0700` và file mode `0600`. Vì cache chứa lịch sử prompt, không bật nó trên máy hoặc thư mục không tin cậy.

Acceptance thực tế gần nhất nằm trong [ACCEPTANCE_REPORT.md](docs/reports/ACCEPTANCE_REPORT.md).

## Kiểm thử

```bash
pytest -q
ruff check .
mypy gpt --ignore-missing-imports
python -m compileall -q gpt

# Combined offline gate
bash scripts/verify.sh
bash scripts/verify-contracts.sh
```

Unit suite không cần network/login. Mọi live reliability/certification run hiện chỉ hợp lệ với `free_anonymous`; nếu browser trở thành authenticated thì run phải fail closed và bị coi là invalid. Mỗi artifact live phải ghi account mode. Các gate chính nằm ở `scripts/verify-free-anonymous.sh`, `scripts/verify-claude-microgates.sh`, `scripts/verify-opencode-microgates.sh`, `scripts/verify-soak-restart.sh`, `scripts/manual-verify-claude.sh`, và `scripts/run-pcap-certification.sh`. Xem [live acceptance matrix](tests/live/README.md), [AGENTS.md](AGENTS.md), và [GATEWAY_CERTIFICATION.md](docs/reports/GATEWAY_CERTIFICATION.md).

Xem [MASTER_EXECUTION_PLAN.md](docs/plans/MASTER_EXECUTION_PLAN.md) để biết execution order hiện hành.
