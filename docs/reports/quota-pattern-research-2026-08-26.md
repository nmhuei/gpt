# QUOTA-PATTERN-RESEARCH — pattern hạn mức ChatGPT Web cho batch/SOAK scheduling (2026-08-26)

Research agent, READ-ONLY code. Mọi claim kèm nguồn + ngày lấy (tất cả fetch
ngày **2026-08-26** trừ khi ghi khác). Nguồn mâu thuẫn được ghi cả hai phía.

## 0. TL;DR chính sách đề xuất

| Tham số | Đề xuất | Căn cứ |
|---|---|---|
| Turn-message/giờ/account | ≤ 8–10 duy trì, burst ≤ 25 trong 3h | Plus ≈ 40 msg/3h (§B1) |
| Task agentic lớn (CTF) | tối đa 1–2 task/ngày/account, cách nhau ≥ 4h | 1 task 30–60 turn ăn gần hết cửa sổ 3h |
| Preflight trước batch | BẮT BUỘC đọc quota (wham/usage hoặc Settings UI) | §A, §D |
| Ngưỡng dừng sớm | primary ≥ 70% hoặc secondary ≥ 50% → không mở batch mới | §C3 |
| Khung giờ batch | 03:00–07:00 sáng local (giữ kế hoạch 03:33) | bằng chứng yếu, xem §B6 |
| Phân loại block | xem bảng §D — cooldown phút vs park giờ/ngày | §D |

---

## A. Endpoint/biến expose remaining-quota (đối chiếu usage_poller.py)

### A1. `GET https://chatgpt.com/backend-api/wham/usage` — XÁC MINH ĐÚNG SHAPE ✅

Struct chính thức từ chính source OpenAI (`openai/codex`, file
`codex-rs/codex-backend-openapi-models/src/models/rate_limit_status_details.rs`
và `rate_limit_window_snapshot.rs`, fetch 2026-08-26):

```jsonc
// RateLimitStatusDetails
{
  "allowed": bool,
  "limit_reached": bool,
  "primary_window":   WindowSnapshot | null,
  "secondary_window": WindowSnapshot | null
}
// RateLimitWindowSnapshot
{
  "used_percent": int,            // 0..100
  "limit_window_seconds": int,    // 18000 = 5 GIỜ ; 604800 = 7 NGÀY
  "reset_after_seconds": int,
  "reset_at": int                 // unix timestamp tuyệt đối
}
```

- `primary_window` = cửa sổ **5 giờ** (18000s), `secondary_window` = **tuần**
  (604800s) — xác nhận độc lập bởi `JiangNanGenius/Codex-Enhance-Manager`
  `official_quota.py::_window_seconds_to_tier_name` (fetch 2026-08-26) và
  `Loongphy/codex-auth docs/api.md` ("5h or weekly value").
- Header cần: `Authorization: Bearer <OAuth access_token>` +
  `ChatGPT-Account-Id: <account_id>` (codex-auth docs/api.md). UA giả lập
  client (`codex_cli_rs/…`) được các tool dùng (`0xtbug/zero-limit
  src/constants/api.ts`).
- Payload thực tế còn thấy `credits.balance` và reset_at dạng ngày-giờ cụ thể
  (openai/codex#31174, 2026-07-05: primary 100% resets 01:38 CST, secondary
  100% resets 11:17 sau ~5 ngày, credits.balance 0).
- **Shape đang tiến hóa**: issue NGÀY HÔM NAY `veildawn/ai-provider-plugins#1`
  (2026-08-26) — parser chết vì field mới `additional_rate_limits` mang giá
  trị `null`. ⇒ poller nhà mình parse defensive là đúng hướng; KHÔNG thêm
  assert nghi ngặt vào field chưa cần.

Đối chiếu `gpt/transport/usage_poller.py`: `extract_used_percent()` đọc
`rate_limit.primary_window.used_percent` — **khớp canonical**, chấp nhận
float (superset của i32, an toàn). `reset_at`/`limit_window_seconds` đang bị
bỏ qua — xem §E đề xuất dùng chúng.

### A2. Endpoint song song `GET /backend-api/codex/usage`

OpenAI/codex#31174 gọi endpoint này với OAuth và cùng shape
(`rate_limit.primary_window.used_percent`). Có thể là alias cũ/mới của wham.
⇒ nếu wham 404 thử path này (env `WEBGPT_USAGE_URL` đã hỗ trợ override).

### A3. Chat Web API: KHÔNG có endpoint "remaining calls" đứng riêng ❌

`ratacat/pro-cli docs/chatgpt-web-api-handbook.md` (probe live trên account
Pro, fetch 2026-08-26) — toàn bộ candidate path đều 404/405:

```
/backend-api/conversation_limits_progress  404     /backend-api/me/quota        404
/backend-api/conversation_limit            404     /backend-api/usage           404
/public-api/conversation_limit/v2          404     /backend-api/rate_limits     404
/backend-api/me/limits                     404     /backend-api/limits_progress 404
(+ ~12 path nữa, nguyên văn trong handbook)
```

Kết luận nguyên văn: *"there is no standalone 'remaining calls' endpoint.
Per-feature counters only appear inside the SSE stream as
`conversation_detail_metadata.limits_progress` events"*.

- `/public-api/conversation_limit` VẪN SỐNG nhưng trả
  `{"message_cap": 0.0, …}` — bị zero, vô dụng (ít nhất trên Pro;
  handbook + extension `saeedezzati/superpower-chatgpt` chỉ đọc được
  field `message_cap`, không thấy counter đếm được).
- **SSE `conversation_detail_metadata.limits_progress`** (shape verbatim):

```json
{"limits_progress": [
  {"feature_name": "deep_research", "remaining": 250,
   "reset_after": "2026-06-07T18:34:14.421525+00:00"},
  {"feature_name": "image_gen", "remaining": 935, "reset_after": "...daily-looking..."}
], "model_limits": []}
```

  Chỉ là quota feature đặc biệt (deep_research/image_gen/odyssey), **KHÔNG
  phản ánh general chat cap**. Nếu gateway muốn bắt free-data này: hook vào
  record SSE `conversation_detail_metadata` trong `_consume_record`.

### A4. Fallback khả thi: UI Settings hiển thị usage %

openai/codex#33685 (2026-07-16): *"The same weekly usage value is also visible
in ChatGPT Settings"* ⇒ khi không có OAuth codex hợp lệ, DOM-driver có thể
scrape % usage từ Settings làm tín hiệu dự phòng (chưa ai spec hoá selector —
cần 1 lần capture thật).

---

## B. Pattern hạn mức cộng đồng 2025–2026

### B1. Plus chat ≈ 40 messages / 3 giờ (mức tin cậy trung bình-ca)

- `Octo-Lex/ChatGPT-Web2API src/chatgpt_web2api/guide.md` (repo sinh
  2026-06-06, push 2026-07-13 — reverse-proxy cùng domain với mình):
  *"Rate limits: Subject to ChatGPT Plus rate limits (~40 messages per 3
  hours for GPT-5.5)"*.
- Corroborate cũ hơn: docs Dayflow (mirror `mintlify-atlas/docs-atlas-7f6a6631
  configuration/chatgpt-claude.mdx`): *"ChatGPT Plus: Limited to a certain
  number of messages per 3 hours"*; chuỗi lỗi kinh điển web
  *"You've reached our limit of messages per 3 hours"* (72 repo lưu string
  này, ví dụ `Erol444/gpt4-openai-api driver.py` bản "per hour").

### B2. Free tier: 10 messages / 5h rồi fallback model nhẹ

`theexperiencecompany/gaia .../research/chatgpt.md` (dữ liệu soạn ~02–04/2026,
trích intuitionlabs.ai + felloai.com): Free = GPT-5.3 Instant,
*"10 messages every 5 hours"*. felloai.com (article updated **2026-08-23**) —
từ tuần 10/08/2026 free được *"removing the cap on text chats entirely"*.
⇒ hạn mức đang thay đổi liên tục; đừng hard-code con số.

### B3. Cấu trúc cấp Plan 2026

(felloai.com cập nhật 2026-08-23 + gaia research doc)

- Go $8: nhiều message hơn Free; Plus $20: *"high weekly limits"*, Deep
  Research 10 runs/tháng; Pro $100 (lễ ra mắt tier 09/04/2026): **5× Plus**;
  Pro $200: **20× Plus**.
- Business Premium seats: *"No five-hour usage limit"* + *"Predictable weekly
  usage resets"* ⇒ xác nhận chat-side cũng có **cửa sổ 5h + tuần** như codex.

### B4. Message-rate vs TOKEN-rate: quota tính THEO TOKEN, thinking nặng hơn

- openai/codex#28879 (2026-06-18, bảng số liệu thật): cùng plan/model/app,
  12/06 prompt 57K token ≈ **1%** budget 5h; 18/06 prompt 20K token ≈
  **10–27%**. Cost-per-token nhảy 10–20×. ⇒ server tính quota theo trọng số
  token/tuần tự, KHÔNG phải đếm tin nhắn thuần.
- Hệ quả cho mình: turn correction loop / context dài đốt quota nhanh gấp bội
  so với chat thường. Batch CTF (context lớn, nhiều turn) phải ngân sách như
  token, không phải như số turn.

### B5. Reset: mốc tuyệt đối (anchored), KHÔNG reset theo kỳ bill

- `reset_at` là unix timestamp tuyệt đối trong payload (§A1).
- openai/codex#39398 (2026-08-19): *"Weekly usage window is not reset by a new
  paid subscription period"* — cửa sổ tuần neo theo usage, không theo ngày
  thanh toán.
- Khi chạm trần, UI/payload luôn cho mốc hồi phục cụ thể (countdown đến
  reset_at). Không tìm thấy nguồn nào nói fixed-clock reset hằng giờ.

### B6. Giờ thấp điểm giảm nguy cơ flag/challenge — BẰNG CHỨNG YẾU

Không tìm được nguồn định lượng nào về khung giờ nên chạy. Chỉ có kinh nghiệm
truyền miệng cộng đồng reverse-proxy (cookie expiry, reputation IP). Đánh giá:
khung 03:33 sáng vẫn nên giữ vì lý do ít user thật cạnh tranh + ít Cloudflare
challenge, nhưng đây là giả định vận hành, không phải fact hạn mức.

### B7. Hành vi bất thường server-side — quota có thể "ma"

- openai/codex#31174 (2026-07-05): idle drain 79%→100% trong đêm KHÔNG có
  session nào; 1 prompt ăn 99% trong 6 phút; incident status.openai.com
  29/06/2026 marked-resolved nhưng tái diễn.
- openai/codex#31696 (2026-07-09): /status xen kẽ 2 giá trị quota decrementing
  trong cùng session.
- openai/codex#33685 (2026-07-16): đổi model → weekly tụt nhanh như 5h cũ.
- Kết luận: `used_percent` đôi khi NÓI DỐI. Breaker phải clamp (đã làm) +
  không park vĩnh viễn chỉ dựa trên 1 reading.

### B8. Cấu trúc cửa sổ thay đổi giữa các account (thí điểm A/B)

openai/codex#32791/#32840/#34402 (13–20/07/2026): một loạt Plus mất 5h window
trên /status, chỉ còn weekly; #38892 (16/08/2026): Plus chuyển sang mô hình
credits (1,000 credits) nhưng vẫn bị block dù weekly 0%. ⇒ KHÔNG giả sử mọi
account cùng cấu trúc hạn mức; preflight phải đọc payload thật từng account.

## C. Hành vi khi bị chặn

1. **HTTP**: POST conversation trả **429** (curl_transport.py:1577 nhà mình
   đã raise `RateLimited`). Challenge page thì 403/503 HTML
   (transport/challenge.py:129 — occasionally 429, dễ NHẦM với rate-limit).
2. **DOM/stream**: dialog/banner chữ *"You've hit your usage limit"* /
   *"reached our limit"* — drivers/ui.py:72-75 đã watch 2 string này; CLI
   surface string tương đương *"You've hit your limit"* (mintlify mirror).
3. **Thời gian hồi phục quan sát được**: ngắn = đến `reset_at` của primary
   (vài giờ); sâu = secondary weekly, đợi tới ngày reset (ví dụ thật #31174:
   cả hai window 100%, weekly còn ~5 ngày). Không có cơ chế mua unlock tức
   thời cho chat; credits chỉ áp codex-side.

## D. Chính sách phân loại block cho coordinator

| Tín hiệu tại thời điểm chặn | Chẩn đoán | Hành động |
|---|---|---|
| 429 đơn lẻ, primary < 90% & secondary < 80% | burst tạm (phút) | cooldown 15–30', probe lại half-open |
| Dialog DOM "usage limit" mà HTTP 200/403 | UI-block mềm | coi như 429, cooldown như trên |
| Payload primary ≥ 95% | cạn cửa sổ 5h | park đến `reset_at + 15'` |
| Payload secondary ≥ 95% | cạn TUẦN | park account, xếp lịch ngày reset |
| 429 lặp qua ≥ 2 probe cách 30' + không đọc được payload | coi là cạn dài | park ≥ 5h hoặc sang ngày |
| 403/503 + HTML challenge | KHÔNG phải quota | đường challenge.py, không đốt breaker quota |

Nguyên tắc: **preflight trước mọi batch** — 1 lần đọc §A (payload nếu có
OAuth codex, không thì DOM Settings §A4); primary ≥ 70% hoặc secondary ≥ 50%
⇒ dời batch, không bắn.

## E. Việc implement phát sinh — ĐỀ XUẤT ROW ROADMAP (không tự sửa ROADMAP.md)

```
| QUOTA-PREFLIGHT | Trước mỗi batch/turn-đầu đọc wham/usage (poller sẵn) hoặc DOM
  Settings fallback; ngưỡng primary≥70%/secondary≥50% ⇒ chặn mở phiên mới + expose
  qua stats | ops | TODO (M) |
```

LƯU Ý chặn: CODEX-SSE row đã chứng minh **AT web-session bị codex backend từ
chối (401)** ⇒ poller chạy bearer web-session sẽ 401 → tự mute vĩnh viễn.
Preflight chỉ sống khi (a) có `WEBGPT_CODEX_AUTH_JSON` OAuth thật, hoặc
(b) fallback DOM Settings. Cần quyết coordinator trước khi wire.

```
| LIMIT-SIGNATURE-TAXONOMY | Phân lớp 429-vs-challenge(403/503 HTML)-vs-DOM-dialog
  trước khi nuôi breaker; chỉ 429 thuần được tính trip (hiện challenge.py có thể
  nhầm 429) | transport | TODO (S) |
| RESET-AWARE-COOLDOWN | Dùng reset_at/window_minutes đang bị ignore trong
  extract_used_percent: cooldown = min(cooldown, reset_at−now+buffer); skip advisory
  open nếu window sắp reset | transport | TODO (S) |
```

## Nguồn (toàn bộ fetch 2026-08-26)

1. github.com/openai/codex — `codex-backend-openapi-models/src/models/rate_limit_status_details.rs`, `rate_limit_window_snapshot.rs` (struct canonical)
2. github.com/openai/codex issues #28879 (token-weighted drain), #31174 (idle drain, credits, reset_at), #31696, #33685, #39398, #32791/#32840/#34402, #38892
3. github.com/JiangNanGenius/Codex-Enhance-Manager `official_quota.py` (parser wham)
4. github.com/Loongphy/codex-auth `docs/api.md` (headers + refresh rules)
5. github.com/0xtbug/zero-limit `src/constants/api.ts` (URL + headers table)
6. github.com/ratacat/pro-cli `docs/chatgpt-web-api-handbook.md` (live-probe 20 endpoint quota + SSE limits_progress)
7. github.com/Octo-Lex/ChatGPT-Web2API `src/chatgpt_web2api/guide.md` (Plus ~40 msg/3h GPT-5.5)
8. github.com/theexperiencecompany/gaia research/chatgpt.md (plan structure 2026)
9. felloai.com chatgpt-pricing-guide (updated 2026-08-23, fetch qua WebFetch)
10. github.com/mintlify-atlas/docs-atlas-7f6a6631 configuration/chatgpt-claude.mdx (error strings)
11. github.com/veildawn/ai-provider-plugins#1 (2026-08-26, additional_rate_limits null)
12. superpower-chatgpt interceptor.js + cyfung1031 userscript + allaboutevemirolive/chatgpt-reverse (message_cap legacy)

Hạn chế nghiên cứu: WebSearch tool hỏng hoàn toàn trong phiên (mọi query trả
rỗng) ⇒ Reddit/help.openai.com trực tiếp không lấy được (bot-block 403);
Wayback 429. Số "160 msg/5h" lan truyền 2025 KHÔNG tìm được nguồn kiểm chứng
nào trong phiên này — không đưa vào kết luận. Con số 40/3h là ước cộng đồng
một nguồn chính + corroborate gián tiếp, độ tin cậy TRUNG BÌNH; cấu trúc
(5h+weekly, token-weighted, reset_at tuyệt đối) là CAO vì khớp source chính
thức OpenAI.
