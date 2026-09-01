# P1-2A — Image placeholder (chấm dứt drop ảnh âm thầm)

Ngày: 2026-08-26 · Status: DONE (render layer) · ROADMAP ref: P1-2A-IMAGE-PLACEHOLDER (TODO S)

## Vấn đề

Claude Code CLI gửi image block (`{"type":"image","source":{...}}` nhánh Anthropic,
`{"type":"image_url",...}` nhánh OpenAI) vào gateway thì ảnh bị drop âm thầm trong
đường render messages → prompt text; model không hề biết có ảnh tồn tại.

## Thay đổi

Implementation nằm ở **`gpt/utils/promptcompat.py`** (điểm hội tụ render của cả hai
nhánh protocol); `gpt/promptcompat.py` chỉ re-export shim:

- `content_text()` nhận diện `_IMAGE_BLOCK_TYPES = {"image", "image_url"}`, thay drop
  bằng placeholder `[image omitted: <mime> ~<KB>KB — image upload not supported yet]`
  (không biết size → bỏ thành phần `~KB`; không biết mime → `unknown`).
- `_base64_size_kb()` ước lượng size decoded (len×3//4, ceil KB).
- Kill switch `WEBGPT_IMAGE_PLACEHOLDER=0` khôi phục hành vi silent-drop cũ (đọc
  động mỗi lần render, phục vụ rollback/test).
- Placeholder là plain user-content text bên trong JSON payload — không đụng vùng
  controller protocol (`<cmd>`/`<json>`/bootstrap).

Không cần sửa `assistantturn.py` (parse output model) hay `openai_types.py` (format
response): usage estimator chars÷4 (`estimate_openai_usage`,
`StreamUsageEstimator`) ăn theo tự nhiên vì đầu vào là chuỗi rendered prompt đã chứa
placeholder — có test chốt.

Files đổi: `gpt/utils/promptcompat.py`, `gpt/promptcompat.py`,
`tests/test_image_placeholder.py`.

## Tests (`tests/test_image_placeholder.py`, 7 tests)

1. image block anthropic → placeholder mime+size trong rendered prompt.
2. Kill switch → silent drop trở lại.
3. Text-only → output không đổi (regression).
4. Nhiều ảnh → placeholder đúng thứ tự.
5. `image_url` data URI → mime+size đúng.
6. `image_url` OpenAI qua full `render_messages` (shape thật của nhánh openai_chat —
   `parse_chat_completion_request` giữ nguyên list block) + kill switch.
7. Estimator chars÷4 tính cả placeholder (so sánh on/off + đúng công thức ceil).

Kết quả chạy targeted + kề bên:
`.venv/bin/python -m pytest tests/test_image_placeholder.py tests/test_prompt_budget.py tests/test_protocol_adapters.py tests/test_api_server.py -q`
→ **84 passed in 0.93s**.

## Gap đã xác minh — cần agent sở hữu protocol_adapters.py xử lý

Đường `/v1/messages` thật của Claude CLI đi qua `parse_anthropic_request`
(`gpt/api/protocol_adapters.py`) → `_text_blocks(content, {"text"})` strip image
block **ở ingress**, trước khi message tới render layer. File này ngoài scope cho
phép của task (agent khác sở hữu). Cho tới khi được sửa (cho phép type `image`
qua lại hoặc tự chèn placeholder tại đó), placeholder chỉ kích hoạt với payload
giữ nguyên list block tới `render_messages` (nhánh openai_chat và mọi caller gọi
thẳng promptcompat). Đề xuất mở mục việc riêng hoặc chuyển cho owner
protocol_adapters.

## Lưu ý vận hành

- Không commit, không restart gateway (theo quy tắc task).
- `ruff`/`mypy` không có trong `.venv`; ruff 0.16 hệ thống flag RUF022/I001 trên cả
  file untouched → mismatch phiên bản, không phải regression của thay đổi này.
