# Master Roadmap — WebGPT Protocol-First Optimization

**Ngày:** 2026-08-24 · Nguồn: 4 agent nghiên cứu song song (transport · gateway-perf · multi-account/orchestrator · live-probe) + bằng chứng Burp/artifact
**Nguyên tắc:** tối ưu thời gian/tài nguyên tuyệt đối · bỏ DOM khỏi hot-path · thuần chức năng (bỏ qua lớp an toàn theo quyết định của owner)

---

## 0. Cơ sở bằng chứng (đã xác minh)

| Fact | Bằng chứng |
|---|---|
| Sentinel legacy `/chat-requirements` đơn bước còn sống; token TTL `expire_after ≈ 540s` nhưng repo mint lại mỗi turn | live probe 24/8 (`docs/reports/live-sse-probe-2026-08-24.md`) |
| Finalize shape đúng là `{"prepare_token": …}` → 200; `{"p": …}` → 500 | live probe 24/8 |
| Conduit `/f/conversation/prepare` trả 422 với mọi shape đã thử — schema chuẩn chưa biết | live probe 24/8 |
| Direct POST `/f/conversation` nhận SSE bị 403 "Unusual activity" — chưa kết luận (probe session bị coi noauth) | live probe 24/8 |
| Flow guest thật: init → sentinel prepare→finalize → f/conversation/prepare → stream qua WS topic "conversations" | capture Burp 24/8 (`docs/reports/2026-08-24-live-protocol-findings.md`) |
| curl_cffi 0.16.1 có sẵn `AsyncWebSocket` — không cần thêm dependency cho WS client | kiểm tra venv |
| Parser SSE v1 JSON-patch đã hỗ trợ (6 test từ artifact thật); sentinel prepare→finalize đã wire với fallback legacy | code hôm nay |

## Trạng thái đã hoàn thành (hôm nay) ✅

1. Tách config env-per-terminal (`environ > .env > default`, `gpt-web env`, launcher về repo)
2. Parser SSE delta_encoding v1 + lọc role assistant
3. Sentinel prepare→finalize + fallback legacy + `prepare_conduit()` helper
4. **[G0]** Sửa shape finalize thành `{"prepare_token": …}` sau khi probe phát hiện bug

---

## TRACK 1 — TRANSPORT (protocol-first)

| Giai đoạn | Việc làm | Gate xác minh | Rollback |
|---|---|---|---|
| **T1 = G3** Mint theo TTL *(S)* | Cache sentinel theo `expire_after` −60s biên; invalidate khi 401/403 | Unit: 2 lần gọi → page.evaluate chỉ chạy 1 lần; trace `sentinel_mint_count ≤ 2` / 10 turn; browser_ms giảm ~100–300ms | Env tắt cache |
| **T2** Wire conduit *(S/M)* | `CurlCffiTransport.send` gọi `prepare_conduit()`, gắn token vào request; đo `conduit_prepare_ms` | Flag on/off byte-diff; live log status prepare qua 10 turn tìm shape hết 422 | Flag mặc định off |
| **T3A** *nếu SSE sống:* hardening *(M)* | Bắt 403 → re-mint credential → retry đúng 1 lần | Fault-injection test; live 20 turn tỷ lệ retry < 10% | Retry=0 qua env |
| **T3B** *nếu SSE chết:* WS stream *(L)* | Capture frame WS thật trước (bắt buộc, không code mù) → `ws_stream.py` dùng `AsyncWebSocket`; mode env `WEBGPT_STREAM_MODE=sse\|ws\|auto` | Fixture replay ra đúng text; WS server local integration; p95 first-delta(ws) ≤ sse+200ms | Ép mode=sse |
| **T4** Bỏ browser khỏi hot-path *(M)* | Persist TokenBundle có TTL vào profile; lazy-start Chromium; flip default transport sang hybrid khi T1–T3 xanh | Gateway khởi động không process chrome khi cache fresh; RSS < 300MB; 20 turn không đụng browser | Env `WEBGPT_REQUIRE_BROWSER=1` |
| **T5** Connection pooling *(S/M)* | 1 `AsyncSession(max_clients=N)` chia sẻ cả factory | Benchmark 10 turn TTFB giảm đo được; không leak connection sau close | Giữ per-session cũ |

## TRACK 2 — GATEWAY HIỆU NĂNG

| Giai đoạn | Việc làm | Gate | Rollback |
|---|---|---|---|
| **P1** Poll/UI quick-win *(S)* | `poll_interval` 0.25→0.12, `stable_grace` 0.9→0.45 (env); cache dismiss_popups | Stress 10 turn giảm ≥ 1.2s/turn; corrections không tăng | Env về 0.25/0.9 |
| **P2** Worker affinity *(M)* | Map conversation→worker tránh `open()` navigate 2–6s mỗi turn miss | `position_ms` p95 < 300ms từ turn 2 | Flag off → LIFO |
| **P3** Leak + persist async | `_conversation_locks` xoá khi hết waiter; `_response_sessions` LRU cap; `_persist` → `to_thread`; TTL evict; cap `_history` | RSS phẳng sau 500 turn mock; p95 request song sinh không spike >10% | Revert riêng từng mục |
| **P4** Prompt trần 80K *(rất thấp)* | Đổi `WEBGPT_MAX_PROMPT_CHARS=80000` trong unit + cache fingerprint canonical | Trace prompt_chars p95 < 80K; browser_ms giảm với transcript dài | Về 250000 |
| **P5** Stream OpenAI path *(M)* | `_stream_turn_on_session` đẩy delta sống thay vì buffer rồi cắt chunk | TTFB-nội-dung giảm ~0.8–1.2s | Flag legacy-buffering |
| **P6** Corrections=1 + idle-timeout *(S)* | Giảm max_corrections; idle-timeout 45s nhả worker sớm | Worker occupancy p99 giảm; lỗi client không tăng | Env về giá trị cũ |
| **P7** A/B hybrid transport | Chạy unit thứ 2 `--transport hybrid` so browser_ms + RSS | browser_ms p50 −1.5s; RSS −30%; lỗi +≤1% | Đổi 1 từ trong unit |

## TRACK 3A — MULTI-ACCOUNT

| Giai đoạn | Việc làm | Gate |
|---|---|---|
| **A1** Health tracker *(nền tảng)* | `AccountHealthTracker` in-memory (cooldown/failures/clock injectable) + health loop định kỳ | Fake-clock test cooldown hết hạn đúng; all-cooldown vẫn fallback không crash |
| **A2** Rate-limit routing | `_lease_session` bắt `RateLimited/AuthRequired` → cooldown 900s (env); pin được honor kể cả cooldown | Mock 2 account: dính 429 → round-robin rơi account kia; quay lại đúng lúc hết hạn |
| **A3** Failover an toàn | KHÔNG migrate conversation_id; chỉ failover khi turn chưa commit → reset record (`web_bootstrapped=False` re-bootstrap có sẵn); `CommitUnknown(submitted=True)` phải reconcile trước | Test đủ 3 scenario; live: hội thoại nhiều turn migrate account giữ ngữ cảnh |
| **A4** Default account | Registry key `default_account` + CLI `gpt-web account default <name>│--show│--clear` + auto-set sau login đầu tiên + env `WEBGPT_DEFAULT_ACCOUNT` (env > registry) | Routing sticky default; login acc mới → tự thành default |

## TRACK 3B — ORCHESTRATOR

| Giai đoạn | Việc làm | Gate |
|---|---|---|
| **B1** Deadline mọi vòng lặp | `ensure_instance_live` deadline 1800s (env); timeout claude turn env-ized; worker deadline 3600s | URL chết → ESCALATED + NEEDS_HUMAN_REVIEW sau đúng deadline |
| **B2** Propagate exception *(quick-win)* | Duyệt kết quả gather, log bảng tổng hợp thắng/thua/lý do | Worker crash giữa race → `worker_errors` đủ entry, race không raise |
| **B3** Cooperative cancel | `create_subprocess_exec` + SIGTERM grace 5s→SIGKILL; stop_event hủy cả turn đang chạy | Winner xong khi worker khác ngủ 60s → race return < 10s, không zombie |
| **B4** Resource caps | Semaphore solve-slot (default 4) + RLIMIT_CPU/nice cho solve.py | Peak concurrency ≤ slot; infinite-loop script bị kill ~CPU-limit |

## Thứ tự thực hiện tổng hợp (ROI-first)

```
Nhanh (< nửa ngày):  B2 · B1-lite · P4(env) · P1(env) · A2-lite(cooldown mini) · A4-lite(env default)
Chuẩn bị nền:        T1(G3) → chờ probe SSE tươi trên session đăng nhập → quyết định T3A/T3B
Song song an toàn:   [A1+A4+B2] → [A2+B1] → [A3+B3] → [B4]
Đích đến:            T4 (gateway không Chromium) + P7 (hybrid chính thức) — chỉ khi gate xanh
```

## Rủi ro chính cần theo dõi

1. Shape conduit prepare 422 chưa giải — nếu chuẩn bị mãi không mint được mà SSE cũng chết → phải quyết định đầu tư PoW solver (ngoài phạm vi hiện tại).
2. 403 "Unusual activity" cần probe lại trên session đăng nhập tươi trước khi tuyên bố SSE chết.
3. Flip default transport (T4/P7) chỉ làm sau khi toàn bộ gate xanh trên live matrix.
