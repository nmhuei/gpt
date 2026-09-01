# Chính sách bảo mật

## Phạm vi

Đây là **tool cá nhân chạy local** (automation gateway), không phải dịch vụ public.
Repo này **KHÔNG được khuyến khích deploy public** hoặc expose ra mạng ngoài:
thiết kế giả định môi trường tin cậy trên máy cá nhân (`127.0.0.1`), không có
cơ chế hardening multi-tenant.

Nếu bạn fork và deploy ở nơi khác, bạn tự chịu trách nhiệm rà soát lại toàn bộ
giả định bảo mật (auth, binding địa chỉ, secrets trong `.env`).

## Cách báo cáo lỗ hổng

- Dùng **GitHub Private Vulnerability Reporting / Security Advisory** trên repo
  (tab Security → Report a vulnerability). Đừng tạo issue public cho lỗ hổng.
- Nếu không dùng được GitHub, liên hệ trực tiếp owner qua tài khoản GitHub `nmhuei`.

Báo cáo nên kèm: mô tả, bước tái hiện tối thiểu, phiên bản/commit, mức độ ảnh hưởng ước lượng.

## Thời gian phản hồi

Dự án cá nhân, phản hồi theo nỗ lực tốt (best-effort): xác nhận trong khoảng
**7 ngày**, khắc phục tùy mức độ nghiêm trọng và thời gian của owner.
Không cam kết SLA.
