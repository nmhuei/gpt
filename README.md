# GPT Web Toolkit

Toolkit Python điều khiển một conversation trên ChatGPT Web bằng Chromium, kèm reverse-capture harness và gateway tương thích một phần với OpenAI Chat Completions.

Thiết kế mới không giả định endpoint nội bộ của ChatGPT. UI semantic driver là đường chạy ổn định; protocol replay chỉ được bật khi có fingerprint đã được xác minh từ ít nhất hai experiment và có replay adapter cụ thể.

## Trạng thái

- Hoạt động offline/tested: redaction, artifact permissions, trace normalization/diff, incremental SSE parser, state machine, protocol→UI fallback, OpenAI response/tool-call translation.
- Cần live browser để xác minh theo account/UI hiện tại: login persistence, selectors, model picker, send/stream/reload.
- Chưa bật mặc định: protocol replay. Repo chưa có evidence ledger đủ để hardcode một request contract an toàn.
- Ngoài V1: upload, voice, image generation, Deep Research, GPTs và multi-agent scheduler.

Đây là chủ ý an toàn của `PLAN_REV.md`: nhận được text nhưng conversation không persist không được xem là protocol replay thành công.

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

Một cửa sổ Chromium headful sẽ mở. Đăng nhập thủ công; toolkit không nhận password, không export cookie/token và không tự xử lý CAPTCHA/security challenge. Profile mặc định:

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

Gateway serialize writer và tự correlate prefix của mảng `messages` với ChatGPT Web conversation trước đó. Response trả header `x-webgpt-session-id`; client có thể gửi lại header này để chọn session tường minh, nhưng vòng lặp OpenAI chuẩn gửi full history vẫn hoạt động mà không cần extension.

Tool calling là controller protocol qua sentinel fail-closed, không phải native ChatGPT Web function calling. Chỉ block `<WEBGPT_TOOL_CALL>` hợp lệ, có tool name đã advertise mới được map; JSON/prose thường không bao giờ được thực thi. `role=tool` phải có ID khớp pending assistant call. Vì DOM render của ChatGPT có thể rewrite nội dung đang sinh và SSE không thể retract, gateway buffer response đến khi hoàn tất rồi phát thành các chunk OpenAI-style xác định; đây là correctness-first streaming, không phải token streaming thời gian thực.

V1 nhận `model`, `messages`, `tools`, `tool_choice`, `stream`, `temperature`. `temperature` được chấp nhận nhưng bỏ qua vì ChatGPT Web không có mapping đáng tin cậy. Dùng `--ephemeral` khi cần test anonymous độc lập; profile persistent vẫn là chế độ vận hành chính.

Acceptance thực tế gần nhất nằm trong [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md).

## Kiểm thử

```bash
pytest -q
ruff check .
python -m compileall -q gpt
```

Unit suite không cần network/login. Live reliability suite (20 sequential turns, reload, interrupted generation, selector mutations) chỉ nên được thêm/chạy opt-in sau khi có profile test riêng; không chạy vào account thật một cách ngầm định.

Xem [PLAN_REV.md](PLAN_REV.md) để biết evidence gates và execution order.
