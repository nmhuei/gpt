#!/usr/bin/env bash
# Auto-review runner: pytest + ruff + git status/log, gộp thành markdown report.
# Exit 0 luôn kể cả khi pytest fail (chỉ ghi kết quả).
set -u

REPO="/home/light/GitHub/gpt"
REPORT_DIR="$REPO/docs/reports/auto-review"
KEEP=50

cd "$REPO" || exit 0

STAMP=$(date '+%Y%m%d-%H%M')
REPORT="$REPORT_DIR/auto-review-$STAMP.md"
mkdir -p "$REPORT_DIR"

pytest_out=$(.venv/bin/python -m pytest tests/ -q --tb=line 2>&1)
pytest_rc=$?

ruff_out=$(.venv/bin/ruff check gpt/ --statistics 2>/dev/null | head -20)

git_status_out=$(git status --short | head -30)
git_log_out=$(git log --oneline -5)

if (( pytest_rc == 0 )); then
    pytest_result="PASS"
else
    pytest_result="FAIL (exit code $pytest_rc)"
fi

{
    echo "# Auto-review $STAMP"
    echo
    echo "- Thời gian: $(date '+%Y-%m-%d %H:%M:%S %Z')"
    echo "- Host: $(hostname)"
    echo "- Kết quả pytest: **$pytest_result**"
    echo
    echo "## Pytest (\`.venv/bin/python -m pytest tests/ -q --tb=line\`)"
    echo
    echo '```'
    echo "$pytest_out"
    echo '```'
    echo
    echo "## Ruff check (\`.venv/bin/ruff check gpt/ --statistics\`, top 20)"
    echo
    echo '```'
    echo "${ruff_out:-<trống — không có vi phạm>}"
    echo '```'
    echo
    echo "## Git status (--short, tối đa 30 dòng)"
    echo
    echo '```'
    echo "${git_status_out:-<clean>}"
    echo '```'
    echo
    echo "## Git log (5 commit gần nhất)"
    echo
    echo '```'
    echo "$git_log_out"
    echo '```'
} > "$REPORT"

# Chỉ giữ tối đa $KEEP report mới nhất
ls -1t "$REPORT_DIR"/auto-review-*.md 2>/dev/null | tail -n +$(( KEEP + 1 )) | while IFS= read -r old; do
    rm -f -- "$old"
done

exit 0
