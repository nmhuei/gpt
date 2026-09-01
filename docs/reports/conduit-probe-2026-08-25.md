# CONDUIT-PROBE — authed `POST /backend-api/f/conversation` với FULL shape (2026-08-25)

Rerun từ đầu (lần trước chết vì `upstream_read_error` hạ tầng, không phải lỗi nhiệm vụ).
Mục tiêu theo spec: tối đa **2 request thật** tới `POST /backend-api/f/conversation`
(KHÔNG qua gateway :18000), replay đúng recipe `hybrid-auth-research-2026-08-25.md` +
`header-diff-auth-2026-08-24.md`: conduit handshake trước (`prepare_conduit`
token_manager.py:470), đủ bộ headers (sec-ch-ua trio, oai-session-id,
x-oai-turn-trace-id, oai-client-version/build-number, telemetry), cookie jar đầy đủ
từ profile CloakBrowser (chỉ đọc), body ~15 field mở rộng, curl_cffi `impersonate=chrome146`.

Script: `/tmp/conduit_probe_20260825.py` (transcript raw: `/tmp/conduit_probe_transcript.log`).

## Setup thực hiện

- Copy **chọn lọc** session state của `/home/light/Downloads/webgpt/cloak-profile` →
  `/tmp/conduit-probe-profile` (**30.8 MB**: `Local State`, `Default/{Cookies*,Preferences,
  Secure Preferences}`, `Local Storage`, `Session Storage`) — KHÔNG copy nguyên profile 2.0 GB
  (IndexedDB 1.2 GB) vì /tmp là tmpfs/RAM, tránh OOM. Xoá ngay sau probe (verified).
- Headless CloakBrowser trên bản copy (`launch_backend=cloakbrowser`), page title `ChatGPT`
  → session sống: access_token 1866 chars, jar 33 cookies (đủ `_puid`, `oai-sc`,
  `__Secure-oai-is`, `__cf_bm`, `cf_clearance`, …).
- Sentinel mint in-page qua SentinelSDK: requirements 1932 / proof 637 / turnstile 12324 chars.
- Jar đọc LẠI sau khi mint để `oai-sc` đôi với requirements token vừa mint.
- curl_cffi 0.16.1, target `chrome146`; UA = CLOAKBROWSER_USER_AGENT (Chrome/146).

## Ghi chép nguyên văn (status + body đầu 500 chars, đã redact)

### Handshake `POST /backend-api/f/conversation/prepare`

| Attempt | Path/Client | Status | Body (≤500 chars) |
|---|---|---|---|
| H1 | curl_cffi chrome146, full header set, NO body | **422** | *(body rỗng qua reader lỗi — xem ghi chú)* |
| H2 | in-page fetch (dead-code style, `/backend-api`) | **422** | `{"detail":[{"type":"missing","loc":["body"],"msg":"Field required","input":null},{"type":"missing","loc":["body"],"msg":"Field required","input":null}]}` |

→ Endpoint prepare **hiện yêu cầu body bắt buộc**; không lấy được `conduit_token` theo cách
nào → cả hai conversation POST bên dưới đi ra **KHÔNG có `x-conduit-token`**.

### Conversation POST #1 (tag `pong-one`)

- `status=403`, `content-type=application/json`
- Body đầu 500 chars: *(không thu được — lỗi reader script: curl_cffi 0.16 Response không có
  `aiter_bytes`; repo dùng `_response_chunks()` fallback `aiter_content` nhưng script probe
  chỉ thử `aiter_bytes`. Status/CT ghi đúng nguyên văn; body 403 tham chiếu evidence cũ =
  JSON "Unusual activity".)*

### Conversation POST #2 (tag `pong-two`, biến thể provenance conduit)

- `status=403`, `content-type=application/json` — cùng hạn chế body như trên.

Ghi chú trung thực: 2 conversation POST đã dùng HẾT budget nên KHÔNG chạy lại chỉ để đọc
body lỗi (tôn trọng ràng buộc ≤2 request và không cạnh tranh quota với agent đang bắn turn
qua gateway). Không thấy 429 trong toàn bộ run.

## Verdict

**403 ×2 → f/conversation authed VẪN CHẾT cho pure HTTP/curl_cffi**, kể cả khi gần-full shape:
sentinel trio tươi in-page, jar 31–33 cookies (có oai-sc pair), client hints, correlation
headers, enriched body 24 keys, TLS chrome146. Điểm khác biệt duy nhất so với trang thật:
**không có `x-conduit-token`** (handshake bất khả thi hôm nay).

## Những gì còn thiếu khả nghi (theo thứ tự)

1. **Conduit token + schema `/f/conversation/prepare` đã đổi.** Capture 24/8 ghi "no body →
   200"; hôm nay 422 FastAPI-style đòi `body` (×2 loc) trên cả curl_cffi lẫn in-page fetch.
   `custom-gpt-pilot-2026-08-24.md` ghi prepare mang `client_prepare_state`;
   `live-protocol-findings` cho thấy prepare chính là nơi submit turn (stream chuyển WS topic
   `"conversations"`). Schema chuẩn chưa biết → không tự chế mù (tốn quota).
2. **Kiến trúc stream đã dời khỏi POST SSE.** Nếu client thật submit qua prepare + nhận stream
   qua WebSocket thì POST `/f/conversation` chờ SSE là đường legacy — shape hoàn hảo cũng có
   thể bị chặn tầng khác (khớp custom-gpt-pilot: in-page fetch đầy sentinel vẫn 403).
3. Hằng số phụ có thể stale: `oai-client-version`/`oai-client-build-number` lấy từ capture
   24/8; `x-oai-is-client-observation`/`oai-echo-logs` sinh tổng hợp; presets trong
   `model_response_contracts` đơn giản hoá `[]`. Bậc hai — khó phải nguyên nhân chính khi #1 thiếu hoàn toàn.

## Kết luận & hệ quả

Bằng chứng mới củng cố quyết định bỏ f/conversation cho authed trên đường HTTP thuần
(khuyến nghị A2 trong hybrid-auth-research): chuyển sang `POST /backend-api/codex/responses`
(spec `codex-sse-spec-2026-08-25.md`, cần verify live riêng) hoặc giữ browser transport cho
account authed. Không đầu tư reverse thêm prepare-contract nếu chưa có capture Burp mới
cho thấy body thật của prepare.
