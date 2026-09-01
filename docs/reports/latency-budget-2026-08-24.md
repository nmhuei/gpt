# Phân tích tĩnh: Ngân sách độ trễ login + gửi prompt (2026-08-24)

Phạm vi: phân tích **tĩnh** (đọc code, không chạy network, không auto-login thật).
Mục tiêu: liệt kê mọi độ trễ cố định có thể cắt trên pipeline
`launch → login → bootstrap → position → send → first-delta`.

File đã đọc:

- `gpt/auth/authenticator.py` (`AutoLoginManager`, `step_delay_scale=1.0`)
- `gpt/drivers/ui.py` (`UIDriver.send`, sau khi hạ `poll=0.12`/`stable_grace=0.45`/env)
- `gpt/transport/session.py` (`ChatGPTWebSession.create`)
- `gpt/gateway/runtime.py` (`CompletionRuntime.position_session`)
- `gpt/drivers/protocol.py` (đối chiếu fastpath in-page fetch)

---

## 1. Login flow (`authenticator.py`)

Kịch bản tiêu biểu: email 25 ký tự, password 12 ký tự, OTP 6 số, mọi selector
xuất hiện tức thì (best case mạng), tài khoản có 2FA.

### 1.1 Bảng chi phí cố định

| # | Bước | Cơ chế | Chi phí (min–max) | Cắt được? | Cách cắt an toàn |
|---|------|--------|-------------------|-----------|------------------|
| 1 | `goto chatgpt.com` | `domcontentloaded`, cap 45s | network-bound | Không (bắt buộc) | Profile persistent + CDP tái dùng phiên đã đăng nhập → bỏ toàn bộ flow |
| 2 | Pause sau goto | `_pause(2.0)` cố định | **2.0s** | Có | Đã có `_is_authenticated_page()` check ngay sau đó; giảm về 0.3–0.5s hoặc poll ngắn |
| 3 | Click "Log in" → nav | `_wait_for_navigation` poll 0.5s + `_pause(1.0)` sau | 0.25s (avg granularity) + **1.0s** | Có | Poll 0.1s; bỏ pause 1.0s (nav-wait đã là điều kiện) |
| 4 | Chờ ô email | `wait_for(visible, 1000ms)` + `_pause(0.5)/miss` | ~0 (hit đầu tiên) | — | OK, event-driven rồi |
| 5 | Gõ email | `random.uniform(20,40)ms` × 25 ký tự | **0.50–1.00s** (mean 0.75) | Có (giữ random) | Hạ range `uniform(6,14)ms` → mean ~0.25s; vẫn jitter chống heuristic |
| 6 | Pause sau gõ email | `_pause(0.5)` | **0.5s** | Có | 0.1s đủ cho React state settle |
| 7 | Submit email | `_click_submit_button`: pause trong **3.0s** + pause ngoài **3.0s** | **6.0s** | Có (trùng lặp!) | Hai pause chồng nhau; thay cả hai bằng wait-for-URL-change/ô-password xuất hiện → ~0.2s |
| 8 | Chờ ô password | `wait_for(750ms)` + `_pause(0.5)/miss` | ~0 | — | OK |
| 9 | Gõ password | `uniform(25,45)ms` × 12 | **0.30–0.54s** (mean 0.42) | Có | `uniform(8,18)ms` → mean ~0.16s |
| 10 | Pause sau gõ pass + submit | `_pause(0.5)` + click trong **3.0s** + ngoài **4.0s** | **7.5s** | Có | Như #7: event-driven, giữ 1 verify ngắn → ~0.2s |
| 11 | Check 2FA | Loop `wait_for(1500ms)` + `_pause(0.25)`. **Nếu KHÔNG cần 2FA**: chạy đến `grace_deadline = 10.0s` mới thoát | Có OTP: ~0. Không OTP: **~10.0s cháy cố định** | Có (case không OTP) | Gate theo URL: nếu sau submit password URL rời auth domain mà không khớp `_MFA_URL_RE` trong ~2–3s → break sớm. Case có OTP: giữ hard_deadline |
| 12 | Gõ OTP | `uniform(25,50)ms` × 6 | **0.15–0.30s** | Có (cẩn trọng hơn) | Giữ random nhưng `uniform(10,22)ms`; OTP ngắn nên tiết kiệm ít — ưu tiên thấp |
| 13 | Pause + submit OTP | `_pause(0.5)` + click trong **3.0s** + ngoài **5.0s** | **8.5s** | Có | Event-driven: đợi redirect về chatgpt.com thay vì 8s ngủ → ~0.3s |
| 14 | Wait landing | `asyncio.sleep(1.0)` poll tới `_is_authenticated_page` (cap `timeout_seconds=120`) | avg +0.5s granularity; max 120s | Granularity có | Poll 0.2s hoặc `page.wait_for_url("chatgpt.com/**")` |

### 1.2 Tổng trễ cố định lý thuyết

Thành phần chỉ tính sleep/pause/granularity (chưa gồm network):

| Thành phần | Min | Max thực tế (flow thành công) |
|---|---|---|
| Pause cố định (#2,3,6,7,10,13,14-granularity) | **25.75s** | như min (không phụ thuộc mạng) |
| Typing (email+pass+OTP) | **0.95s** | **1.84s** |
| Burn grace không-OTP (#11) | 0 (có 2FA) | **~10s** (không 2FA) |
| **Tổng 1 login 2FA tiêu biểu** | **≈ 26.7s** | ≈ 27.6s |
| **Tổng 1 login không 2FA** | ≈ 36.7s | ≈ 37.6s |

Timeout trần (chỉ khi lỗi): navigation 20s · email 10s · password 35s · MFA 60s · landing 120s (tổng `login(timeout_seconds=120)`).

### 1.3 Mức sàn sau tối ưu

Giữ nguyên random typing (range hạ), thay mọi pause cứng bằng điều kiện
(URL-change, selector kế tiếp xuất hiện), gate grace theo URL:

> **Sàn lý thuyết ≈ 1.0–1.5s typing + ~1s verify + network ≈ 3–6s/login**
> (từ ~27–37s → cắt ~21–31s, ~80%).

Lưu ý an toàn: typing delay là giả lập người. Đề xuất mức thấp nhất vẫn
giữ `random.uniform` và giữ tốc độ < gõ máy (≥ ~60 ký tự/phút tương đương
10–16ms/ky tự); không dùng `fill()` đồng loạt cho auth form vì Auth0 có
heuristic nhập-liền.

---

## 2. Send path (`drivers/ui.py`) — phần còn lại sau khi Perf đã hạ poll/grace

Đã hạ: `DEFAULT_POLL_INTERVAL=0.12`, `DEFAULT_STABLE_GRACE=0.45`, env-tunable.
`Locator.is_visible(timeout=…)` trả về gần-tức-thì nên các sweep selector
không tốn timeout, chỉ tốn round-trip CDP (~0.5–2ms mỗi cái).

| Hạng mục | Cơ chế | Chi phí còn lại | Cắt được? | Cách cắt |
|---|---|---|---|---|
| `dismiss_popups` | Cache theo URL + listener `framenavigated` reset cache | Sweep 5 selector chỉ lần đầu sau mỗi navigation; các send sau = ~0 | Đã tối ưu | Không cần |
| `get_composer` | Sau check editable có **`asyncio.sleep(0.25)` vô điều kiện** | **0.25s mỗi lần lấy composer (mỗi send)** | Có | Sleep chỉ khi editable=False ở lần quét trước, hoặc hạ 0.05s |
| Effort re-check trong `send` | Nếu effort chưa confirmed: `_first_visible(MODEL_PICKER_SELECTORS, 1_000)` | **0–1.0s** nếu pill không render ngay | Có | Confirm effort 1 lần ở bootstrap (`SendRequest.reasoning_effort` đã có cơ chế confirmed — đảm bảo session set nó trước send) |
| File attach | `asyncio.sleep(1.5)` sau `set_input_files` | **1.5s** khi có files | Có (chỉ khi dùng files) | Đợi upload widget biến mất thay vì ngủ |
| `select_model` slug path | `goto(?model=slug)` + `sleep(0.5)` | **0.5s** khi đổi model qua URL | Có | Đợi pill text phản ánh slug thay vì 0.5s |
| `capabilities()` | `auth_status` + picker probes + `list_models` (sleeps 0.2 + 0.3) + effort discovery (sleeps 0.2+0.25+0.25) | **~0.7–1.2s sleeps** + nhiều RPC | Có | Lazy/defer: chỉ gọi khi client hỏi models; hoặc cache TTL |
| Poll-loop mỗi tick (120ms) | `_raise_known_page_error` (21 locator) + stop-button (4) + `_assistant_count` (≤7 count) + `_extract_latest_response` (≤7 inner_text) + `_composer_usable` | ~30–40 CDP round-trips/tick → có thể vượt 120ms tick, trễ delta | Có | Gộp 1 lần `page.evaluate()` trả JSON {count,text,stop,errors} → 1 round-trip/tick |
| Tail hoàn tất | `stable_grace=0.45` sau delta cuối | +0.45s cuối mỗi turn | Env-tunable | Đã có env; có thể hạ 0.25 khi stream network-listener đang hoạt động |

---

## 3. Bootstrap (`transport/session.py:create`)

Trình tự hiện tại: `manager.start()` → `new_page()` → `goto(domcontentloaded, cap 45s)`
→ **`wait_for_load_state("load", 10s)` (serial)** → `dismiss_popups`
→ poll `auth_status` mỗi **0.5s**, deadline **15s** (tối đa 30 vòng, mỗi vòng 3 sweep)
→ `capabilities()` (~1s).

| Bước | Bắt buộc? | Chi phí | Song song/bỏ được? |
|---|---|---|---|
| Browser start + page | Bắt buộc | 0.5–3s (cold); ~0 với CDP attach vào browser sống | Persistent pool tránh cold-start |
| `goto` domcontentloaded | Bắt buộc | network 1.5–4s | Không |
| `wait_for_load_state("load", 10s)` | **Không bắt buộc** | 0–10s; SPA hay fire `load` muộn | **Song song/bỏ được**: auth_status + get_composer đã gate hydration; chuyển sang chạy nền, không chặn |
| `dismiss_popups` | Rẻ | ~0 (is_visible tức thì) | Có thể chạy song song với auth poll |
| Auth-status poll | Bắt buộc (phát hiện login wall/rate-limit) | 0.5s granularity, avg +0.25s; worst 15s khi `blocked` | Thay poll bằng `locator.wait_for(state="visible")` trên composer → event-driven, bỏ granularity |
| `capabilities()` | Không bắt buộc cho send mặc định | ~1s | **Defer**: chạy nền sau READY, hoặc lazy tại lần đầu cần model info |

Bootstrap hiện tại điển hình ~4–10s; sàn ≈ goto (1.5–3s) + 1 event-driven wait ≈ **2–3.5s**.

---

## 4. Position (`gateway/runtime.py:position_session`) — góc nhìn fastpath

Hiện trạng:

- Có `conversation_id` → chỉ `session.open()` khi `session.conversation_id != record.conversation_id` (đã skip đúng cho turn cùng hội thoại).
- Không conv id → chỉ `new_conversation()` khi đổi `_active_gateway_session_id` (đã có memo).
- `select_model`/`select_reasoning_effort` được session-level memo hoá (`_selected_model_matches`).

Nghĩa là với **worker affinity** (sticky worker theo `record.session_id`),
position ổn định = ~0s/turn sau turn đầu.

Góc fastpath bổ sung: `ProtocolDriver` (replay qua `window.fetch` in-page,
hiện `available=False` vì chưa có fingerprint verified ≥2 experiments) khi
kích hoạt sẽ loại hẳn nhu cầu position-per-turn:

- Tạo conversation bằng POST `/backend-api/conversation` không cần navigate;
  `conversation_id` do client chọn → `open()` chỉ cần đúng origin, không cần đúng trang.
- Vị trí DOM không còn ý nghĩa giao thức → position hội tụ về **1 lần duy nhất
  lúc lease bootstrap** (đủ để có fetch context + token); mọi turn sau zero-position.
- Fallback UI vẫn cần DOM, nhưng DOM tồn tại xuyên turn trên cùng page →
  cũng chỉ position lại sau navigation/recovery, không phải mỗi turn.

Rủi ro: fingerprint chưa verified; khi bật cần gate như hiện tại
(`probe_protocol_compatibility` + evidence ledger), không bật shortcut trước.

---

## 5. Tổng hợp pipeline

| Giai đoạn | Hiện tại (từ code, điển hình) | Sàn lý thuyết | Cách đạt | Rủi ro |
|---|---|---|---|---|
| Launch | 0.5–3s cold; ~0 nếu CDP attach | ~0 | Browser pool / persistent context sống xuyên request | Zombie process, profile lock |
| Login | **26.7–37.6s** (2FA / không 2FA), chưa kể network | **3–6s** | Bỏ pause đôi (#7/#10/#13), URL-gate grace không-OTP, hạ typing range giữ random, poll granularity 0.1–0.2s | Bot-detection heuristic tăng nếu typing quá nhanh; giữ random + tốc độ người |
| Bootstrap | 4–10s | 2–3.5s | Bỏ/song-song hoá `load`-wait; event-driven auth wait; defer `capabilities()` | Miss popup hiếm nếu dismiss bị defer — giữ dismiss sớm |
| Position | ~0/turn (affinity, cùng conv); 2–5s turn đầu có conv cũ | 1× / session lifetime | Affinity worker; fastpath in-page fetch loại navigate/turn | Protocol chưa verified; affinity fail-over phải reopen |
| Send (pre-submit) | 0.35–1.3s (composer 0.25 + effort recheck 0–1 + sweeps) | ~0.1s | Bỏ sleep 0.25 vô điều kiện; confirm effort tại bootstrap; batch evaluate | Composer race nếu bỏ settle — chỉ sleep khi tick trước thấy not-editable |
| First delta | model TTFB + ≤0.24s (poll) + overhead ~30 RPC/tick | TTFB + ~0.12s | Batch evaluate 1 round-trip/tick; network SSE listener đã có | Lỗi parse gộp — giữ fallback per-selector |

---

## TOP-5 cắt giảm lớn nhất (theo giây)

1. **Login: bỏ pause đôi sau submit (email 6.0s + password 7.5s + OTP 8.5s) → event-driven wait** — tiết kiệm **~13–17s/login**. Ba vị trí đều ngủ cứng 2 lớp (trong `_click_submit_button` 3.0s + ngoài 3.0/4.0/5.0s) trong khi điều kiện thật là "URL đổi / field kế tiếp hiện".
2. **Login: gate grace 10s của bước check-2FA theo URL** — tài khoản không 2FA hiện cháy đủ `grace_deadline=10s`; nếu URL rời auth domain mà không khớp `_MFA_URL_RE` sau ~2–3s thì break → **~7–10s/login (không 2FA)**.
3. **Bootstrap: bỏ/song-song `wait_for_load_state("load", 10s)` + auth-poll 0.5s → event-driven** — **~2–8s/session**; `capabilities()` defer thêm ~1s.
4. **Login: bỏ nhóm pause mở đầu (2.0s sau goto + 1.0s sau click login + 0.5s sau gõ)** — **~3.5s/login**, thay bằng check `_is_authenticated_page` đã có sẵn và nav-wait.
5. **Send: bỏ `sleep(0.25)` vô điều kiện trong `get_composer` + confirm effort trước send để skip probe 1s** — **~0.3–1.2s/turn**, nhân theo số turn trong loop agent (batch evaluate trong poll-loop cộng thêm độ trễ delta).

Tổng tiềm năng: login ~27–37s → ~3–6s; mỗi turn send tiết kiệm thêm ~0.3–1.2s.
