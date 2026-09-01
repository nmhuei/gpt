# Practical CLI Bench (Claude Code CLI → webgpt gateway)

Ba bài thực tế "đời thường" cho Claude Code CLI chạy **qua gateway** (kiến trúc
stealth), xếp theo độ khó tăng dần, kèm **auto-grading máy chấm**: harness chỉ
tin bằng chứng cơ học (pytest exit code, subprocess gọi CLI, đo AST, hash diff),
không bao giờ tin lời model in ra.

- Script: `scripts/practical_cli_bench.py`
- Test harness: `tests/test_practical_cli_bench.py`
- Benchmark "dựng project từ đầu" cũ: xem `E2E_PROJECT_BENCHMARK.md`

## Ba bài

| Task | Fixture | Đề bài | Grading |
|------|---------|--------|---------|
| `bugfix` | `calc` — Python package có 1 bug thật (`average()` dùng `//` thay vì `/`) + 3 test fail | "Repo này có failing tests. Tìm và sửa bug, giữ style code." | 1. `pytest` exit 0. 2. File test không bị sửa (hash diff). 3. Diff chỉ đụng `calc/ops.py`. |
| `feature` | `notes` — CLI đọc/ghi JSON sẵn hoạt động (add/list/count/remove) | Thêm `--search <keyword>` vào subcommand `list`: lọc tiêu đề theo substring không phân biệt hoa thường, in mỗi tiêu đề một dòng theo thứ tự file, không khớp → không in gì + exit 0 | 1. Gọi CLI qua subprocess với 3 bộ input: khớp / không khớp / file rỗng. 2. Test cũ vẫn xanh. |
| `refactor` | `shop` — `process_order` dài 65 dòng toàn block copy-paste | Refactor thành các hàm nhỏ, hành vi không đổi | 1. Test hành vi (9 case) vẫn xanh. 2. Số dòng hàm gốc giảm ≥50% (AST: 65 → ≤32). 3. API public không đổi: tên hàm + signature `(items, customer=None)` (AST). |

Chi tiết chống gian lận đã tích hợp:

- `bugfix`: nếu model sửa test cho "xanh" thay vì sửa bug, hash diff bắt ngay.
- Bỏ qua nhiễu `__pycache__`, `.pytest_cache`, `*.pyc` khi tính diff.
- `feature`: case "không khớp" yêu cầu **exit 0 và stdout rỗng** — in dòng
  "No notes found" kiểu nào cũng FAIL.
- `refactor`: đo bằng `ast` (span `lineno..end_lineno`), không đếm tay; đổi tên
  hoặc đổi signature `process_order` đều FAIL dù test xanh.

## Cách chạy

### 1. Chế độ thật (mặc định) — tốn quota ChatGPT

```bash
gpt restart                                            # đảm bảo gateway sống ở :18000
gpt bench practical                              # toàn bộ practical suite
gpt bench practical --task feature --timeout 2400
```

Script probe `/health` của gateway trước; nếu chưa sống thì exit code `2` kèm
hướng dẫn. Biến môi trường: `WEBGPT_GATEWAY_URL`, `WEBGPT_API_KEY`,
`E2E_CLAUDE_MODEL` (giống `e2e_project_benchmark.py`).

Flag:

| Flag | Ý nghĩa |
|------|---------|
| `--task bugfix\|feature\|refactor\|all` | chọn bài (mặc định `all`) |
| `--timeout N` | timeout claude cho mỗi bài (mặc định 1800s) |
| `--base-url URL` | override URL gateway |
| `--keep-workdir` | giữ thư mục fixture tạm để soi |
| `--mock-gateway` | self-test harness, không tốn quota |

Exit code tổng: `0` khi mọi tiêu chí mọi bài PASS, `1` khi có FAIL.

### 2. Chế độ mock (`--mock-gateway`) — smoke-test harness

```bash
gpt bench practical --mock-gateway --task all --timeout 240
```

Tự chọn port trắng, spawn instance gateway riêng với `mock_backend=True`
(`WEBGPT_LOCAL_MOCK=1`), đợi `/health`, chạy claude nhắm vào đó, rồi teardown
(terminate → kill gateway, xoá tmp).

**Kỳ vọng:** assertion gần như chắc chắn FAIL (mock backend trả lời chữ, không
thật sự sửa file) — mode này chỉ chứng minh pipeline chạy sạch từ dựng fixture →
chạy claude qua gateway → grading → teardown, không phải kết quả bài.

## Output mẫu (mock run)

```
[harness] mock gateway healthy at http://127.0.0.1:56695 (port 56695)
========== TASK 'bugfix' REPORT ==========
criterion                            result  detail
------------------------------------------------------------------------------------
bugfix_pytest_green                  FAIL    exit=1 passed=None | 3 failed, 5 passed in 0.02s
bugfix_tests_untouched               PASS    test files clean
bugfix_only_logic_changed            PASS    diff confined to calc/ops.py
------------------------------------------------------------------------------------
claude run exit ok                   PASS
TASK OVERALL                         FAIL
...
================ PRACTICAL CLI BENCH SUMMARY ================
  bugfix     FAIL
  feature    FAIL
  refactor   FAIL
=============================================================
[harness] mock gateway stopped
```

## Kiến trúc grading (cho người sửa harness)

- Mỗi fixture là template nhúng trong script (`CALC_OPS_SRC`, `NOTES_CLI_SRC`,
  `SHOP_PRICING_SRC`, ...), dựng trong tmp qua `build_*_project()`; hàm build
  trả về snapshot hash toàn bộ cây để grader so diff.
- Grader thuần và inject được runner (`runner(cmd, cwd, timeout)`): unit test
  dùng `FakeRunner` ghi sẵn output pytest, không cần process thật.
- Helper đo AST: `count_function_lines()`, `top_level_functions()` (tên hàm +
  positional args cấp module).
- Chạy một bài không cần claude: `run_task(spec, sandbox, base_url, ...,
  skip_claude=True)` — dùng để chấm trực tiếp một cây fixture có sẵn.
