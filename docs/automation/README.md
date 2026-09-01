# WebGPT Automation State

Bộ file trạng thái cho vòng lặp tự động 24/7 (memory-bank pattern). Session mới BẮT BUỘC đọc 4 file này trước khi làm gì khác.

- `ROADMAP.md` — danh sách task có ID + status
- `DECISIONS.md` — quyết định đã chốt (append-only, không sửa lại)
- `STATE.md` — cursor hiện tại: đang làm gì, bước kế tiếp, lệnh verify
- `FAILURES.md` — hướng đã thử và thất bại (chống lặp vô hạn xuyên session)

Quy tắc vận hành (từ nghiên cứu best-practice 2026):
1. Mọi dispatch xong → cập nhật STATE.md trước khi thoát
2. Test fail được fix tối đa 2 lần liên tiếp; lần 3 → blocked + ghi FAILURES.md
3. Phân loại transient/permanent trước khi fix: chạy lại suite 1 lần, fail ổn định mới là permanent
4. Không auto-commit lên main; mỗi task một branch `task/T-xx`
5. Task bị blocked 2 lần không có input người → done-as-blocked, không dispatch lại
