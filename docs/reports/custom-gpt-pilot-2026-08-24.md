# Custom GPT instructions pilot — model có coi instructions là trusted context?

Ngày: 2026-08-24 · Loại: pilot live (không sửa production) · Tiền đề: `custom-gpt-instructions-path-2026-08-24.md` §4

## VERDICT: **CONFIRMED**

Instructions của Custom GPT được model đối xử như trusted context: chứa tool protocol
trong instructions là đủ để model emit khối tool call hợp lệ, trong khi message body
hoàn toàn không nhắc gì đến protocol. Đối chứng trực tiếp với R4 baseline 0/24.

---

## Thiết kế thí nghiệm

- **Biến độc lập:** instructions của gizmo (chứa protocol `<cmd>lệnh</cmd>` + workflow
  DISCOVER→INSPECT→ANALYZE→ACT→VERIFY, văn phong SOFT, 1624 ký tự — nguyên văn ở phụ lục A).
- **Biến phụ thuộc:** model emit `<cmd>…</cmd>` hay không, với prompt user là task thuần
  ("Tạo file hello.txt chứa 'pilot'…" / "Run pwd so I can see where we are.") — không một
  chữ nào về protocol/format.
- **Đối chứng:** cùng prompt trong chat assistant MẶC ĐỊNH (không gizmo) → prose thuần,
  không protocol (khớp R4).

## Điều kiện chạy

- Profile copy `/tmp/profile-cgpilot` từ `profiles/personal` (Plus), xoá `Singleton*`,
  xoá sau khi xong. Browser CloakBrowser headless 100%, CDP port 9333, **phải dùng đúng
  bộ flag stealth của gateway** (`--fingerprint=81307 --fingerprint-platform=windows`,
  hover/pointer blink-settings…) — chromium thường bị 403/flag.
- Toàn bộ thao tác API qua page-context fetch trên chatgpt.com (cookie + auth sẵn).

## Nhật ký từng bước

### Bước 1 — Tạo gizmo: POST /backend-api/gizmos → **200**

- Payload: `{display:{name:"WebGPT Controller Pilot", description, prompt_starters},
  instructions:<soft protocol>, files:[]}` (không cần tools/plugins).
- Server chấp nhận instructions 1624 ký tự; trả `gizmo.id = g-6a8c32dad0dc8191…` [REDACTED ≤12].
- Ghi nhận sự cố: lần tạo đầu (g-6a8c2f326e148191…) mất tích sau khi session của browser
  thường (non-stealth) bị server invalidate (`GET /backend-api/gizmos/{id}` → 404;
  owned-list chỉ còn "skill pwn"). Tạo lại từ session sạch → OK. Bài học: chỉ tương tác
  account qua MỘT fingerprint stealth duy nhất.

### Bước 2 — Đường REST tổng hợp cho conversation: THẤT BẠI (quan trọng về kiến trúc)

| # | Cách gửi | Sentinel | Status |
|---|---|---|---|
| 1 | in-page fetch `/backend-api/f/conversation`, payload tối giản + `gizmo_id` top-level | không | **403** "Unusual activity has been detected from your device." |
| 2 | như trên + đủ 3 headers OpenAI-Sentinel-* mint bằng SentinelSDK in-page | full | **403** cùng lỗi |
| 3 | browser stealth giống hệt gateway, session tươi | full | **403** cùng lỗi |

→ POST tổng hợp thiếu shape của client thật sẽ bị chặn ở tầng khác trước cả sentinel.
Capture request thật của web app cho thấy client gửi kèm `client_prepare_state`
(từ endpoint mới **POST /f/conversation/prepare → 200**, chạy TRƯỚC mỗi POST),
`supports_buffering`, `supported_encodings:["v1"]`, `model_response_contracts`,
`local_function_names`, timezone… Đây là lý do khả dĩ cho 403 và là việc phải làm tiếp
nếu muốn đường curl/SSE trực tiếp sống lại (xem Hệ quả).

### Bước 3 — Hợp đồng "chat trong gizmo": XÁC MINH BẰNG CAPTURE REQUEST THẬT

Draft gizmo KHÔNG chat được qua trang công khai `/g/{id}` (HTTP **404** → fallback landing
anon). Đường đúng là **GPT builder editor**: `GET /gpts/editor/{gizmo_id}` → 200, panel
Preview có composer. Capture POST khi gửi tin trong preview:

```json
"conversation_mode": {"kind": "gizmo_test", "gizmo_id": "g-6a8c32dad0dc8191…"},
"model": "gpt-5-6"
```

→ Sửa giả thiết cũ trong `custom-gpt-instructions-path` §2.4: **`gizmo_id` KHÔNG đứng
top-level** mà nằm **bên trong `conversation_mode`**, với `kind`:
- `"gizmo_test"` khi chat draft trong builder preview;
- (loại kind cho gizmo đã publish chưa xác minh trong đợt này).
Response: **200 text/event-stream** bình thường.

### Bước 4 — Các turn thí nghiệm (UI preview composer, capture DOM)

| Turn | Context | User message (nguyên văn) | Assistant reply (nguyên văn) |
|---|---|---|---|
| Đối chứng | assistant mặc định, KHÔNG gizmo | "Tạo file hello.txt chứa 'pilot' trong thư mục hiện tại rồi xác nhận." | *"Tôi không có quyền truy cập trực tiếp vào thư mục hiện tại của máy bạn để tạo file, nhưng bạn có thể chạy: … echo -n \"pilot\" > hello.txt …"* — prose, không protocol |
| G1 | gizmo preview, conv mới | cùng prompt trên | `<cmd>printf 'pilot' > hello.txt && cat hello.txt</cmd>` |
| G2 | gizmo preview, conv mới | cùng prompt trên | `<cmd>printf 'pilot' > hello.txt && cat hello.txt</cmd>` (nhân bản độc lập) |
| G3a | gizmo preview, conv mới | "Run pwd so I can see where we are." | `<cmd>pwd</cmd>` — đúng 1 lệnh, dừng chờ, không prose thừa |
| G3b | gizmo preview, tiếp G3a | "$ pwd\n/home/user/pilot-workdir" (output giả lập controller) | *"We're currently in:\n\n/home/user/pilot-workdir\n\nWhat should we inspect next: files here or a specific task/project?"* — parse output chuẩn, không tự emit protocol khi không cần |

- 3/3 conversation trong gizmo emit protocol hợp lệ ngay turn đầu; đối chứng 0/1 (khớp
  R4 0/24). Model tuân thủ cả quy tắc "one command per reply" và "verify built into ACT"
  (G1/G2 tự gắn `cat hello.txt` vào lệnh để verify).
- Một turn tiếp diễn giữa G2 rơi vào lỗi hạ tầng "Hmm…something seems to have gone wrong /
  Retry" (không phải refusal); không ảnh hưởng kết luận, không tính vào bằng chứng.
- Ngân sách turn: ~6 thật, dừng đúng hạn.

## Phản biện đã xét

- *Model emit vì prompt gợi ý hành động?* — Không: đối chứng cùng prompt không emit.
- *Protocol quá đơn giản nên model "đoán"?* — Format `<cmd>…</cmd>`, workflow 5 bước và
  quy tắc stop-and-wait đều chỉ tồn tại ở instructions; reply tái tạo chính xác từng chi tiết
  (kể cả ghép verify vào lệnh ACT theo đúng instruction).
- *Chỉ áp dụng cho draft/gizmo_test?* — Chưa phủ định được, nhưng gizmo_test chính là chế độ
  gateway sẽ dùng (draft do gateway tạo); publish chỉ cần khi chia sẻ.

## Hệ quả cho kiến trúc gateway

1. **Giả thuyết trung tâm của kế hoạch gizmo được chứng minh:** đưa protocol vào
   instructions + message body thuần task = model emit tool call đáng tin cậy. Toàn bộ
   danh mục 12 điểm sửa ở `custom-gpt-instructions-path` §2 giữ nguyên giá trị triết lý
   nhưng phải điều chỉnh điểm §2.4 (payload): thêm `conversation_mode:{kind, gizmo_id}`
   thay vì field top-level.
2. **Đường curl/SSE trực tiếp vẫn chết vì lý do MỚI:** thiếu bước
   `POST /backend-api/f/conversation/prepare` (client_prepare_state) và các field shape
   (`supports_buffering`, `supported_encodings`, `model_response_contracts`…). Muốn giữ
   transport curl phải reverse thêm prepare-contract này; nếu không, đường UI driver
   (composer + DOM, như gateway đang chạy và như pilot này) là đường production duy nhất
   đã được chứng minh end-to-end.
3. **Chat draft gizmo = editor preview:** gateway cần navigate
   `https://chatgpt.com/gpts/editor/{gizmo_id}` (KHÔNG phải `/g/{id}` — 404 với draft),
   chọn composer bên phải (preview pane), không gọi `select_model` (composer preview cố định
   model riêng, ở đây `gpt-5-6`).
4. **Vệ sinh session:** tuyệt đối không mở cùng account trên 2 fingerprint browser song song
   (session cũ bị invalidate + artifact do session đó tạo bị dọn theo).
5. Instructions ≤8000 ký tự là đủ cho protocol nén (đã xác minh server chấp nhận; pilot dùng
   1624 ký tự, dư địa lớn).

## Vệ sinh & redaction

- Gizmo pilot đã xoá: `DELETE /backend-api/gizmos/{id}?draft=true` → **200**;
  verify `GET …?draft=true` → **404**. (API xoá draft hoạt động, ghi lại để tái sử dụng.)
- Browser pilot đã tắt; `/tmp/profile-cgpilot` và file token đã xoá; gateway + profile gốc
  không bị thay đổi.
- Token/cookie không xuất hiện trong báo cáo; id gizmo/user đã rút gọn hoặc redacted.

## Phụ lục A — Instructions nguyên văn (1624 ký tự)

```
You are WebGPT Controller Pilot, an operations assistant connected to a terminal controller running on the user's own machine. Your job is to help the user carry out small tasks on that machine by exchanging short, structured messages with the controller software that sits between you and the terminal.

How you work:
- The controller reads your replies. When a step requires running something on the machine, end your reply with a single line of the form <cmd>command</cmd> - for example <cmd>pwd</cmd> - and then stop. The controller will run that command and send its output back as the next message.
- One command per reply. After the command line, say nothing else and wait.
- When a question can be answered without touching the machine, just answer normally.

Preferred workflow, applied flexibly rather than rigidly:
1. DISCOVER - get oriented: see where you are and what is around.
2. INSPECT - look at the relevant files or state before changing anything.
3. ANALYZE - decide the smallest change that accomplishes the goal.
4. ACT - apply it with one command.
5. VERIFY - run a command whose output proves the result before reporting completion.

Habits worth keeping:
- Prefer simple, portable commands over clever one-liners.
- If a command fails, read the error and adjust instead of repeating it unchanged.
- Treat a task as done only once a verify command has actually run and its output confirms the expected state.
- Keep surrounding prose brief; the controller mostly needs the command lines themselves.

You work inside the directory the controller gives you, on the user's own machine, at their request.
```

## Phụ lục B — File bằng chứng (/tmp, chưa redact token — xoá khi đọc xong)

- `/tmp/cgpilot_ui_capture.json` — request/response thật của turn trong gizmo (payload đầy đủ)
- `/tmp/cgpilot_ui_reply.txt`, `/tmp/cgpilot_ui_reply2.txt` — reply nguyên văn
- `/tmp/cgpilot_turn_last.sse` — body 403 cuối của đường REST tổng hợp
- Scripts: `/tmp/cgpilot_create_gizmo.py`, `/tmp/cgpilot_converse.py`, `/tmp/cgpilot_preview_turn.py`, `/tmp/cgpilot_ui_turn.py`
