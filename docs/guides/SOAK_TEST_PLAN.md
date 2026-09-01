# SOAK TEST PLAN — Kiểm tra độ chịu đựng toàn hệ thống (Gateway + Transport + Browser Session)

> Trạng thái: PROPOSAL — chưa bật tự động. Mọi lần chạy thật tiêu tốn quota account và thời gian máy,
> cần người duyệt trước khi kích hoạt lịch nightly (xem mục 6 và mục 8).
>
> Harness tương ứng: `scripts/bench/soak_runner.py` · Test: `tests/test_soak_runner.py` · Báo cáo: `docs/reports/soak/`

## 1. Mục tiêu

Xác nhận toàn chuỗi **gateway (`gpt debug api-server`) → transport → browser session** chạy liên tục
nhiều giờ mà không:

- tăng RAM vô hạn (leak ở gateway hoặc Chrome),
- tích lũy lỗi (error rate tăng dần theo số turn),
- treo turn (>120s không phản hồi),
- mất khả năng tự hồi phục sau sự cố browser.

## 2. Điều kiện tiền đề

| Hạng mục | Giá trị |
|---|---|
| Gateway đích | `127.0.0.1:18000` (mặc định, service `webgpt-gateway.service`) |
| Health check trước khi chạy | `GET /health` phải 200 |
| Endpoint đo | `POST /v1/chat/completions` (payload nhỏ: `"Reply with the single word: ok"`) |
| Tài nguyên máy | RAM đang căng (~6.7Gi available / 15Gi) — chỉ chạy 1 kịch bản/lần, không mở browser thủ công song song |
| Quy tắc an toàn harness | Tối đa 500 turn/lần nếu không có `--i-know-this-is-long`; `--dry-run` để xem plan không gửi gì |

## 3. Ma trận kịch bản

### (a) `stable` — Soak ổn định

- **Mô tả:** N turn liên tiếp cách đều `--interval` giây, 1 conversation luồng đơn, prompt nhỏ cố định.
- **Cách chạy:**
  `gpt bench soak --scenario stable --turns 100 --interval 20 --port 18000`
- **Thời lượng tham chiếu:** 100 turn × (latency ~15–40s + interval 20s) ≈ 1–1.7h. Đợt dài: 500 turn ≈ 5–8h (chạy qua đêm).
- **Số liệu thu:** p50/p95/max latency per turn · error rate · RSS gateway + tổng RSS chrome con (sample mỗi 30s vào JSONL).
- **Ngưỡng PASS/FAIL:**
  | Check | Ngưỡng PASS |
  |---|---|
  | Error rate | < 2% |
  | Turn chậm nhất | < 120s (không turn nào treo) |
  | p95 latency | ≤ 90s |
  | RSS tổng (gateway+chrome) cuối vs baseline đầu | tăng < 20% |

### (b) `burst` — Nhiều conversation song song

- **Mô tả:** các "wave" gửi đồng thời `--concurrency` request (mỗi request = conversation riêng qua header session), nghỉ `--interval` giữa các wave, đến hết `--turns`.
- **Cách chạy:**
  `gpt bench soak --scenario burst --turns 40 --concurrency 4 --interval 30 --port 18000`
- **Số liệu thu:** như stable + phân bố latency theo wave (phát hiện xếp hàng/queue timeout).
- **Ngưỡng PASS/FAIL:**
  | Check | Ngưỡng PASS |
  |---|---|
  | Error rate | < 2% (ngoài lỗi 429 có chủ đích của kịch bản rate-limit riêng) |
  | Turn chậm nhất | < 120s |
  | p95 latency | ≤ 120s (chấp nhận chậm hơn stable do tranh chấp worker/browser) |
  | RSS tăng | < 25% (cho phép cao hơn stable do nhiều session sống đồng thời) |

### (c) `recovery` — Kill browser giữa chừng

- **Mô tả:** chạy như stable; tại giữa chừng (sau turn thứ N/2) harness tìm 1 process chrome con của gateway và `SIGKILL`, sau đó tiếp tục gửi turn để đo gateway tự hồi phục (spawn lại browser/session) thế nào.
- **Cách chạy:**
  `gpt bench soak --scenario recovery --turns 20 --interval 15 --port 18000`
- **Số liệu thu:** error rate giai đoạn trước/sau kill · số turn từ lúc kill tới turn thành công đầu tiên (thời gian hồi phục) · `recovered` = có ít nhất 1 turn OK trong 25% turn cuối · RSS trước/sau kill (browser mới có RSS hợp lý, không nhân đôi).
- **Ngưỡng PASS/FAIL:**
  | Check | Ngưỡng PASS |
  |---|---|
  | Turn chậm nhất | < 120s |
  | Recovered | ≥ 1 turn OK trong 25% turn cuối |
  | Error rate toàn cục | < 15% (đợt ngay sau kill được phép fail) |
  | RSS sau hồi phục | không vượt baseline × 1.5 (không chồng browser cũ+mới) |

### (d) `leak` — Đo RSS theo thời gian dài

- **Mô tả:** giống stable nhưng kéo dài (hàng trăm đến 500 turn) và đánh giá trọng tâm vào **độ dốc tăng RSS** (hồi quy tuyến tính trên chuỗi sample) chứ không chỉ điểm đầu/cuối.
- **Cách chạy:**
  `gpt bench soak --scenario leak --turns 300 --interval 20 --port 18000 --i-know-this-is-long`
- **Số liệu thu:** baseline (trung bình 10% sample đầu) · final (3 sample cuối) · peak · slope KB/sample · growth %.
- **Ngưỡng PASS/FAIL:**
  | Check | Ngưỡng PASS |
  |---|---|
  | Growth cuối vs baseline | < 20% |
  | Trung bình RSS ¼ cuối vs baseline | ≤ baseline × 1.2 |
  | Error rate | < 2% |
  | Turn chậm nhất | < 120s |

### (e) `rate-limit` — Hành vi khi account bị giới hạn

- **Mô tả:** kịch bản quan sát (observation-only), hiện chạy **thủ công**: ép gateway về trạng thái giới hạn (giảm interval xuống rất nhỏ để dồn lưu lượng, hoặc dùng account đã biết đang 429), ghi lại mã HTTP trả về client (mong muốn 429/5xx có cấu trúc, không treo, không crash gateway).
- **Cách chạy:**
  `gpt bench soak --scenario burst --turns 30 --concurrency 6 --interval 1 --port 18000`
  (kết hợp account hạn chế quota; đọc cột status trong báo cáo)
- **Số liệu thu:** tỷ lệ 429/5xx · latency khi bị limit (phải fail nhanh, không treo tới 120s) · gateway còn sống (`/health` = 200 sau đợt burst).
- **Ngưỡng PASS/FAIL:**
  | Check | Ngưỡng PASS |
  |---|---|
  | Gateway sống sau đợt limit | `/health` = 200 |
  | Turn bị limit | trả lời ≤ 130s bằng lỗi rõ ràng (429/5xx), không treo im |
  | Không crash | 0 kết nối bị reset ngoài errors có thân xác HTTP |

> Kịch bản (e) phụ thuộc việc gây ra giới hạn thật phía account — cần quyết định của người sở hữu
> account trước khi chạy vì ảnh hưởng quota dài hạn.

## 4. Số liệu & cơ chế thu (dùng chung)

- **Latency per turn:** đo quanh `POST /v1/chat/completions` bằng `time.monotonic()`; tính p50/p95 (nội suy trên mảng đã sort), mean, max.
- **RSS:** thread sampler mỗi `--rss-interval` (mặc định 30s):
  - tìm PID gateway qua `ss -lptn sport = :PORT` → fallback `lsof -ti tcp:PORT` → fallback `pgrep -f "gpt.debug api-server"`;
  - RSS gateway: `ps -o rss= -p PID`;
  - tổng RSS chrome con: duyệt cây `pgrep -P` (depth ≤ 3), lọc tên process khớp `chrome|chromium|cloak`, cộng RSS;
  - ghi từng sample dạng JSONL (`kind=sample`) cùng sự kiện turn (`kind=turn`) và sự kiện kill (`kind=event`).
- **Correction count từ trace bus:** truyền `--trace-file PATH` cho soak harness để chụp sequence đầu
  phiên và tự tổng hợp các `request_completed` mới vào báo cáo markdown: số request có correction,
  tổng correction đã gửi và max correction/request. Dòng trace lỗi được bỏ qua có đếm; nếu không truyền
  `--trace-file` thì harness giữ nguyên chế độ nhẹ và không đọc trace.

## 5. Ngưỡng tổng hợp (mặc định của harness)

| Ngưỡng | Giá trị mặc định | Flag override |
|---|---|---|
| Tăng RSS tối đa | 20% (burst 25%) | `--max-rss-growth-pct` |
| Error rate tối đa | 2% (recovery 15%) | `--max-error-rate-pct` |
| Turn chậm nhất | 120s | `--max-turn-latency-s` |
| p95 latency tối đa | 90s (burst 120s) | `--max-p95-latency-s` |

Verdict FAIL → harness exit code 1 (để systemd báo failed).

## 6. Lịch chạy đề xuất (systemd user timer sẵn có)

Theo đúng mô hình trong `docs/guides/AUTOMATION_OPS.md` (watchdog mỗi 5 phút, auto-review daily 04:17),
bổ sung 1 cặp unit **webgpt-soak**:

- `~/.config/systemd/user/webgpt-soak.timer`: `OnCalendar=*-*-01 03:33` (mỗi tháng ngày 1, 03:33 — khung
  giờ vắng nhất, tránh trùng auto-review 04:17) + `Persistent=true`.
- `~/.config/systemd/user/webgpt-soak.service` (Type=oneshot):
  `ExecStart=gpt bench soak --scenario leak --turns 300 --interval 20 --port 18000 --i-know-this-is-long`
- Tuần tự xen kẽ: tuần đổi `--scenario` (stable ↔ burst ↔ recovery) bằng cách sửa ExecStart, hoặc thêm
  timer thứ hai `OnCalendar=Weekly` nếu cần. Chỉ chạy khi không ai dùng máy (RAM đang căng).
- Báo cáo tự nằm tại `docs/reports/soak/soak-<scenario>-<timestamp>.md`; FAIL sẽ hiện trong
  `systemctl --user list-timers | grep webgpt` như unit failed.

> Chưa tạo unit thật trong đợt này — chỉ đề xuất. Cần duyệt trước khi `systemctl --user enable`.

## 7. Quy trình một buổi soak (checklist vận hành)

1. `curl -fsS http://127.0.0.1:18000/health` — gateway phải sẵn sàng.
2. `free -h` — cần ≥ 4Gi available; nếu thấp hơn, dời lịch.
3. `--dry-run` trước mọi cấu hình mới, đọc plan in ra.
4. Chạy kịch bản (nền/tmux/nohup), theo dõi JSONL bằng `tail -f`.
5. Sau khi xong: mở báo cáo markdown, đối chiếu verdict + soát trace bus về correction count.
6. Nếu FAIL do RSS: giữ lại JSONL làm bằng chứng so kỳ trước (so slope 2 tuần).

## 8. Cần người duyệt trước khi chạy thật

- **Chi phí quota:** mỗi turn là 1 request thật lên backend qua account đang đăng nhập; 300–500 turn/đêm
  × vài đêm/tuần là khối lượng đáng kể. Cần chốt giới hạn turn/ngày.
- **Khung giờ & tài nguyên:** máy đang thiếu RAM; xác nhận khung 03:33 thực sự nhàn rỗi.
- **Kịch bản recovery/rate-limit** chủ động phá browser / dồn lưu lượng — xác nhận không xung đột với
  session đang dùng thật của gateway cùng lúc.
