# E2E Project Benchmark (Claude Code CLI → webgpt gateway → ChatGPT Web)

Bài kiểm thử E2E **thật** cho toàn chuỗi: Claude Code CLI gọi tới webgpt gateway,
gateway chuyển tiếp tới backend (ChatGPT Web hoặc mock). Khác với các test chỉ
kiểm tra câu trả lời chữ, benchmark này buộc Claude **thao tác với máy**: dựng
một dự án phần mềm hoàn chỉnh trong thư mục tạm, sau đó harness **assert cơ học**
trên từng artifact sinh ra.

- Script: `scripts/bench/e2e_project_benchmark.py`
- Unit test cho harness: `tests/test_e2e_benchmark_harness.py`

## Kịch bản

Claude nhận một prompt tiếng Anh dài, rõ ràng, yêu cầu tạo project `taskmanager`:

| # | Deliverable | Chi tiết |
|---|-------------|----------|
| 1 | `README.md` | Không rỗng, có hướng dẫn cài đặt/usage/chạy test |
| 2 | `pyproject.toml` hoặc `requirements.txt` | Metadata project `taskmanager` |
| 3 | Package `taskmanager/` | Ít nhất `__init__.py`, `models.py`, `storage.py`, `cli.py` |
| 4 | Pytest suite | ≥5 test, tự chạy và phải PASS |
| 5 | `Makefile` | Target `test` và `lint` |
| 6 | Git | `git init` + ít nhất 1 commit đầu tiên |

Prompt cũng cấm Claude đụng vào bất kỳ đường dẫn nào ngoài thư mục làm việc.

## Hai chế độ chạy

### 1. Chế độ thật (mặc định) — tốn quota ChatGPT

```bash
gpt bench e2e                                      # gateway :18000
gpt bench e2e --timeout 2400
```

Script **check `/health` của gateway trước tiên**. Nếu gateway chưa sống,
script exit code `2` kèm hướng dẫn khởi động lại (`gpt restart`). Không bao giờ
tự dựng browser khi chạy chế độ này.

Cấu hình qua biến môi trường:
- `WEBGPT_GATEWAY_URL` — URL gateway mặc định (mặc định `http://127.0.0.1:18000`)
- `WEBGPT_API_KEY` — API key gửi kèm (mặc định `sk-webgpt-local`)
- `E2E_CLAUDE_MODEL` — model claude yêu cầu (mặc định `claude-3-5-sonnet`)

### 2. Chế độ mock (`--mock-gateway`) — không tốn quota, dùng để smoke-test harness

```bash
gpt bench e2e --mock-gateway --timeout 300
```

Script tự:
1. Chọn một port trắng ngẫu nhiên (kernel cấp bằng `bind(0)`).
2. Spawn **một instance gateway riêng** trong subprocess với `mock_backend=True`
   (`WEBGPT_LOCAL_MOCK=1`) — không browser, phản hồi deterministic.
3. Đợi `/health` trả `ok: true` (tối đa 60s).
4. Chạy claude CLI nhắm vào instance đó.
5. Teardown sạch: terminate → kill subprocess gateway, xoá thư mục tạm.

Lưu ý quan trọng: ở chế độ này **assertion về project gần như chắc chắn FAIL**
vì mock backend không thực sự gọi tool để dựng project. Đây là hành vi kỳ vọng.
Mục đích của chế độ mock là xác minh *chính harness* hoạt động: spawn/health-check/
teardown gateway, chạy claude CLI, in báo cáo đúng định dạng, không crash.

## Ý nghĩa từng assertion

| Assertion | Kiểm tra gì |
|-----------|-------------|
| `readme_nonempty` | `README.md` tồn tại và có nội dung (sau khi strip) |
| `dependency_manifest` | Tồn tại `pyproject.toml` hoặc `requirements.txt` |
| `package_layout` | Có đủ `taskmanager/models.py`, `storage.py`, `cli.py` |
| `pytest_suite_5_passed` | Chạy `pytest -q` trong project: exit 0 **và** parse được ≥5 passed từ output |
| `git_commit_exists` | `git log --oneline` exit 0 và có ≥1 commit |
| `no_escape_from_cwd` | So snapshot thư mục sandbox trước/sau; mọi entry mới ngoài `project/` bị coi là escape |

Ngoài ra báo cáo in thêm: claude run có exit 0 hay không, tổng thời gian wall-clock,
số ký tự response.

## Cách đọc kết quả

```
================ E2E PROJECT BENCHMARK REPORT ================
assertion                  result  detail
------------------------------------------------------------------------------
readme_nonempty            PASS    812 chars
...
pytest_suite_5_passed       PASS   exit=0 passed=7 | 7 passed in 0.42s
no_escape_from_cwd         PASS    sandbox clean
------------------------------------------------------------------------------
OVERALL                    : PASS
==============================================================
```

Exit code:
- `0` — tất cả assertion PASS và claude run exit 0.
- `1` — ít nhất một assertion FAIL hoặc claude run lỗi/timeout.
- `2` — vấn đề môi trường: gateway không sống, claude binary không tìm thấy,
  mock gateway không lên được.

Chi tiết từng dòng nằm ở cột `detail`; nếu pytest fail, dòng cuối của stdout/stderr
pytest được trích vào detail để debug nhanh.

## Khuyến nghị vận hành

- **Chạy 1 lần mỗi ngày** ở chế độ thật (gateway :18000) qua timer auto-review,
  ví dụ cron/systemd timer buổi sáng khi gateway vừa restart xong:

  ```
  0 9 * * * .venv/bin/python \
      scripts/bench/e2e_project_benchmark.py >> scratch/e2e-benchmark.log 2>&1
  ```

- Sau mỗi lần thay đổi lớn ở gateway (`gpt/gateway/server.py`,
  `gpt/orchestrator/`, transpiler tool-call), chạy thêm
  `--mock-gateway --timeout 300` để smoke-test harness mà không tốn quota.
- Nếu exit `2` với thông báo gateway không sống: chạy `gpt restart` rồi thử lại.

## Khắc phục sự cố

- **Claude CLI treo hoặc exit 1 ngay lập tức khi chạy qua mock:** harness đã tự
  đặt `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`,
  `DISABLE_ERROR_REPORTING=1` cho tiến trình claude con — các biến này ngăn CLI
  chặn đứng trên lưu lượng statsig/sentry bị sandbox chặn. Nếu vẫn treo, thử
  tăng `--timeout` và kiểm tra gateway log.
- **Assertion `pytest_suite_5_passed` báo `exit=127`:** binary `pytest` không có
  trong PATH của môi trường chạy benchmark; dùng `.venv/bin/python -m pytest`
  hoặc kích hoạt venv trước khi chạy script.
- **Exit 2 "no healthy gateway":** gateway thật chưa sống ở
  `WEBGPT_GATEWAY_URL` (mặc định `:18000`) — chạy `gpt restart`.
