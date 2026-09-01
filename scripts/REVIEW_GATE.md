# Review Gate — cổng kiểm duyệt tự động

`scripts/review_gate.py` là **một lệnh duy nhất** tổng hợp chất lượng thay đổi,
dùng làm điều kiện chặn trước khi mọi thay đổi được coi là hoàn tất trong vòng
lặp 24/7.

## Cách dùng

```bash
# Đầy đủ (tóm tắt + JSON ở cuối)
python scripts/review_gate.py

# Chỉ JSON (máy đọc, dùng cho automation)
python scripts/review_gate.py --json

# Chạy trên repo khác (mặc định: thư mục cha của scripts/)
python scripts/review_gate.py --repo /path/to/repo
```

Yêu cầu: chỉ stdlib Python. Tự gọi subprocess cho `pytest`, `ruff`, `git`.
Thiếu ruff/mypy hay git không có diff thì script vẫn chạy bình thường
(kèm ghi chú trong `notes`), không bao giờ crash. Mypy chỉ chạy khi repo khai báo
`[tool.mypy]` để không áp một policy ngầm lên repo không dùng typecheck.

## Gate kiểm tra gì

| Thành phần | Lệnh | Kết quả |
|---|---|---|
| Test suite | `.venv/bin/python -m pytest tests/ -q` | passed / failed / errors |
| Linter | `ruff check . --output-format=concise` | toàn repo, tôn trọng exclude trong `pyproject.toml` |
| Typecheck | `mypy . --no-error-summary` | chạy khi có `[tool.mypy]`; lỗi type là blocking |
| Diff | `git diff --stat HEAD` (+ `--numstat`) | số file đổi, +/- dòng |
| Danger scan | các dòng mới trong diff + file untracked trong `gpt/` | danh sách vi phạm |

Danger patterns được quét:

- `while True` không có hint deadline/timeout (`deadline`, `timeout`,
  `os.environ`, `break`, ...) trong phạm vi ±5 dòng → **high**
- `verify=False` (tắt TLS verification) → **medium** theo quyết định hardening hiện hành
- Secret-like string dạng `sk-[a-z0-9]{20,}` → **high**
- `except Exception: pass` (nuốt exception) → **medium**
- Hardcode đường dẫn `/home/<tên>/` → **medium**

## Verdict và quy tắc chặn

| Verdict | Exit code | Điều kiện |
|---|---|---|
| `PASS` | 0 | pytest xanh, Ruff sạch, mypy sạch/n/a, không danger pattern |
| `WARN` | 1 | chỉ có Ruff lỗi hoặc danger mức medium |
| `FAIL` | 2 | pytest failed/errors > 0, mypy error > 0, hoặc danger mức high |

**Quy tắc chặn cho vòng lặp 24/7:** một thay đổi chỉ được coi là hoàn tất khi
gate trả về `PASS`. `WARN` cho phép tiếp tục nhưng phải được ghi nhận;
`FAIL` bắt buộc sửa lại trước khi commit/hợp nhất.

## Định dạng JSON đầu ra

```json
{
  "verdict": "FAIL",
  "pytest": {"passed": 401, "failed": 0, "errors": 0},
  "ruff_errors": 23,
  "ruff_by_rule": {"F401": 5},
  "ruff_available": true,
  "mypy_errors": 0,
  "mypy_available": true,
  "mypy_ran": true,
  "danger": [{"file": "gpt/x.py", "line": 10, "pattern": "verify=False", "severity": "medium"}],
  "diff_summary": {"files_changed": 34, "insertions": 1907, "deletions": 383, "files": []},
  "notes": []
}
```

## Test

```bash
.venv/bin/python -m pytest tests/test_review_gate.py -q
```

9 scenario: repo sạch PASS; pytest fail FAIL; mypy error có config FAIL; `while True` không deadline FAIL;
`while True` có timeout không bị FAIL; secret token FAIL; `verify=False` WARN +
`except Exception: pass` WARN; diff rỗng vẫn chạy; thư mục không phải git vẫn chạy.
Mỗi scenario dựng repo git tạm trong `tmp_path` rồi gọi gate như subprocess.
