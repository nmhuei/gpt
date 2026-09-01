# Live CLI Verification — Round 7 (2026-08-24) — FRESH-RUNTIME MEASURE + T3 BLOCKED_BY_USAGE_CAP

**BÁO CÁO TẠM (interim)** theo yêu cầu điều phối lúc 20:06. Round này là lần ĐẦU TIÊN gateway
được đo trên code mới nhất (stealth protocol + protocol-aware delta filter + handshake re-append
theo web-thread-change) — R6 chỉ đo được stale runtime. Pre-flight PASS, T1/T2 PASS, nhưng
**T3 bị chặn môi trường: USAGE-CAP của tài khoản ChatGPT**, không phải lỗi protocol.

- Repo: `/home/light/GitHub/gpt` — KHÔNG sửa code. Browser headless.
- Gateway: `http://127.0.0.1:18000`. Env xác minh từ systemd unit:
  `WEBGPT_TOOL_PROTOCOL=soft`, `WEBGPT_MAX_CORRECTIONS=4`, `WEBGPT_PROMPT_DEBUG_DIR` bật,
  `--account personal --max-workers 8 --warm-workers 4 --headless --allow-authenticated`.
- Thư mục test: `/tmp/cc-live-test7` (sạch), task.md fizzbuzz tiếng Việt y hệt R1–R6.
- Turn CLI đã dùng: **6/8** (T1 ×3 = hết trần 2 retry, T2 ×1, T3 ×2). Còn **2 turn** cho người
  kế nhiệm (khuyến nghị dùng cho đúng T3 attempt 3 + 1 dự phòng).
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` — **dùng chung với traffic client khác**
  chạy cùng account trong toàn bộ round (xem "Cảnh báo nhiễu" bên dưới).
- Prompt dumps round này: `/home/light/Downloads/webgpt/logs/prompt-debug/0000…–001087_*`
  (từ 19:34:35; dump có `"client": "claude-code"` + model `claude-opus-5` là của round này).

## Bước 0 — Pre-flight: **PASS**

1. **Fresh-runtime check**: process start `19:33:38` (sau khi tôi restart sạch lần cuối) > mtime
   `gpt/gateway/server.py` = `2026-08-24 18:39:05`. ⇒ process CHẠY CODE MỚI — điều kiện tiên
   quyết của R6 đã thỏa.
2. **Probe SSE nhanh** (`POST /v1/messages` stream:true, prompt "Reply with exactly: OK7",
   httpx trực tiếp): status 200, chuỗi event đầy đủ
   `message_start → content_block_start(text) → content_block_delta{"OK7"} → … → message_stop`,
   elapsed 8.7s. **Không leak tag vào text delta, không error, không fail-closed** ⇒ FIX-A
   (protocol-aware delta filter) hoạt động đúng trên live cho case prose thuần.

## Sự cố vận hành phải xử lý trước khi đo được gì (19:11–19:33)

Không thuộc scope fix nhưng chiếm ~20 phút round và có giá trị cho vận hành:

1. Sau restart 19:11 của user, **mọi request hit `AuthRequired`** ("ChatGPT login is required")
   trên nhiều session liên tiếp ~5 phút — profile render trang logged-out (anonymous landing có
   composer + nút Log in).
2. `gpt-web account login --auto --use-saved` (zero-interaction headless) **thất bại 4/4 lần** ở
   Step 2: click mọi selector nút Log in đều **không điều hướng** tới auth.openai.com (trang đứng
   yên tại chatgpt.com) ⇒ không bao giờ tới bước password ⇒ `LoginError: Password input field did
   not appear in time`. Không có captcha nào được phát hiện (`_has_security_challenge` không bắn).
   Đây là **regression tiềm tàng của authenticator với UI ChatGPT hiện tại** — nếu session logout
   thật sự thì hiện KHÔNG có đường khôi phục tự động. Cần điều tra riêng.
3. Profile tự phục hồi sau vài lần load: trang sau đó render trạng thái **đã đăng nhập** (có
   `__Secure-next-auth.session-token.*`, accounts-profile-button, không còn nút Log in) ⇒ đợt
   AuthRequired là **hydration race/flaky state**, không phải logout thật. Restart gateway lần
   cuối 19:33:38 rồi probe OK ⇒ sạch.
4. Bài học vận hành: kill browser giữ `SingletonLock` phải xong trước khi start service, nếu
   không worker đầu tiên chết với `ProcessSingleton` và browser manager không tự revive
   (`browser_crash_classified_as_disconnected`).

## Kết quả từng mức

### T1 kết nối & streaming — **PASS lần 3/3 (hết trần retry), 42s, stdout nguyên văn `AY OK GATEWAY OK`**

- Attempt 1 (RC=0, 165s): stdout **bị cắt còn `AY OK GATEW` (11 chars)**. Forensic: trace
  `submit_completed assistant_chars=11` ngay tầng capture browser ⇒ truncation xảy ra **trước**
  delta filter (filter có remainder reconciliation đầy đủ ở finalize, `server.py::_anthropic_live_stream`);
  nghi capture glitch trong cơn hỗn loạn RateLimited/AuthRequired lúc đó. Anomaly cần theo dõi,
  chưa repro lại.
- Attempt 2: **timeout 180s RC=124, stdout rỗng** — trùng window USAGE-CAP đầu tiên (mọi submit →
  `RateLimited` → 503 retryable; CLI tự retry đến hết giờ).
- Attempt 3: PASS sạch 42s.

### T2 tool use 1 bước — **PASS lần 1, 28s, stdout nguyên văn `TOOLCHAIN_1787575727`**

- Token sinh runtime (`date +%s`) — CLI thực thi bash THẬT trên máy, output khớp từng ký tự, RC=0.
- Trace cho thấy generation thành công của turn này đi qua **2 soft correction** trước khi emit
  tool call (`submit_completed correction_count=2, parsed tool_calls=1`) — correction loop soft
  hoạt động đúng và vẫn cho kết quả nhanh (28s tổng).
- FIX-B evidence: prompt dumps các thread mới trong round đều ghi `soft_handshake_appended: true`
  (bao gồm thread replay sau failover) — khác R6 nơi thread quyết định bị `false`.

### T3 đa bước tự chủ — **FAIL 0/2 attempt, attempt 3 = BLOCKED_BY_USAGE_CAP**

| | T3a (19:54) | T3b (19:57) |
|---|---|---|
| RC | 1 | 1 |
| Thời gian | 296s | 286s |
| stdout nguyên văn | `API Error: 503 Conversation failed over to a fresh ChatGPT web session; please resend this request to continue. This is a server-side issue, usually temporary — try again in a moment. If it persists, check your inference gateway (127.0.0.1:18000).` | y hệt |
| Artifact fizzbuzz.py / output.txt | **KHÔNG** | **KHÔNG** |

Root cause đo được (trace): **100% submit trong 2 attempt này chuyển
`generating → rate_limited`** ("ChatGPT Web reports that the current usage limit was reached." /
"request was rate limited.") sau 3–12s mỗi gen ⇒ failover conversation ⇒ cạn internal retry ⇒
503 trả client. **Không một generation web nào thành công trong T3a/T3b** ⇒ chưa đo được bất cứ
thứ gì về FIX A/B ở mức đa bước. Đây là blocker tài khoản, không phải điểm gãy protocol.

Trạng thái chờ: bắt đầu poll cửa sổ cap mở từ **19:59** (probe nhỏ mỗi 3 phút, yêu cầu 2 lần OK
liên tiếp), đến **20:06 vẫn đóng** khi điều phối ra lệnh dừng chờ và viết báo cáo tạm. Poller
đã bị stop; script còn tại `/tmp/wait_cap_open.sh`, probe tại `/tmp/sse_probe7.py`.

## Bảng so sánh R5 / R6 / R7 từng mức

| Mức | Round 5 (soft stealth) | Round 6 (fix A/B, stale runtime) | **Round 7 (fresh runtime)** |
|---|---|---|---|
| Pre-flight stale-check | (chưa có) | FAIL — process 17:27 < file 18:39 | **PASS — process 19:33:38 > file 18:39:05** |
| Probe SSE leak-tag | leak `<cmd>` vào text delta rồi fail-closed | (chính symptom phát hiện stale) | **Sạch — "OK7" full, không tag, không error** |
| T1 kết nối & streaming | PASS lần 1, ~11s | PASS lần 1, ~42s | **PASS lần 3/3, 42s** (lần 1 truncated 11 chars ở capture layer; lần 2 timeout do RateLimited) |
| T2 tool use 1 bước | PASS lần 1 (~128s, 0 correction) | PASS lần 1 (~3 gens, 1 correction) | **PASS lần 1, 28s, 2 soft correction, token thật khớp** |
| T3 đa bước tự chủ | FAIL ×3 (pipeline làm rơi cmd đủ) | FAIL ×3 (leg SSE mất + thiếu handshake) | **FAIL ×2 + BLOCKED** — 0/8+ generation thành công, mọi submit RateLimited |
| Artifact fizzbuzz.py/output.txt | Không | Không | **Không** (T3 chưa từng tới bước tạo file) |
| Output nhiễm bẩn / leak tag | Sạch (riêng T3a nhân đôi) | T3c debug-log lẫn + nhân đôi final | **Sạch ở mọi turn có output** (T1c/T2); anomaly truncate T1a |
| Silent-fail RC=0 | Có (deflection) | Có ×3 | Không xuất hiện (fail giờ là 503 lộ diện, RC=1) |
| Handshake re-append (FIX-B) | append-once (BUG-B) | false trên thread quyết định (stale code) | **true trên mọi thread mới/replay có dump** — fix quan sát được hoạt động |
| Delta filter (FIX-A) | N/A | Chưa được nạp | **Hoạt động trên live**: probe sạch, T2 roundtrip sạch, 0 leak |

Generations round này (attributable claude-code): T1 ≈ 3 ok legs (1 truncated) + nhiều 503 ·
T2 = 2 gens (gen quyết định qua 2 correction) · T3a/b = **0 ok / ≥10 RateLimited**. Lưu ý trace
dùng chung: tổng 85 request `client: claude-code` từ 19:33 gồm cả traffic agent khác chạy cùng
gateway/account — số tuyệt đối có nhiễu, các con số trên đã lọc theo turn window.

## Cảnh báo nhiễu cho người kế nhiệm

Một client khác đang chạy song song trên cùng gateway + cùng account `personal` (POST liên tục
trong journal, openai_chat + anthropic_messages). Mỗi request của nó đốt quota của cùng account
⇒ USAGE-CAP có thể quay lại bất cứ lúc nào và T3 attempt 3 có thể lại đỏ vì môi trường. Nếu thấy
lại 503 RateLimited: kiểm tra `state_transition … rate_limited` trong trace trước khi kết luận
về protocol.

## Khuyến nghị cho vòng kế tiếp (Round 7b — chỉ còn đúng việc T3)

1. **Ai chạy**: agent live-test mới, tái dùng nguyên protocol này (task.md tại
   `/tmp/cc-live-test7/task.md`, env `ANTHROPIC_BASE_URL=http://127.0.0.1:18000`,
   `--dangerously-skip-permissions`, cwd `/tmp/cc-live-test7`).
2. **Turn budget cần**: tối đa 2 turn CLI — T3 attempt 3 (timeout 900) + 1 retry duy nhất nếu
   gặp 503 failover. KHÔNG chạy lại T1/T2 (đã PASS, budget 6/8 đã ghi).
3. **Điều kiện bắt đầu**: probe SSE nhỏ (`/tmp/sse_probe7.py`) OK **2 lần liên tiếp cách nhau
   30s** rồi mới bắn T3 (script `/tmp/wait_cap_open.sh` còn dùng được).
4. **Tiêu chí mốc**: T3 attempt 3 tạo `fizzbuzz.py` + `output.txt`, nội dung đúng 1..15,
   `python3 fizzbuzz.py` chạy đúng ⇒ tuyên bố mốc "CLI tự chủ đa bước qua ChatGPT Web".
   Nếu lại fail vì lý do KHÔNG phải RateLimited/AuthRequired ⇒ verdict đỏ đích danh điểm gãy
   (so sánh với R6: leg-SSE-loss đã hết nhờ JSON path; handshake đã true — điểm gãy nghi vấn
   còn lại chỉ là hành vi model trên thread feedback).
5. **Việc song song không tốn turn**: điều tra authenticator Step-2 (nút Log in không điều
   hướng) — rủi ro mất khả năng khôi phục session tự động; và anomaly truncation capture-layer
   của T1a.

## Verdict tạm thời

- **KHÔNG tuyên bố mốc "CLI tự chủ đa bước qua ChatGPT Web"** — T3 chưa có kết quả đo hợp lệ.
- Đã chứng minh trên fresh runtime: pre-flight PASS, FIX-A hết leak/fail-closed, FIX-B handshake
  re-append true, T2 pass với correction soft hoạt động đúng.
- T3 = **BLOCKED_BY_USAGE_CAP** (bắt đầu chờ 19:59, cap vẫn đóng lúc 20:06). Verdict cuối thuộc
  về Round 7b.

## Phụ lục — artifact raw

- CLI outputs: `/tmp/cc-live-test7/t1{,b,c}.{stdout,stderr}`, `t2.{stdout,stderr}`,
  `t2.token`, `t3{,b}.{stdout,stderr}`, `task.md`.
- Probe/poller: `/tmp/sse_probe7.py`, `/tmp/wait_cap_open.sh`; login logs `/tmp/login7*.log`;
  diagnostic scripts `/tmp/diag_page7.py`, `/tmp/diag_state7.py`, `/tmp/diag_click7.py`.
- Trace: `/home/light/Downloads/webgpt/logs/trace.jsonl` (seq ~1–1104 từ 19:11; section claude-code
  của round này bắt đầu ~19:34). Prompt dumps: `prompt-debug/000045…001087_*`.
- Sessions round này (dump claude-code): `wgs_d6a356a2…` (T1a), `wgs_cdbe59ad…`/`wgs_c9315488…`
  (T2 region), `wgs_8dc60eaa…`, `wgs_59cd9360…`, `wgs_0850a59c…` (T3a/b).
