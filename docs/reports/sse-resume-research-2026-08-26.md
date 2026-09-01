# SSE-RESUME RESEARCH — số phận nhánh f/conversation + cơ chế resume (2026-08-26)

Phạm vi: research-only (repo READ-ONLY, web fetch 26/8/2026). Câu hỏi đặt ra bởi
CONDUIT-PROBE (25/8): prepare 422 + conv 403 ⇒ "schema đổi, đường chết"?

## TL;DR — VERDICT: **SỐNG (có điều kiện)**

Kết luận 422 của probe là **thiếu body**, không phải schema chết — recipe hiện hành
**luôn đòi body** và **≥2 dự án độc lập xác nhận prepare authed trả 200** trong 8/2026.
Probe 25/8 bắn H1/H2 đều **KHÔNG có body** nên 422 là hành vi đúng của endpoint,
chưa phủ định được recipe. Conv POST 403 xảy ra **không có x-conduit-token**
(handshake fail do không gửi body) — chưa phải phán quyết của đường có đủ conduit.
Chưa đủ căn cứ bỏ nhánh trước 1 replay đúng recipe ≤4 request.

## Q(a) — Schema `/f/conversation/prepare` hiện nay

| Nguồn | Ngày | Bằng chứng |
|---|---|---|
| `5yu4n/gptweb2api` `internal/chatgpt/{sentinel,client}.go` + `docs/research/chatgpt-conversation-prepare.md` | repo push 2026-08-24T13:55Z; live-validate streaming **200 ngày 23–24/8** (PROJECT_STATUS, dẫn trong ws-stream-research-2026-08-25) | prepare **bắt buộc body 15-field** (action, client_contextual_info, client_prepare_dispatch/source/state, conversation_mode, local_function_names, model, parent_message_id, supported_encodings, supports_buffering, system_hints, timezone, timezone_offset_min ± conversation_id) → 200 `{conduit_token (~350 chars), status}` |
| `kymuco/chatgpt-web-adapter` PR #40+#41 (**merged 2026-08-11**) | repo push 2026-08-26T05:22Z | Live probe authed GPT-5.6: `POST /backend-api/f/conversation/prepare` → **HTTP 200, status=ok, conduit_token present**; body gồm `partial_query` (user-message-shaped, id == message id cuối) — field MỚI ngoài bộ 15; header khởi đầu **`x-conduit-token: no-token`** (literal marker — giải đáp [CẦN VERIFY] #1 của f-conversation-recipe-fields.md) |
| `realasfngl/ChatGPT` qua deepwiki (index 18/11/2025) | 2025-11-18 | Variant anon `/backend-anon/f/conversation/prepare` → `conduit_token` dùng làm `x-conduit-token` — contract tồn tại ít nhất từ 11/2025 |

Hệ quả: "schema đổi" của probe đọc sai tín hiệu — endpoint đòi body là trạng thái
BÌNH THƯỜNG theo client thật. Hai biến thể body cùng được chấp nhận (gptweb2api
không gửi partial_query mà vẫn 200; kymuco gửi và 200).

### Contract sentinel hai pha (mới quan sát, kymuco #41)

Thứ tự browser thật (authed, 8/2026):
```
POST /backend-api/f/conversation/prepare        (x-conduit-token: no-token)
POST /backend-api/sentinel/chat-requirements/prepare   {"p": ...}
POST /backend-api/sentinel/chat-requirements/finalize
POST /backend-api/f/conversation                (client_prepare_state:"success")
```
Response requirements/prepare: `persona`, `prepare_token`,
`turnstile{required,dx}`, `proofofwork{required,seed,difficulty}`,
**`so{required,collector_dx,snapshot_dx}`** (mới). Finalize request:
`{prepare_token, proofofwork, turnstile}`; finalize response:
`{persona, token, expire_after, expire_at}`.
Lưu ý: gptweb2api đi sentinel-TRƯỚC-prepare và vẫn live 200 ⇒ thứ tự chưa bị
server cứng hóa; giữ nguyên thứ tự hiện có của repo, ghi nhận khác biệt.

## Q(b) — RESUME stream sau đứt

Endpoint `POST https://chatgpt.com/backend-api/f/conversation/resume` — **3 nguồn
độc lập khớp byte-level**:

1. **gptweb2api `internal/chatgpt/resume.go`** (raw fetch 26/8/2026): body
   `{conversation_id, offset}`; offset thử tuần tự **0→1→2**, chỉ retry khi 404;
   headers = base envelope + **`X-Conduit-Token` lấy từ SSE event
   `resume_conversation_token`** (kèm conversation_id) + `X-OAI-Turn-Trace-Id`
   của turn gốc; theo handoff tối đa 64 lần (`defaultMaxStreamHandoffs`),
   break khi lặp token/conversation; decoder giữ phiên để resumed frames
   patch lên cây tin nhắn cũ (reconcile chứ không suppress); Accept:
   text/event-stream; 401 → invalidate token.
2. **OmniRoute `open-sse/executors/chatgpt-web/handoff.ts`** (repo push
   **2026-08-26T05:45Z**): cùng endpoint/body/`RESUME_OFFSETS=[0,1,2]`/
   404-retryable/`x-conduit-token: resumeToken`.
3. **ratacat/pro-cli `docs/chatgpt-web-api-handbook.md`** (quan sát 8/5 +
   29/7/2026): `/f/conversation/resume` "supports long streams and reconnects";
   xác nhận event `{"type":"resume_conversation_token", ...}`.

GitHub code-search `"f/conversation/resume"` (26/8/2026): **79 hits** đa repo
(thêm codex-app-transfer 8/24, NiakGPT 8/26…). Bản chất: resume là cơ chế cho
**stream dài bị server chia đoạn** (SSE kết thúc `[DONE]` nhưng còn phát
resume_conversation_token) — không phải poll conversation_id; last-event-id
không xuất hiện trong nguồn nào.

## Q(c) — Endpoint thay thế cho account authed

Không tìm thấy đường stream chat mới ngoài `f/conversation` (SSE) và
`codex/responses` (agent-only). Bằng chứng cập nhật nhất: pro-cli vẫn dùng
f/conversation tại catalog refresh **29/7/2026**; kymuco merge ngày 11/8;
gptweb2api live 23–24/8; OmniRoute executor push hôm nay 26/8. Các project cũ:
chat2api đứng yên từ **2025-05-17**, go-chatgpt-api **archived 2024** — hết giá trị
làm nguồn. WS codex/responses giữ nguy cơ close 1008 policy (đã chốt ở
ws-stream-research). sub2api (active 26/8) không dùng f/conversation (0 hits) —
không liên quan.

## Đối chiếu code sẵn có — thiếu mảnh gì

Đã có trong working tree (uncommitted, flag `WEBGPT_FCONV_PREPARE` default OFF):
bootstrapProof local + PoW SHA3-512 (`asyncio.to_thread`), requirements/prepare +
classic fallback, `build_fconv_prepare_body()` đủ 14+1 field, conduit non-fatal,
per-turn oai-session-id/x-oai-turn-trace-id, accept-language/no-cache,
invalidate-Bearer khi 401/403 (codexfix-c, codex13fix-a).

Thiếu:
1. **`X-Conduit-Token: no-token`** trên prepare call (`_prepare_fconv_turn`,
   curl_transport.py:452) — marker literal kymuco quan sát; chưa gửi header này.
2. **Resume handoff**: parser SSE đang **BỎ** event `resume_conversation_token`
   (curl_transport.py:2011 liệt kê vào nhóm ignore) — không theo được stream dài
   bị chia đoạn. Endpoint resume chưa tồn tại trong code.
3. `partial_query` trong prepare body (biến thể kymuco) — chưa cần vì gptweb2api
   chứng minh không bắt buộc; thêm chỉ khi prepare 400/422 dù đủ 15 field.
4. X-OAI-IS passthrough — gap chung mọi project (gptweb2api cũng chưa mô hình hoá).
5. Chưa live-verify chain sau khi port (probe 25/8 chạy TRƯỚC khi có body/PoW local).

## Đề xuất ROADMAP

| ID | Scope | File | Flag | Nội dung + rủi ro |
|---|---|---|---|---|
| **FCONV-NOTOKEN-REPLAY** | **S** | `gpt/transport/curl_transport.py::_prepare_fconv_turn` (+script debug tái dùng `_prepare_fconv_turn`) | `WEBGPT_FCONV_PREPARE` (đã có) | Thêm header `X-Conduit-Token: no-token` vào prepare call, rồi live replay thang ≤4 request (requirements→PoW→prepare→conversation). **Tiêu chí chốt sống/chết**: prepare 200 + conduit nhưng conversation vẫn 403 ⇒ đóng nhánh vĩnh viễn, dồn lực CODEX-SSE OAuth. Rủi ro chính: reputation IP/thiết bị (row SENTINEL-SDK từng ghi auth-403 do reputation) — nếu vậy thử 1 lần từ profile/IP khác trước khi kết luận; turnstile có thể bị enforce bất kỳ lúc nào. |
| **FCONV-RESUME-HANDOFF** | **M** | `curl_transport.py` parser :2011 (giữ token+conversation_id thay vì drop) + hàm follow handoff POST `/f/conversation/resume` (offsets 0→2, cap 64, chỉ nối khi `[DONE]`-mà-còn-token, decoder continuity) | mới `WEBGPT_FCONV_RESUME` default OFF | Tăng độ bền stream dài; chạy chỉ khi event xuất hiện nên rủi ro thấp; làm SAU khi row trên xác nhận đường sống. |

## Nguồn (tất cả fetch/verify 26/8/2026)

- https://github.com/5yu4n/gptweb2api — resume.go (raw), repo pushed_at 2026-08-24T13:55Z, 0 issues; live 23–24/8 theo PROJECT_STATUS (dẫn trong ws-stream-research-2026-08-25.md)
- https://github.com/kymuco/chatgpt-web-adapter — PR #40 (merged 2026-08-11T12:30:17Z), PR #41 (merged 2026-08-11T12:30:37Z); repo pushed_at 2026-08-26T05:22Z
- https://github.com/diegosouzapw/OmniRoute — open-sse/executors/chatgpt-web/handoff.ts; pushed_at 2026-08-26T05:45Z; issue #8813 (Sentinel/Turnstile block, 27/7) + PR #9703 (classify SENTINEL_BLOCKED 403)
- https://github.com/ratacat/pro-cli — docs/chatgpt-web-api-handbook.md (quan sát 8/5 + 29/7/2026); pushed_at 2026-07-31
- https://deepwiki.com/realasfngl/ChatGPT/9-openai-backend-endpoints — index 18/11/2025 (anon conduit flow)
- gh api code-search `"f/conversation/resume"` — 79 hits (26/8/2026)
- Nội bộ: docs/reports/conduit-probe-2026-08-25.md · docs/reports/ws-stream-research-2026-08-25.md · docs/reports/f-conversation-recipe-fields.md · gpt/transport/token_manager.py · gpt/transport/curl_transport.py (:452, :2011)
