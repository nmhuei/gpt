# CTF Challenge Picker — Verify-Loop Tooling (2026-08-24)

Công cụ chọn bài CTF làm bài kiểm tra cho vòng lặp auto-solver.

- Script: `scripts/pick_ctf_challenge.py`
- Test: `tests/test_pick_ctf_challenge.py` (11 passed)
- Kết quả chạy thật: `docs/reports/ctf-candidates-2026-08-24.json`

## Kết quả quét thật (`--probe-remote 10`)

| Chỉ số | Giá trị |
|---|---|
| Thư mục bài quét được | 499 |
| Loại (tổng) | 326 |
| — đã giải (có flag.txt) | 28 |
| — NEEDS_HUMAN_REVIEW.md | 4 |
| — không rõ đề | 128 |
| — không giải được (không file đính kèm, không remote URL) | 166 |
| Nhận (candidates) | 173 |
| Local-only / có remote URL | 164 / 9 |
| Probe remote (10) | 10/10 alive |

## Top 10 candidates dễ nhất

| # | Tên | Category | Nguồn | Điểm |
|---|---|---|---|---|
| 1 | Invoice | onboarding | local | 35 |
| 2 | Decompile? | onboarding | local | 40 |
| 3 | HRBot | onboarding | local | 40 |
| 4 | Slis | crypto | local | 100 |
| 5 | Brunner Radio | crypto | local | 100 |
| 6 | Company Discount | forensics | local | 100 |
| 7 | Go Go Decompile | rev | local | 100 |
| 8 | Brunner Mifflin (User) | boot2root | local | 100 |
| 9 | π-crypt 0.57 | crypto | local | 100 |
| 10 | KPWhy | rev | local | 100 |

(Thứ tự theo `ease_score`: ưu tiên bài giải LOCAL, difficulty easy, điểm thấp, nhiều lượt solve.)

## Hướng dẫn dùng trong vòng lặp (cron)

Mỗi lần cần một bài verify MỚI (chưa từng dùng):

```bash
cd /home/light/GitHub/gpt

# 1. Lấy danh sách candidates (không probe mạng khi chỉ cần bài local)
.venv/bin/python scripts/pick_ctf_challenge.py \
    --output docs/reports/ctf-candidates-latest.json --min-count 5

# 2. Lấy bài đầu tiên chưa dùng từ JSON (trường used_at == null),
#    giao cho solver, sau đó đánh dấu đã dùng:
.venv/bin/python scripts/pick_ctf_challenge.py \
    --output docs/reports/ctf-candidates-latest.json \
    --mark-used "/home/light/Workspace/CTF/<event>/<cat>/<challenge>"

# Hoặc đánh dấu cả loạt: --mark-used ALL
```

- State file mặc định: `scripts/.ctf_used_challenges.json` (map path → timestamp
  `used_at`). Candidate đã dùng bị loại khỏi lần chạy sau; `--include-used` để xem lại.
- Sau khi solver giải xong (flag.txt xuất hiện), bài tự động bị loại vĩnh viễn ở các
  lần quét sau nhờ tiêu chí `already_solved(flag.txt)` — state file chỉ cần cho các
  bài KHÔNG giải được nhưng đã thử.
- Gate CI: `.venv/bin/python -m pytest tests/test_pick_ctf_challenge.py -q`.

## Giới hạn đã biết

- Heuristic "file đính kèm" loại trừ `*.py`, artifact solve/debug/gdb — bài chỉ còn
  script solve của ta sẽ bị coi là không-local (an toàn về phía loại).
- Thư mục chứa thư mục-bài con không được tính là bài (quy tắc leaf-most); một bài
  có thư mục con mang README riêng sẽ bị nuốt vào container cha.
