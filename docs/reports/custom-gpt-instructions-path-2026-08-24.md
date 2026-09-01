# Custom GPT / Project Instructions — kênh trusted cho tool protocol

Ngày: 2026-08-24 · Loại: nghiên cứu tĩnh (không sửa production) · Bối cảnh: VERIFY-R4

## Tóm tắt điều hành

- **Khả thi về hạ tầng: CÓ.** Repo đã tự động hoá được toàn bộ vòng đời tạo Custom GPT qua
  `/backend-api/gizmos` từ trang authenticated (không cần UI flow), và trường
  `instructions` đã nằm sẵn trong payload — hiện chỉ bị hardcode một câu ngắn.
- **Điểm thiếu duy nhất ở phía tạo:** không có tham số truyền instructions dài;
  và gateway chưa có khái niệm "chat trong gizmo" (payload gửi tin nhắn chưa từng
  chứa `gizmo_id`, `ConversationRecord` không lưu trường này).
- **Khả thi về mục tiêu gốc (model xem protocol là trusted): CHƯA XÁC MINH.** Đây là giả thuyết
  có cơ sở (instructions của gizmo được server chèn vào system-context với vai trò
  author/developer content) nhưng mức chắc chắn trung bình (~60–70%), vì lớp
  injection-defense quan sát được ở VERIFY-R4 hoạt động trên message body và chưa có
  bằng chứng nào cho biết nó xử lý thế nào với gizmo instructions. Chỉ pilot mới trả lời được.
- **Rủi ro chính:** (1) giới hạn 8000 ký tự instructions — protocol đo được 4.5–4.8k cho
  2 tool, full toolset có thể tràn; (2) tạo gizmo cần tài khoản Plus/Team (anon/free
  sẽ 403); (3) cùng exposure ToS như hiện trạng reverse-engineering, không thêm rủi ro policy
  riêng biệt rõ nào cho việc dùng GPT làm controller.

---

## 1. Quy trình tạo Custom GPT hiện tại

### Chuỗi gọi lệnh

```
gpt-web install-bqa-plugin            (CLI)
  └─ gpt/debug.py::cmd_install_bqa_plugin          (dòng 859)
       └─ gpt/bqa_installer.py                     (re-export mỏng)
            └─ gpt/mcp/installer.py::BQAPluginInstaller.register_plugin   (dòng 208)
```

### Bên trong `register_plugin()` (gpt/mcp/installer.py)

3 bước:

1. **verify_and_ensure_bqa()** (dòng 48) — health-check BQA local `127.0.0.1:18427`,
   tự restart nếu chết. Không liên quan trực tiếp đến gizmo.
2. **get_or_create_public_tunnel()** (dòng 88) — tái dụng hoặc launch `cloudflared`
   trycloudflare tunnel cho Action domain.
3. **Đăng ký gizmo** (dòng 228–332) — **KHÔNG phải UI flow**: connect_over_cdp tới
   Chromium đang đăng nhập (port 9222, tự launch Cloak nếu chưa có — dòng 230–250),
   rồi chạy JS in-page:
   - `GET /api/auth/session` lấy accessToken;
   - `GET /backend-api/gizmos/snorlax/sidebar?owned_only=true&limit=50` — list gizmo
     để chống trùng tên (tự đổi thành `"Tên (2)"`);
   - `POST /backend-api/gizmos` với payload (dòng 287–306):

```js
{
  display: { name, description, prompt_starters: [...] },
  instructions: 'You are BQA Autonomous Security Bot...',   // ← HARDCODE, dòng 293
  files: [],
  tools: [{
    type: 'plugins_prototype',
    user_settings: { is_installed: true },
    metadata: { domain, raw_spec: JSON.stringify(openapi_spec), auth: { type: 'none' } }
  }]
}
```

   - Response trả `gizmo.id` → `BQAPluginResult.gizmo_id`.

**Kết luận:** trường Instructions **đặt được độ dài tuỳ ý qua API này** (payload là
string tuỳ ý; chỉ có validate phía server về limit). Không cần click UI editor gì cả.

### Bản sao thứ hai

`gpt/drivers/protocol_fast.py::FastProtocolClient.register_mcp_plugin_fast` (dòng 68–159)
lặp lại đúng flow JS trên (instructions hardcode dòng 119) + `list_plugins_fast()`
(dòng 161) để list gizmo. **Hai bản copy dễ drift — nên chọn một làm chuẩn.**

### `gizmo_id` xuất hiện ở đâu trong codebase (grep toàn repo)

| Vị trí | Vai trò |
|---|---|
| `gpt/mcp/installer.py:25,217,262–336` | tạo/list gizmo, trả gizmo_id |
| `gpt/drivers/protocol_fast.py:91–178,153,221` | fast-path tạo/list gizmo |
| `gpt/debug.py:867` | in kết quả CLI |

→ **Gateway/runtime/transport hoàn toàn KHÔNG dùng gizmo_id**. Chưa có đường nào
"chọn gizmo cho conversation".

### Lỗ hổng của CLI wrapper

`cmd_install_bqa_plugin` chỉ nhận `--name/--description/--cdp-url`; không có cách
truyền instructions dài (phải sửa mới dùng được cho WebGPT Controller).

---

## 2. Nếu tạo Custom GPT "WebGPT Controller" — gateway cần thay đổi gì

Mục tiêu: conversation qua gateway diễn ra **bên trong gizmo đó**, để instructions
(chứa JSON function-calling protocol + WORKSPACE POLICY, hiện bị inject vào message
body qua `ToolTranspiler.build_tool_instructions`) trở thành system-context.

### Danh sách file:hàm phải đụng

**Phía tạo (parameter hoá instructions):**
1. `gpt/mcp/installer.py::BQAPluginInstaller.register_plugin` — thêm tham số
   `instructions: str` (mặc định giữ text cũ), truyền vào dict args của `page.evaluate`.
2. `gpt/debug.py::cmd_install_bqa_plugin` + parser `install-bqa-plugin` (dòng 928–941) —
   thêm `--instructions-file PATH`.

**Phía hội thoại trong gizmo (đường curl/SSE — path chính của gateway):**

3. `gpt/utils/types.py::SendRequest` (dòng 139) — thêm trường
   `gizmo_id: str | None = None`.
4. `gpt/transport/curl_transport.py::_build_conversation_payload` (dòng 256–277) — khi
   có gizmo: thêm field top-level `"gizmo_id": "g-..."` vào payload
   (hiện chỉ có `conversation_mode: {"kind":"primary_assistant"}`, dòng 271).
   ⚠️ Vị trí/chính tả field cần xác minh live ở bước pilot — đây là điểm duy nhất của
   hợp đồng request không có bằng chứng trong repo.
5. `gpt/conversations.py::ConversationRecord` (dòng 100) — thêm
   `gizmo_id: str | None = None` để persist + restore đúng ngữ cảnh gizmo
   (worker-affinity/restore ở runtime dựa trên record).

**Phía hội thoại trong gizmo (đường UI fallback):**

6. `gpt/drivers/ui.py` — thêm hàm `open_gizmo(gizmo_id)` (goto
   `https://chatgpt.com/g/{gizmo_id}`, tương tự `open_conversation` dòng 792);
   `new_conversation` (dòng 784) nhận origin override.
7. `gpt/transport/session.py::ChatGPTWebSession.new_conversation/open/send` (dòng
   311/339/395) — nhận và giữ `gizmo_id`, truyền xuống driver.

**Phía điều phối:**

8. `gpt/gateway/runtime.py::position_session` (dòng 1138–1175) — nhánh
   fresh-conversation: nếu record/session mang gizmo_id → navigate vào trang gizmo
   thay vì chatgpt.com root; nhánh restore thì `session.open(conversation_id)` như cũ
   (conversation thuộc gizmo vẫn mở được bằng /c/{id}).
9. `gpt/gateway/server.py::_position_session` (dòng 1653) + chỗ resolve model
   (dòng ~800, `ModelRegistry.resolve`) — route cấu hình: env `WEBGPT_GIZMO_ID` hoặc
   alias model `"webgpt-controller"` → gizmo_id; gắn vào record lúc tạo session.
10. `gpt/model_registry.py` — chỉ cần không đụng: alias map nạp từ file đã hỗ trợ
    chuỗi tuỳ ý; nhưng `select_model` UI sẽ bỏ qua khi ở trang gizmo (composer gizmo
    dùng model mặc định) → cần guard tránh gọi `select_model` gây lỗi sau khi vào gizmo.

**Phía prompt (giải phóng message body):**

11. `gpt/gateway/runtime.py::execute_raw_on_session` (dòng 1215) — khi bật chế độ
    gizmo: không nhúng `build_tool_instructions` vào message body nữa (hoặc chỉ còn
    một dòng nhắc "follow your instructions"), giữ nguyên transpiler đầu ra
    `<WEBGPT_TOOL_CALL>` để parse phía gateway không đổi.
12. `gpt/utils/toolcall.py::ToolTranspiler.build_tool_instructions` — thêm phương thức
    xuất bản thân protocol (schema + WORKSPACE POLICY + format ví dụ) dưới dạng text
    để nhét vào instructions, tái dùng chung nguồn sự thật với renderer hiện tại.

### Điều KHÔNG cần đổi

- Parser tool-call (`toolcall.py` phần parse) — model vẫn viết `<WEBGPT_TOOL_CALL>`
  trong response text; gateway parse như cũ.
- Tool execution/orchestrator (`orchestrator/race_solver.py`, `session_runner.py`).
- Auth/multi-account (`auth/`, `transport/multi_account.py`).

---

## 3. Đánh giá rủi ro (kèm mức chắc chắn)

### 3.1 Limit 8000 ký tự instructions — CHẮC CHẮN CAO

GPT builder UI giới hạn Instructions 8000 ký tự; backend-api gần như chắc chắn enforce
cùng giá trị. **Đo thực tế trong repo** (chạy `ToolTranspiler.build_tool_instructions`
với 2 tool Bash+Read):

| Protocol | Số ký tự |
|---|---|
| xml | 4.834 |
| json-fn | 4.573 |

→ Với 2–3 tool là vừa; full toolset (thêm Write/virtual-write + examples) sẽ tiến gần
hoặc vượt 8000. Mitigation: dùng biến thể json-fn nén, bỏ ví dụ dài, hoặc chuyển phần
schema sang OpenAPI spec của Action (ngách riêng, hạn mức lớn hơn nhiều).

### 3.2 Policy cấm làm automation controller — RỦI RO THẤP–TRUNG BÌNH, MỨC CHẮC CHẮN TRUNG BÌNH

Không có điều khoản public nào cấm "Custom GPT làm controller cho automation của chủ
tài khoản" — tạo GPT + Action gọi API của mình là use case chuẩn của sản phẩm.
Rủi ro thực nằm ở chỗ khác, vốn đã tồn tại từ trước: toàn bộ gateway đi đường
reverse-engineered (sentinel headers, cf_clearance) → cùng exposure ToS hiện hữu,
việc dùng gizmo **không tăng thêm** rủi ro riêng. Lưu ý phụ: framing "run arbitrary
shell commands" trong instructions/description có thể chạm safety classifier khi
review gizmo (gizmo có thể bị flag nếu mô tả quá lộ) — nên đặt tên/mô tả thấp-key,
chi tiết kỹ thuật để trong instructions.

### 3.3 "Model xem instructions là trusted" — GIẢ THUYẾT HỢP LÝ NHƯNG CHƯA XÁC MINH, chắc chắn ~60–70%

Cơ sở tin:
- Gizmo instructions được server chèn vào context với vai trò author/developer
  content, khác tầng user-message; các nghiên cứu system-prompt extraction từ trước
  cho thấy chúng nằm trong system block.
- VERIFY-R4 chứng minh lớp defense classify nội dung **message body** là injected
  (23/24 từ chối) — tức classifier phân biệt theo *kênh*, đúng loại ranh giới mà
  gizmo instructions nằm bên kia.

Nghi ngờ hợp lý:
- Defense hiện đại cũng scan instructions (ví dụ chặn "ignore previous instructions"
  trong GPT config); một protocol mô tả cách emit tool-call JSON có thể vẫn bị coi là
  hành vi vi phạm bất kể kênh.
- Kể cả khi instructions được đọc, model có thể vẫn từ chối emit protocol nếu
  classifier chạy trên output.

**Kết luận: phải coi đây là giả thuyết cần pilot, không phải kế hoạch chắc chắn.**

### 3.4 Yêu cầu tier tài khoản — CHẮC CHẮN CAO

Tạo/cập nhật Custom GPT cần Plus/Team/Enterprise. Free/anon (`/backend-anon`) sẽ 403
ở POST /backend-api/gizmos. `gpt/auth/accounts.py` không track plan → cần kiểm tra
tài khoản nào trong profiles là Plus trước khi pilot.

### 3.5 Rủi ro kỹ thuật khác

- Hợp đồng `gizmo_id` trong conversation POST chưa có bằng chứng trong repo (xem §2.4).
- Update instructions của gizmo hiện có: repo chỉ có code POST tạo mới; update có thể
  cần PUT/PATCH `/backend-api/gizmos/{id}` (chưa xác minh; fallback xoá-tạo-lại).
- Hai bản copy installer dễ drift (§1).
- Model picker trong trang gizmo khác trang thường → guard `select_model` (§2.10).

---

## 4. Kế hoạch pilot 3 bước (KHÔNG thực hiện trong đợt này)

**Tiền điều kiện:** 1 phiên Cloak CDP đăng nhập tài khoản Plus; không cần BQA tunnel
cho pilot (turn thử chỉ cần 1 tool giả lập echo phía gateway).

**Bước 1 — Tạo gizmo "WebGPT Controller":**
Sửa `mcp/installer.py::register_plugin` nhận `--instructions-file` (thay đổi §2.1–2.2),
soạn instructions = biến thể json-fn nén của protocol + WORKSPACE POLICY
(`ToolTranspiler` xuất bản text, mục tiêu ≤7k ký tự để dư biên). Chạy
`gpt-web install-bqa-plugin --name "WebGPT Controller" --instructions-file ...`,
ghi lại `gizmo_id`. Verify đọc-không: GET lại gizmo detail, đối chiếu độ dài
instructions server đã chấp nhận.

**Bước 2 — Set/adjust instructions:** dùng cùng lệnh để cập nhật (nếu POST lên gizmo có
sẵn hoạt động như update thì dùng luôn; nếu không, bổ sung PATCH
`/backend-api/gizmos/{id}`). Đây cũng là bước đo xem backend reject ở limit nào
(nếu 400 → giảm rồi thử lại; ghi lại giá trị limit thật).

**Bước 3 — 1 turn thử qua gateway:** đặt env/config `WEBGPT_GIZMO_ID` (thay đổi
§2.3–2.12, tối thiểu đường curl: SendRequest + payload + record). Gửi 1 task tool
tối giản ("run pwd and report the output") với `WEBGPT_TOOL_PROTOCOL=json-fn`.
Tiêu chí thành công: ≥1 `<WEBGPT_TOOL_CALL>` JSON hợp lệ trong response, không refusal,
không correction loop; so baseline VERIFY-R4 (0/24 turn có tool call hợp lệ).
Trace + response dump vào prompt-debug như vòng trước để đối chiếu.

Rollback: gateway không có gizmo_id → hành vi y như hiện trạng (env off = off).

---

## 5. Verdict cuối

| Câu hỏi | Trả lời |
|---|---|
| Khả thi không? | Hạ tầng: có sẵn ~80% (tạo gizmo đã tự động hoá xong). Thiếu: parameter instructions, gizmo_id trong pipeline hội thoại (12 điểm sửa, liệt kê §2). |
| Việc cần làm cụ thể | §2 — installer (2 điểm), transport/types (3), UI driver (2), gateway (3), prompt builder (2). |
| Rủi ro lớn nhất | Giả thuyết trusted-context chưa được chứng minh (60–70%); limit 8000 ký tự; cần tài khoản Plus; hợp đồng `gizmo_id` trong POST hội thoại chưa xác minh. |
| Kế hoạch pilot | §4 — 3 bước, chi phí thấp, rollback bằng env off, không đụng tool execution layer. |
