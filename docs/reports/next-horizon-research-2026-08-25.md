# Next-Horizon Research — 2026-08-25

Khảo sát OSS + kỹ thuật mới (2025–2026) cho chân trời kế tiếp sau CODEX-SSE. READ-ONLY,
mỗi finding kèm bằng chứng URL + task CONCRETE ghi ROADMAP (tên / file test / effort).

---

## Trục 1 — ChatGPT-web→API bridges còn sống

### F1.1 — Kitjesen/chatgpt-to-api (FastAPI, 02/2026, hoạt động với Plus)
https://github.com/Kitjesen/chatgpt-to-api
Cùng endpoint `/backend-api/codex/responses` — xác nhận độc lập spec CODEX-SSE (S1).
**Chi tiết mới so với spec hiện tại:**
- Vision: `image_url` (URL lẫn base64/data-URI, có `detail`) → `input_image` trên nhánh codex.
- Tool history → `function_call` / `function_call_output` items; parse `function_call_arguments.delta/.done`.
- `response_format` `json_object` | `json_schema` pass-through.
- Session-token (`__Secure-next-auth.session-token`, TTL ~30 ngày) auto-refresh AT trước khi hết hạn, persist về `.env`; admin endpoint hot-swap token không restart.
- Model slug PHẢI dấu chấm (`gpt-5.2`, không `gpt-5-2`); legacy names map về 5.2.
- Params không hỗ trợ (`top_p`, penalties, `max_tokens`) nhận rồi **im lặng drop**.
- TLS: curl_cffi Chrome 131 (mình dùng chrome146 — tốt hơn, nhưng lưu ý phải đồng bộ UA↔impersonate).

### F1.2 — sabyaghosh/gpt2api (Python, Docker, 410 commits)
https://github.com/sabyaghosh/gpt2api
Nhánh `/backend-api/conversation` cổ điển nhưng có trick đáng lấy:
- **PoW handling**: `POW_DIFFICULTY` threshold `000032` cho sentinel/proof-of-work (nhánh f/conversation).
- **Multi-account pool**: list AccessToken/RefreshToken, xoay vòng sequential/random, auto-retry × rotate khi fail; seed-based account assignment.
- Scheduled refresh RefreshToken→AccessToken mỗi 4 ngày lúc 03:00.
- Team account: header `ChatGPT-Account-ID`, hoặc `Bearer <token>,<account_id>`.
- Export proxy riêng để giấu IP nguồn khi download file/image (IP Nhật hay fail no-login).
- `ENABLE_LIMIT` chủ động tôn trọng rate-limit để tránh ban.

### F1.3 — David-Factor/codex-responses-proxy (Go, active 06/2026)
https://github.com/David-Factor/codex-responses-proxy
Proxy thuần `/v1/responses` → codex/responses. **Payload-patch rules quý:**
- Force `stream:true`; `store:false` (bắt buộc — khớp spec mình S3).
- **Strip các `reasoning` items được replay lại** (store:false ⇒ không persist ⇒ gửi lại là lỗi/vô nghĩa).
- **DELETE `max_output_tokens` / `max_completion_tokens`** — backend từ chối/nếu không sẽ lệch shape.
- Thêm `instructions` nếu thiếu; convert string `input` → list items.
- Rename/drop tool type: `web_search_preview` → `web_search`.
- Refresh flow chính xác: POST `https://auth.openai.com/oauth/token` với refresh_token từ `~/.codex/auth.json`, ghi đè file 0600; header `ChatGPT-Account-ID` lấy từ claims trong tokens.
- Local file → base64 `input_file` items (`x_proxy_files`, cap 25 MiB, chặn symlink escape).

### F1.4 — openai-oauth tunneling + dư luận rủi ro
https://agent-wars.com/news/2026-03-15-openai-oauth-chatgpt-api-proxy
- Tunnel Codex CLI OAuth → codex/responses; giới hạn = Codex quota từng account ($20 Plus / $200 Pro), không per-token billing.
- Config override client_id/token_url/base_url sẵn để chạy theo khi backend đổi.
- Expectation phổ biến: OpenAI sẽ detect behavioral-fingerprint khác official CLI → revoke/restrict client. (Xem Trục 2.)

### F1.5 — B4PT0R/codex-backend-sdk (Python, MIT, 04/2026) — bản đồ surface lớn nhất
https://github.com/B4PT0R/codex-backend-sdk/tree/main
Unofficial SDK quanh backend ChatGPT/Codex, chứng minh surface xa hơn nhiều so với chỉ `responses`:
- `files.upload`: create metadata → **PUT lên signed URL do backend cấp** → finalize; URI dạng `sediment://file_…`. Đây là đường upload file THẬT qua web.
- `images.generate/edit`: `gpt-image-2`, trả `b64_json`; edit có mask.
- `audio.transcriptions.create` (gpt-4o-mini-transcribe) qua web backend.
- `responses.compact(...)`: trả **opaque encrypted backend state** replay verbatim — co context mà không cần parse (bù cho việc backend KHÔNG có `previous_response_id`, mọi turn phải replay full input).
- `codex.usage()` → `rate_limit.primary_window` + daily token breakdown: **đọc quota chủ động**, thay vì chờ 429.
- Realtime v3 WebRTC SDP + sideband OAuth channel; `responses.websocket.connect()` giữ 1 connection nhiều turn.
- Retry tự động 429/5xx/timeout; hosted MCP/Apps qua `apps.connect_hosted_mcp`.

### F1.6 — Biajin-PKU/webpro-bridge (vendor/chatgpt-api)
https://github.com/Biajin-PKU/webpro-bridge/tree/main/vendor/chatgpt-api
Nhánh f/conversation, học được về hành vi phía ChatGPT:
- Asset polling: input ảnh echo lại `sediment://<id>` — phải bỏ qua ID echo, chờ asset ID MỚI rồi mới download output.
- Deep research cancel cần đọc widget session từ WSS stream trước (conversation_id/message_id/session_id) rồi mới MCP stop.
- Browser-capture headers expire ~10 ngày; **hidden burst limits** vượt hẳn quota UI hiển thị (Pro ~1000 ảnh/ngày vẫn bị cooldown) → cần throttle bucketed + preflight quota per feature bucket (image_gen/file_upload/deep_research).
- Quota-aware routing preflight TRƯỚC khi gửi, chọn account còn quota.

### Tổng kết trục 1 — mình CÓ gì / THIẾU gì
Có sẵn: breaker/backoff toàn cục, multi-account + health, curl_cffi chrome146, TokenBundle persist, hybrid-auth, payload-budget.
Thiếu (theo thứ tự giá trị): (1) image/file input nhánh codex, (2) usage introspection, (3) payload-shape CLI-like (strip reasoning/max_tokens), (4) files-upload signed-PUT, (5) response_format pass-through, (6) compact endpoint.

---

## Trục 2 — Anti-bot / reputation: cái gì sắp đánh vào kiến trúc

### F2.1 — Layered bot scoring của Cloudflare (JA4 + HTTP/2 profile + behavioral reputation)
https://blog.crawlex.net/blog/cloudflare-tls-http2-fingerprinting/
- JA4 hash cipher/ext **sorted** nên chống permutation của Chrome; tính ngay ở handshake, edge-wide.
- Lớp HTTP/2: SETTINGS order/value, WINDOW_UPDATE, PRIORITY frames, pseudo-header order (Chrome m,a,s,p) — khó spoof hơn TLS vì engine emit liên tục; pseudo-header order lạ = high-confidence automation.
- **Cross-layer consistency**: TLS giả Chrome + HTTP/2 Go + JS headless tạo mâu thuẫn mà từng lớp riêng không thấy. Đây đúng mô hình hybrid của mình (CloakBrowser mint cf_clearance ↔ curl_cffi gọi API) — điểm yếu số 1 nếu hai stack lệch phiên bản/fingerprint.
- **JA4 Signals (08/2024)**: reputation per-fingerprint cửa sổ 1h toàn cầu — `browser_ratio_1h`, `h2h3_ratio_1h`, `reqs_quantile_1h`. Fingerprint "browser-like" mà ít browser dùng / volume spike bất thường → feature âm vào ML score.
- Heuristics DB (~50 rule từ H2 fingerprints + ClientHello ext) ép score=1 trực tiếp.
- QUIC/h3: tín hiệu chuyển sang QUIC transport parameters.

### F2.2 — OpenAI đã flag sub2api (subscription→API redistribution) qua fraud system
https://www.gate.com/news/detail/openai-clarifies-codex-rate-limit-discrepancies-blames-sub2api-abuse-23610462 (2026-08-21)
- OpenAI công khai xác nhận hệ thống fraud-prevention đã cờ việc "convert subscriptions into API traffic via sub2api and redistributing".
- Cùng ngày: Codex Pro 5x users báo weekly quota tụt 77% — môi trường quota đang siết toàn cầu.
- Kết hợp F1.4: tín hiệu detect khả dĩ = payload/headers KHÔNG giống official codex CLI dù originator khai `codex_cli_rs`. **Gateway mình gửi `<WEBGPT_MESSAGE>` block + tool schema qua nhánh codex — độ phân kỳ này là bề mặt detect rõ nhất.**
- Chưa thấy ban account hàng loạt — nhưng hướng đi đã công bố.

### F2.2b — Đối xứng Turnstile giữa 2 endpoint
Kitjesen (F1.1) xác nhận trạng thái hiện tại: `/backend-api/conversation` sau challenge; `codex/responses` trả **400 chứ không 403** ⇒ chưa Turnstile-gated. Nhưng đây là bất biến KHÔNG cam kết — nếu flip, CF-RESILIENCE re-mint hiện tại (phủ f/conversation) không tự cứu nhánh codex. Cần detector 403-challenge chung cho cả hai nhánh.

### Hàm ý cho repo
1. Đồng bộ consistency: UA ↔ curl_cffi impersonate ↔ H2 settings một chuỗi duy nhất, kiểm tra bằng dump thực (hiện chrome146 + UA thật đã khớp — giữ kỷ luật này khi nâng cấp).
2. Payload shape CLI-like ở nhánh codex: strip reasoning replay, không gửi max_output_tokens, thêm `session_id`/`version` header kiểu S3, instructions trông như CLI prompt thay vì block WEBGPT dài.
3. Cadence nhân bản: request rate + jitter kiểu human/CLI (CLI cũng burst, nhưng đều đặn hơn gateway fanout).
4. Monitor flip 403 trên codex/responses → alarm sớm trước khi Turnstile hoá.

---

## Trục 3 — Parity gap xa: tính năng CLI dùng mà chưa ai bridge

### P3.1 — Image/file INPUT (gap lớn nhất, CLI dùng thật: Read() screenshot, paste ảnh)
- Bằng chứng làm được: Kitjesen chuyển image_url/base64 → `input_image` trên codex/responses (F1.1); codex-responses-proxy làm `input_file` từ local file (F1.3); B4PT0R có files.upload signed-PUT chuẩn (F1.5).
- Hiện trạng repo: P1-2A chỉ placeholder `[image omitted]` — drop ảnh âm thầm.
- Task: **CODEX-IMG-INPUT (M)** — nhận image_url/base64 ở cả 2 protocol → `input_image` data-URL nhánh codex; test mở rộng `tests/test_codex_sse.py` (+ case Anthropic `source.base64`). Upload file thật (signed-PUT) tách task L sau.

### P3.2 — Prompt caching semantics
- Backend codex: KHÔNG có `previous_response_id`, store:false ⇒ zero server-side cache; mọi turn replay full input (trùng thiết kế render_messages hiện tại — hợp lý).
- Lối ra: `responses.compact(...)` trả encrypted state replay verbatim (F1.5) — nén context mà không phá payload-budget.
- Task: **COMPACT-EVAL (S, spike)** — thử 1 call compact thật, đo bytes tiết kiệm vs payload-budget trim; quyết định có đưa vào luồng session dài không.

### P3.3 — Structured outputs / response_format
- Kitjesen pass-through `json_object`|`json_schema` (F1.1). Server OpenAI-side của mình chưa expose.
- Task: **RESPONSE-FORMAT-PASSTHRU (S)** — map response_format vào instructions + validate JSON trả về; test `tests/test_api_server.py`.

### P3.4 — Usage/quota introspection chủ động
- `codex.usage()` → `rate_limit.primary_window` (F1.5); gpt2api ENABLE_LIMIT cùng tư duy (F1.2).
- Task: **USAGE-INTROSPECTION (M)** — poll usage định kỳ/khi nghi, wire vào `gpt/transport/breaker.py` làm ngưỡng preemptive (cooldown TRƯỚC khi ăn 429), per-account; test mở rộng `tests/test_backoff_breaker.py`.

### P3.5 — Computer-use stream / realtime
- Không tìm thấy bridge web nào cho computer-use tool; realtime/WebRTC tồn tại trong SDK (F1.5) nhưng ngoài phạm vi CLI hiện tại. Ghi WATCH-ONLY, không task.

### P3.6 — Image generation/edit (chiều output)
- gpt-image-2 generate/edit qua web (F1.5, F1.6). Claude CLI ít dùng, nhưng mở đường tool `image_gen` cho automation loop. Effort M, ưu tiên thấp hơn P3.1.

---

## Bảng task đề xuất ghi ROADMAP

| Task | File test | Effort | Nguồn |
|---|---|---|---|
| CODEX-PAYLOAD-CLI-SHAPE: strip replayed reasoning items + DELETE max_output_tokens + thêm session_id/version header + instructions CLI-like | tests/test_codex_sse.py | S | F1.3, F1.4, F2.2 |
| CODEX-IMG-INPUT: image_url/base64 → input_image cả 2 protocol nhánh codex | tests/test_codex_sse.py + test_api_server.py | M | F1.1, F1.3 |
| USAGE-INTROSPECTION: codex.usage → breaker preemptive per-account | tests/test_backoff_breaker.py | M | F1.5, F1.2 |
| RESPONSE-FORMAT-PASSTHRU: json_object/json_schema OpenAI-side | tests/test_api_server.py | S | F1.1 |
| CF-CODEx-403-MONITOR: challenge-detector chung codex/responses + alarm khi flip Turnstile | tests/test_cf_resilience.py | S | F2.2b |
| COMPACT-EVAL: spike responses.compact đo lợi ích vs payload-budget | spike script + notes | S | F1.5 |
| FILES-UPLOAD-WEB: metadata→signed PUT→finalize cho input_file | tests/test_codex_sse.py | L | F1.5, F1.3 |
| IMGGEN-WEB: gpt-image-2 generate/edit tool | tests/live | M | F1.5, F1.6 |

Ưu tiên top-3: **CODEX-PAYLOAD-CLI-SHAPE (S)** — rẻ, làm NGAY trước khi CODEX-SSE đóng băng shape, giảm bề mặt fraud-detect (Trục 2 đang nóng: sub2api bị cờ công khai 2026-08-21); **CODEX-IMG-INPUT (M)** — giá trị CLI cao nhất (Read screenshot/paste ảnh đang bị drop), 2 nguồn độc lập xác nhận khả thi; **USAGE-INTROSPECTION (M)** — biến breaker phản ứng thành chủ động đúng lúc quota toàn cầu đang siết (−77% weekly Pro).
