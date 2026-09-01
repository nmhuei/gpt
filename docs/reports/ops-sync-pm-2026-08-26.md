# OPS-SYNC PM 2026-08-26 — đồng bộ hồ sơ quyết định & ops

docs-agent, read-only trừ docs. Không sửa code, không commit.

## Đã thêm

1. **docs/automation/DECISIONS.md** — +4 entry `[2026-08-26]`:
   - Chính sách quota vận hành: ≤8–10 msg/ngày/account · preflight wham/usage bắt buộc trước batch (exit 0/2/3) · phân loại 429 / DOM-dialog / park reset_at / challenge. Nguồn: quota-pattern-research-2026-08-26.md.
   - CODEX-AUTH path: AT web-session bị codex backend từ chối (liveprobe 401) → PKCE mint ngoài CLI qua `scripts/codex_oauth_login.py` + `WEBGPT_CODEX_AUTH_JSON`.
   - IMAGE-UPLOAD-WEB: recipe Azure blob 3 bước chấp nhận triển khai flag `WEBGPT_IMAGE_UPLOAD_WEB` OFF fail-open placeholder.
   - ACCOUNTS registry: backup-on-write `.bak.1..3` xoay vòng + startup warn khi registry thiếu nhưng profiles còn (sau sự cố xoá accounts.json).

2. **docs/guides/AUTOMATION_OPS.md** — +mục 8 "Vận hành batch":
   - 8.1 quy trình preflight (`scripts/preflight_quota.py`, bearer nguồn, exit 0 OK / 2 DEFER / 3 UNKNOWN, cảnh báo chưa live-verify + 401 web-session).
   - 8.2 quy tắc cooldown 1-chu-kỳ khi RateLimited (chờ trọn cửa sổ breaker, probe lại đúng 1 lần, kinh nghiệm MED-BATCH4).
   - 8.3 lưu ý probe T1 nhỏ PASS ≠ upstream khoẻ với turn lớn (quota token-weighted; bằng chứng VERIFY-R10 pass nhưng MED-BATCH4 0/5 commit_unknown).

3. **.env.example** — cả 2 placeholder đều THIẾU, đã thêm commented cuối block opt-in:
   `WEBGPT_USAGE_POLL_SECONDS` (default 0 = tắt, advise_pressure 85%) và
   `WEBGPT_IMAGE_UPLOAD_WEB` (OFF fail-open, chỉ nhánh fconv).
