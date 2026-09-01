# Mid-wave Smoke — 2026-08-26

QA smoke giữa wave merge hôm nay (correction tighten, codex12/13 fix, image pipeline,
late-fail, dedup, polish bundle, refusal mapping, breaker taxonomy, registry backup...).

## 1. Full pytest suite

Lệnh: `.venv/bin/python -m pytest -q` (không --timeout)

```
FAILED tests/test_picker_needs_remote.py::test_zip_with_redacted_flag_excluded
FAILED tests/test_picker_needs_remote.py::test_zip_redacted_reason_string - A...
2 failed, 1218 passed in 20.09s
```

## 2. Offline golden evals

Lệnh: `.venv/bin/python evals/run_evals.py --filter all`

```
EVALS RESULT: total=24 pass=24 xfail=0 skip=0 fail=0
```

Toàn bộ 24 golden PASS (gồm correction-tighten, codex12/13 freshness, image placeholder,
late-fail event, placeholder excision FIX-A...).

## 3. Chẩn đoán 2 fail (KHÔNG sửa — chờ coordinator dispatch)

Cả hai fail cùng một gốc, nằm ở **CTF picker tooling**, không phải gateway runtime:

| Test | Assert hỏng | Thực tế |
|---|---|---|
| `test_picker_needs_remote.py::test_zip_with_redacted_flag_excluded` | `sum(hits.values()) == 1` với key prefix `redacted_flag_in_zip` | got 0 — reason không còn tên `redacted_flag_in_zip*` |
| `test_picker_needs_remote.py::test_zip_redacted_reason_string` | `reason.startswith("redacted_flag_in_zip(")` | thực tế `'redacted_flag_in_archive(chall.zip:src/flag.txt)'` |

**Nghi nguyên:** đổi tên taxonomy reason trong `scripts/pick_ctf_challenge.py`
(dòng 598: `return True, f"redacted_flag_in_archive({label})"`, mtime 26/08 14:17 —
nhánh archive của breaker taxonomy/polish bundle hôm nay). File test mới
`tests/test_pick_ctf_challenge.py` (mtime 14:18) đã cập nhật đúng tên mới và PASS;
riêng `tests/test_picker_needs_remote.py` (mtime **25/08 21:26**) bị sót, vẫn expect
tên cũ `redacted_flag_in_zip(`.

**Fix đề xuất (cho agent được dispatch):** cập nhật `tests/test_picker_needs_remote.py`
dòng 171 và 184 từ prefix `redacted_flag_in_zip` → `redacted_flag_in_archive`.
Không cần đổi code picker.

## 4. Verdict

**KHÔNG boundary-ready** — 2 fail (cùng gốc, test-only, phạm vi hẹp: CTF picker).
Core gateway + evals 24/24 xanh nên không có dấu hiệu các wave gateway phá nhau;
chỉ cần 1 fix nhỏ phía test rồi chạy lại suite là boundary-ready.
