# Sandbox Protocol Reverse — ChatGPT Web Code Interpreter (2026-08-24)

Mục tiêu: xem ChatGPT web server trả tool-call / tool-output về client dưới dạng nào
khi model chạy code trong sandbox (Python/Code Interpreter), để bắt chước format đó
cho protocol của webgpt thay vì tự phát minh XML sentinel.

Verdict: **THÀNH CÔNG** — đã trigger sandbox thật và capture nguyên vẹn chuỗi frame
`code → execution_output → final text` trên endpoint `/backend-api/f/conversation`.

---

## 1. Phương tiện & môi trường

- Browser: binary CloakBrowser `~/.cloakbrowser/chromium-146.0.7680.177.5/chrome`, headless 100%.
- Profile ANON: context mới tinh → Cloudflare chặn ("Just a moment...") với chromium thường;
  CloakBrowser binary thì qua, chat được qua `/backend-anon/f/conversation` nhưng KHÔNG có python tool
  (model chỉ mô phỏng output trong markdown text block).
- Profile đăng nhập: copy `/home/light/Downloads/webgpt/cloak-profile` → `/tmp/profile-rev-sb`
  (không dùng trực tiếp profile gốc; profile gốc không bị process nào giữ).
- Hook: `context.on("response")` lọc mọi URL chứa `conversation`, lưu raw body + post_data.
- Script tạm: `/tmp/rev-sb/capture.py`. Tổng 3 prompt (1 anon + 2 logged-in).

## 2. Endpoint map

| Endpoint | Method | Vai trò |
|---|---|---|
| `/backend-api/f/conversation` | POST | SSE stream chính của turn (logged-in) |
| `/backend-anon/f/conversation` | POST | SSE stream chính (anonymous mode) |
| `/backend-api/f/conversation/prepare` | POST | chuẩn hoá turn trước khi stream |
| `/backend-api/conversation/{id}/stream_status` | GET | poll trạng thái stream |
| `/backend-api/conversation/{id}/textdocs` | GET | file đính kèm context |

## 3. Transport encoding — "delta" v1

SSE đầu tiên khai báo:

```
event: delta_encoding
data: "v1"
```

Sau đó mỗi event là JSON patch operation:

```json
{"p": "<json-pointer>", "o": "<op>", "v": <value>, "c": <counter>}
```

- `p`: JSON pointer vào message object; `p=""` = gốc (toàn bộ message hoặc default path hiện hành).
- `o` quan sát được: `add` (thêm message mới), `append` (nối text), `replace`,
  `truncate` (`v`=số ký tự cắt từ cuối — dùng để sửa text đã stream!),
  `patch` (`v` = MẢNG các op con áp cùng lúc).
- `c`: counter tăng đơn điệu theo frame (0,1,2,...) — dùng để client kiểm tra thứ tự/bỏ frame trễ.

Ví dụ sửa lỗi giữa dòng (frame thật, turn 2 logged-in):

```
data: {"v":"The output is:\n\n```text\n42\n```\nBạn", ...}          # append nháp
data: {"delta":{"o":"truncate","v":10}}                             # cut lại 10 ký tự cuối
data: {"delta":{"o":"append","v":" is:\n\n```text\n"}}
data: {"delta":{"v":"42\n```\nBạn muốn thử"}}                       # p rỗng = default path
```

Các event type ngoài `delta`: `resume_conversation_token`, `input_message`,
`message_marker`, `title_generation`, `server_ste_metadata`,
`message_stream_complete`, `conversation_detail_metadata`, `beacon_ui_response`,
kết thúc bằng dòng `data: [DONE]`.

## 4. Frame shapes NGUYÊN VĂN (redacted)

### 4.1 Tool CALL — assistant gọi python

Đây là câu trả lời chính: **server đánh dấu "đây là code cần chạy" bằng một message
assistant với `recipient: "python"` + `content_type: "code"`**, gửi qua đúng channel SSE chung:

```json
{"v": {"message": {
  "id": "d7c3e44f-b955-4ff6-94fc-b7d028c11d51",
  "author": {"role": "assistant", "name": null, "metadata": {}},
  "content": {
    "content_type": "code",
    "language": "unknown",
    "response_format_name": null,
    "text": "pow(783, 987, 1000000007)\n"
  },
  "status": "finished_successfully",
  "end_turn": false,
  "weight": 1.0,
  "metadata": {
    "finish_details": {"type": "stop", "stop_tokens": [200012]},
    "is_complete": true,
    "hide_inline_actions": true,
    "disable_turn_actions": true,
    "reasoning_title": "Computing Modular Exponentiation",
    "reasoning_status": "is_reasoning",
    "tool_icons": ["code"],
    "model_slug": "gpt-5-6-thinking",
    "parent_id": "<thoughts-msg-id>"
  },
  "recipient": "python",
  "channel": null
}, "conversation_id": "...", "error": null}, "c": 10}
```

### 4.2 Tool RESULT — sandbox trả kết quả về

**Message riêng với `author.role: "tool"`, `author.name: "python"`,
`content_type: "execution_output"`**, kèm toàn bộ telemetry Jupyter trong
`metadata.aggregate_result`:

```json
{"v": {"message": {
  "id": "8508717c-e7ad-430d-b976-4776a133aec0",
  "author": {"role": "tool", "name": "python", "metadata": {}},
  "create_time": 1787561271.8178065,
  "update_time": 1787561272.7741053,
  "content": {
    "content_type": "execution_output",
    "text": "609471299"
  },
  "status": "finished_successfully",
  "weight": 1.0,
  "metadata": {
    "is_complete": true,
    "aggregate_result": {
      "status": "success",                     // | "failed" khi lỗi
      "run_id": "8b400bbb-694e-4ce3-8616-e3230ddf4ea6",
      "start_time": ..., "end_time": ...,
      "code": "pow(783, 987, 1000000007)\n",   // echo code đã chạy
      "final_expression_output": "609471299",  // giá trị biểu thức cuối
      "in_kernel_exception": null,
      "system_exception": null,
      "messages": [],
      "jupyter_messages": [
        {"msg_type": "status",        "content": {"execution_state": "busy"}},
        {"msg_type": "execute_input", "parent_header": {"msg_id": "..._263_7", "version": "5.3"}},
        {"msg_type": "execute_result","content": {"data": {"text/plain": "609471299"}}},
        {"msg_type": "status",        "content": {"execution_state": "idle"}}
      ],
      "timeout_triggered": null
    },
    "hide_inline_actions": true,
    "disable_turn_actions": true,
    "parent_id": "<thoughts-msg-id>",
    "model_slug": "gpt-5-6-thinking"
  },
  "recipient": "all",
  "channel": null
}}, "c": 12}
```

### 4.3 Cấu trúc turn hoàn chỉnh (thứ tự frame)

```
resume_conversation_token        # JWT topic token (REDACTED)
delta add   : system msg (hidden) x N
delta add   : user msg (echo)
input_message x2                # echo input; có thể là role "developer" do server inject
delta add   : model_editable_context (system, weight 1)
delta add   : THOUGHTS msg (content_type "thoughts", status in_progress)
delta add   : TOOL CALL msg  (recipient="python", content_type="code")
message_marker: {marker:"user_visible_token", event:"first"}     # cho tool-call msg
delta add   : TOOL RESULT msg (role="tool", name="python", execution_output)
delta add   : thoughts summary msg (metadata.tool_summary_type="python",
              metadata.inline_cot_expandable_content.source_message_ids=[<call-id>, <result-id>])
delta add   : reasoning_recap msg ("Worked for 9s", reasoning_status: is_reasoning→reasoning_ended)
delta add   : FINAL TEXT msg (recipient="all", channel="final", content_type="text")
delta append/patch trên parts/0  # text streaming
delta patch : [status=finished_successfully, end_turn=true, metadata.finish_details=...]
message_marker: cot_token / final_channel_token / last_token
server_ste_metadata             # {tool_name:"PythonCaasCotTool", tool_invoked:true, plan_type:"plus"(REDACTED-generic), ...}
message_stream_complete
[DONE]
```

Ghi chú đáng chú ý:

- Message text cuối có `"channel": "final"` — khớp cấu trúc `channel/metadata`
  repo ta đã biết từ probe trước. Tool-call/result messages có `channel: null`.
- Server inject developer message (`role:"developer"`, `metadata.real_author:
  "sonic.genui_prefetcher"`, `content_type:"developer_content"` với instructions
  genui/web.run) — client nhận nó như `input_message` event.
- `message_marker.marker` nhận giá trị: `user_visible_token`, `cot_token`,
  `final_channel_token`, `last_token`; `event`: `first` | `last` — mốc UI cho từng phase.
- Anon mode: endpoint `/backend-anon/f/conversation`, KHÔNG có python tool; model trả
  text block markdown thuần (`parts[0]` chứa ```text fence). Không thấy content_type nào
  mới so với trên.

### 4.4 Client request body (POST /f/conversation)

```json
{
 "action": "next",
 "messages": [{"id":"...", "author":{"role":"user"},
               "content":{"content_type":"text","parts":["<prompt>"]},
               "metadata":{"serialization_metadata":{"custom_symbol_offsets":[]}}}],
 "parent_message_id": "client-created-root",
 "model": "auto",                        // hoặc slug cụ thể
 "client_prepare_state": "success",
 "timezone_offset_min": -420, "timezone": "Asia/Saigon",
 "conversation_mode": {"kind": "primary_assistant"},
 "enable_message_followups": true,
 "system_hints": [],
 "model_response_contracts": [
   {"id": "photo_upload_action.v1", "protocol_version": 1,
    "presets": ["cap:image", "cap:file", "placement:end"]}
 ],
 "supports_buffering": true,
 "supported_encodings": ["v1"],
 "client_contextual_info": {...viewport/screen...},
 "thinking_effort": "standard",          // chỉ khi model thinking
 "local_function_names": ["local.continue_in_work"]
}
```

## 5. So sánh với sentinel XML protocol hiện tại của repo

Repo (`gpt/utils/toolcall.py`, `gpt/utils/toolstream.py`) dùng hướng ngược lại:
nhét `<tool_calls><invoke name=".."><parameter ..>` vào chính TEXT stream của model,
rồi transpiler parse ra; toolstream phải withhold buffer để bắt sentinel bị split,
lo mixed-sentinel, DSML/legacy variants...

ChatGPT server KHÔNG làm vậy: tool call là **một message object riêng biệt trong cùng
SSE stream**, phân loại bằng 2 field có sẵn của message schema:

- call: `recipient=<tool-name>` + `content_type="code"`
- result: `author.role="tool"` + `author.name=<tool-name>` + `content_type="execution_output"`

Không cần sentinel trong text vì text thường nằm ở message khác (`recipient="all"`,
`channel="final"`).

## 6. Khuyến nghị cho webgpt

1. **Bắt chước shape message, không bắt chước sentinel**: nếu mình làm controller,
   hãy tách tool-call khỏi prose thành record riêng kiểu
   `{recipient/tool_name, content_type, text}` thay vì nhét XML vào text stream.
   Format tối thiểu nên gồm: `recipient` (tên tool), `content.content_type` ("code"),
   `content.text` (payload), và cho kết quả: `role:"tool"`, `name`, `content_type:
   "execution_output"`, `text`, kèm block `aggregate_result{status, run_id, code,
   final_expression_output, exceptions}`.
2. **Giữ XML sentinel như lớp tương thích**, nhưng thêm transpiler nhận dạng "chatgpt-style"
   records (JSON line/event có `recipient` + `execution_output`) — giúp bridge trực tiếp
   log/snapshot từ ChatGPT thật vào pipeline test của mình.
3. **Mượn cơ chế delta-patch transport**: `{"p","o","v","c"}` với các op
   add/append/replace/truncate/patch + counter `c` — giải quyết gọn vấn đề repo đang
   xử lý thủ công (streaming text, withhold suffix, sửa lỗi giữa dòng). `truncate`
   cho phép server sửa text đã phát mà không cần resend.
4. **Mượn `message_marker`** (`cot_token`, `final_channel_token`, `last_token`) để UI
   biết ranh giới reasoning/final mà không cần đoán từ content_type.
5. **`aggregate_result.jupyter_messages`** là nguồn telemetry tốt nếu sau này muốn
   render tiến trình chạy code (busy/idle/execute_result) giống UI ChatGPT.

## 7. Hạn chế

- Chỉ trigger được python tool (Code Interpreter); chưa reverse search/browser/agent-mode tools.
- Chưa thấy cơ chế upload-file→analysis (cần multipart flow khác).
- Model slug quan sát được tại thời điểm capture: gpt-5-6 / gpt-5-6-thinking;
  tên tool nội bộ `PythonCaasCotTool`.
- Raw captures (đã redact JWT) còn ở /tmp/rev-sb/*.txt; profile copy đã xoá.

---
Redacted report generated 2026-08-24. JWT/session tokens replaced with placeholders.
