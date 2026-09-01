# Hybrid transport cho account authenticated — research 2026-08-25

Bối cảnh: flip `--transport browser→hybrid` THẤT BẠI hôm nay với account personal
(authenticated): T2 đỏ ×2, model emit protocol chuẩn nhưng turn chết. Rollback browser OK.
Nhiệm vụ: tìm đường làm hybrid sống cho account đã đăng nhập. READ-ONLY.

## 1. Diff fingerprint/headers: curl_transport vs browser thật

Nguồn: `docs/reports/header-diff-auth-2026-08-24.md` (capture byte-by-byte trang thật,
cùng profile/máy/IP) + đọc lại `gpt/transport/curl_transport.py::_build_headers` (:434-457).

**Kết luận then chốt đã có sẵn trong repo:** cùng profile, cùng máy, cùng IP — trang
thật POST `/backend-api/f/conversation` → **200 SSE**; curl_cffi đủ bộ sentinel → **403
"Unusual activity"**. Vậy 403 là **phân biệt theo hình dạng request**, KHÔNG phải ban
IP/device như FAILURES.md dòng 8 kết luận. Đây là mâu thuẫn evidence quan trọng nhất.

TLS layer: cf-clearance-lifecycle probe chứng minh curl_cffi `impersonate=chrome146`
(đúng target mới nhất 2026 mà curl_cffi khuyến nghị — chrome146 thêm ở v0.15, kèm HTTP/3)
+ UA Windows Chrome/146 replay cookie → 200 trên `/backend-api/models`. **JA3/JA4/H2
KHÔNG phải vấn đề.** UA hiện tại trong code cũng đã đúng (CLOAKBROWSER_USER_AGENT).

Những gì còn thiếu so với request thật (theo thứ tự nghi phạm trong report):

| # | Thiếu/different | Chi tiết |
|---|---|---|
| 1 | **x-conduit-token + handshake `/f/conversation/prepare`** | Trang thật gọi prepare (NO BODY) → JWT conduit TTL ≈60s, gửi kèm mọi turn. Repo có sẵn `TokenManager.prepare_conduit()` (token_manager.py:470) NHƯNG không ai gọi — dead code. Lưu ý: nó POST vào `/backend-anon/.../prepare` còn trang authed dùng `/backend-api/...`; FAILURES ghi probe tay bị 422 mọi shape (có thể do sai path/header) |
| 2 | Client hints | `sec-ch-ua`, `sec-ch-ua-platform`, `sec-ch-ua-mobile` — absent hoàn toàn; UA↔hints inconsistency là input bot-score kinh điển (JA4H tính cả header order) |
| 3 | Cookie envelope thiếu | Synthetic chỉ gửi `bundle.cookies` + cf_clearance; jar thật 28 cookies gồm `oai-sc` (đôi với requirements token — vỡ correlation server-side nếu thiếu), `__cf_bm`, `__Secure-oai-is`, `_puid`. `extract_all()` lấy TOÀN BỘ context.cookies() nên jar đầy đủ CÓ SẴN trong bundle — builder tự siết mất |
| 4 | Session-correlation headers | `oai-session-id` (ổn định cả page-session), `x-oai-turn-trace-id` (mỗi turn), `x-oai-is-client-observation`, `oai-client-build-number`, `oai-client-version`, `oai-echo-logs`, `x-openai-target-path/route`, `oai-telemetry` |
| 5 | Body shape | Thiếu ~15 field (`client_prepare_state:"sent"`, timezone pair, `client_contextual_info`, string-boolean `"True"...`); `parent_message_id` phải literal `"client-created-root"` thay vì uuid |

Sentinel tokens: proof/turnstile/requirements shape khớp — KHÔNG phải nguyên nhân.

## 2. Web research (curl_cffi / cộng đồng)

- curl_cffi targets mới nhất: **chrome146/safari260** là recommended (docs chính thức);
  repo đã dùng đúng. JA4 support có sẵn; custom fp qua `ja3=/akamai=/extra_fp=` nếu cần.
- Anti-bot hiện đại (Cloudflare JA4/JA4H, Akamai): điểm số gồm TLS hello + HTTP/2 frame
  + **thứ tự header** + consistency giữa UA/sec-ch-ua/Accept-Language. curl_cffi chỉ giữ
  thứ tự default của target cho header NÓ tự sinh; header user-supplied append theo dict
  order → bộ header tự chế thiếu hints là tín hiệu yếu rõ.
- **Finding lớn nhất:** cộng đồng đã gặp CHÍNH 403 "Unusual activity" này và xử lý bằng
  cách RỜI endpoint `/f/conversation`: dùng `POST https://chatgpt.com/backend-api/codex/responses`
  — endpoint KHÔNG gate Turnstile (lỗi trả về 400 backend-level thay vì 403), auth bằng
  session-token/access-token thường, chỉ cần curl_cffi chrome131+ TLS + 2 header đặc biệt:
  `OpenAI-Beta: responses=experimental`, `originator: codex_cli_rs`, bắt buộc
  `stream: true`. Ref: github.com/Kitjesen/chatgpt-to-api (Feb 2026, hoạt động),
  Securiteru/codex-openai-proxy (payload dạng Responses API: `instructions` + `input[]`
  typed items + `store:false`). Nghĩa là đường HTTP thuần cho account PAID tồn tại mà
  không cần conduit/full client emulation.

## 3. Đánh giá 3 hướng

| Hướng | Effort | Rủi ro | Nhận xét |
|---|---|---|---|
| **A. Align fingerprint curl_cffi ↔ CloakBrowser** | M–L | Trung bình–cao | TLS/UA đã align từ trước; phần còn lại là A1: wire `prepare_conduit()` (đã có sẵn!) + x-conduit-token + hints + full jar + correlation headers + enriched body. Arms-race dài; schema `/prepare` còn 1 điểm chưa ngả (FAILURES ghi 422 vs capture thật no-body 200). **A2 (khuyến nghị hơn): đổi endpoint sang `/backend-api/codex/responses`** — bỏ cả rừng header/f/conversation; cần viết payload Responses-API + parser SSE mới (`response.output_text.delta`...) |
| **B. MIXED ROUTING qua MultiAccountWorkerFactory** | S (wire) | Thấp | Khả thi kỹ thuật: `factories: Mapping[str, Any]` chỉ cần `.lease/.start/.close/.stats/.browser_manager` — api/server.py (:415-437) chọn class uniform, chỉ cần sửa vòng lặp để chọn ChatGPTWorkerFactory cho account authed + Hybrid cho anon. NHƯNG giá trị hạn chế: CurlCffiTransport raise AuthRequired khi thiếu access_token (curl_transport.py:431) nên account ANON cũng không chạy được hybrid → B thực chất chỉ quay về status quo (authed→browser). Dùng được như công cụ vận hành an toàn, không giải quyết "hybrid sống cho authed" |
| **C. Cải thiện reputation (warm-up, tần suất)** | S | Thấp | Bị evidence phản bác: 403 là request-shape-differentiated, không phải IP ban (trang thật cùng IP 200). Warm-browser-mint pattern đã implement (CF-RESILIENCE done). Chỉ giữ C như hygiene: không retry mù, không đổi IP giữa session |

## 4. Đề xuất ROADMAP

1. **CODEX-SSE (top, effort M)** — thêm nhánh endpoint codex vào hybrid:
   - Files: `gpt/transport/curl_transport.py` (hoặc `gpt/transport/codex_transport.py`
     mới tái dùng session/token_manager; `gpt/transport/token_manager.py` đã có đủ
     access_token/session cookies).
   - Headers: Bearer AT, `originator: codex_cli_rs`, `OpenAI-Beta: responses=experimental`,
     UA chrome146, impersonate chrome146 (giữ nguyên), `stream: true` bắt buộc.
   - Payload: Responses API (`instructions` + `input[]` typed items, `store:false`).
   - Parser: SSE events `response.output_text.delta` / `response.completed` — map vào
     TurnResult hiện có; tool-call của Responses API (function_call items) map tự nhiên
     hơn cả f/conversation.
   - Tests: `tests/test_codex_transport.py` — fake AsyncSession assert header set/order +
     parse fixture SSE; kill-switch env `WEBGPT_CODEX_ENDPOINT=1`.
   - Rủi ro: endpoint có thể yêu cầu plan phù hợp (evidence nói Plus); verify live 1 POST
     trước khi wire sâu.
2. **CONDUIT-PROBE (prereq rẻ, effort S)** — 1 lần live: replay đúng recipe header-diff
   (prepare no-body trên `/backend-api` + x-conduit-token + hints + jar đầy đủ) để chốt
   liệu A1 cứu được f/conversation hay không. Nếu 200 → A1 thành fallback khi codex hụt;
   nếu vẫn 403 → dứt khoát bỏ f/conversation cho authed.
3. **MIXED-AUTH-ROUTING (effort S, sau)** — server.py chọn factory per-account; hữu ích
   khi hybrid-codex sống: authed đi codex-HTTP, dev/mock đi hybrid hiện tại.
