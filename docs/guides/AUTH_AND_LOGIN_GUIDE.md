# ChatGPT Web Gateway Layered Clean Architecture Implementation Plan & Auth Guide

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the ChatGPT Web Gateway codebase into a clean, modular Layered Architecture with a standardized `.env` configuration parser while preserving 100% feature parity (5.5 High effort selection, Headless CloakBrowser CDP, multi-worker concurrency, fast session/Gizmo bridge, and Anthropic/OpenAI API gateway).

**Architecture:** Split the monolithic repository into dedicated packages: `gpt/config/` (environment & settings), `gpt/auth/` (credentials & TOTP), `gpt/transport/` (browser lifecycle & session workers), `gpt/gateway/` (Starlette API server & protocol adapters), while maintaining facade compatibility in `gpt/__init__.py` and `gpt/debug.py`.

**Tech Stack:** Python 3.10+, Playwright, CloakBrowser, PyOTP, Starlette, Uvicorn, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-08-22-repo-architecture-refactoring-design.md`

## Global Constraints
- Preserve all existing unit tests without breaking existing public API signatures.
- Support both standard `.env` (`CHATGPT_EMAIL=...`) and legacy pipe format (`EMAIL|PASSWORD|TOTP`).
- Ensure 5.5 High auto-lock (URL + 88% slider click + pre-send guard) remains active.
- Default headless mode for zero desktop GUI interference.
- Maintain Ruff linter clean status (0 errors).

---

## 🔑 Hướng Dẫn Chi Tiết: Đăng Nhập & Quản Lý Tài Khoản ChatGPT Plus

### 1. Cấu Trúc Thông Tin Xác Thực Trong `.env`
Thông tin tài khoản Plus được cấu hình trong file `.env` tại thư mục gốc của repo:
```env
CHATGPT_EMAIL=loading_chassis.4e+kiweslum@icloud.com
CHATGPT_PASSWORD=Dekzti5Z!Aw6
CHATGPT_TOTP_KEY=5JSZWJVFYDHIU5ZZAGKU74PZUIC4N4RM
PROFILE_DIR=/home/light/Downloads/webgpt/cloak-profile
CDP_PORT=9222
API_PORT=8000
BROWSER_HEADLESS=true
```
- `CHATGPT_TOTP_KEY`: Khóa bí mật Base32 của 2FA. `gpt/auth/totp.py` sẽ tự động tính toán mã 6 số OTP theo thời gian thực mỗi khi đăng nhập.

---

### 2. Cách 1: Tự Động Đăng Nhập Không Cần Thao Tác (Zero-Interaction Auto Login)
Chạy lệnh đăng nhập tự động sử dụng module `gpt.auth`:

```bash
# Cách A: Dùng script Python tự động đọc từ .env
python3 -c "
import asyncio
from gpt.config import get_config
from gpt.auth import AutoLoginManager, LoginCredentials

async def main():
    config = get_config()
    creds = LoginCredentials(
        username=config.email,
        password=config.password,
        totp_secret_or_code=config.totp_key
    )
    mgr = AutoLoginManager(
        profile_dir=config.profile_dir,
        headless=config.headless,
        cdp_url=config.cdp_url
    )
    print(f'[*] Dang dang nhap tai khoan Plus: {config.email}...')
    ok = await mgr.login(creds, timeout_seconds=120)
    print(f'[+] Ket qua dang nhap: {\"THANH CONG\" if ok else \"THAT BAI\"}')

asyncio.run(main())
"
```

```bash
# Cách B: Dùng lệnh CLI gpt-web login
python3 -m gpt.debug login \
  --profile-dir /home/light/Downloads/webgpt/cloak-profile \
  --cred "$(grep CHATGPT_EMAIL .env | cut -d= -f2)|$(grep CHATGPT_PASSWORD .env | cut -d= -f2)|$(grep CHATGPT_TOTP_KEY .env | cut -d= -f2)"
```

> **Cơ chế hoạt động**:
> 1. Điền Email & Password tự động vào form đăng nhập Auth0/OpenAI.
> 2. Lấy secret seed từ `CHATGPT_TOTP_KEY`, sinh mã OTP 6 số qua `pyotp`.
> 3. Tự động submit form 2FA.
> 4. Lưu toàn bộ Cookies, Session Token và LocalStorage vào `/home/light/Downloads/webgpt/cloak-profile`.

---

### 3. Cách 2: Đăng Nhập Thủ Công Có Giao Diện (Manual Interactive Fallback)
Sử dụng khi OpenAI yêu cầu giải Cloudflare CAPTCHA hoặc Turnstile mà robot không tự vượt được:

```bash
# Mở trình duyệt CloakBrowser có giao diện để đăng nhập trực tiếp
/home/light/.cloakbrowser/chromium-146.0.7680.177.5/chrome \
  --user-data-dir=/home/light/Downloads/webgpt/cloak-profile \
  --remote-debugging-port=9222 \
  "https://chatgpt.com/auth/login"
```
1. Điền Email/Password và mã 2FA trên cửa sổ trình duyệt.
2. Khi thấy giao diện ChatGPT Plus và tên tài khoản xuất hiện ở góc dưới bên trái, **đóng trình duyệt lại**.
3. Toàn bộ thông tin xác thực đã được lưu vĩnh viễn trong `cloak-profile`.

---

### 4. Cách 3: Kiểm Tra Trạng Thái Đăng Nhập (Doctor Verification)
Để xác nhận profile đã đăng nhập thành công và sẵn sàng:

```bash
# Khởi động browser headless trên cổng 9222
python3 -m gpt.debug cloak-launch \
  --port 9222 \
  --profile-dir /home/light/Downloads/webgpt/cloak-profile

# Kiểm tra trạng thái xác thực
python3 -m gpt.debug doctor \
  --cdp-url http://127.0.0.1:9222 \
  --profile-dir /home/light/Downloads/webgpt/cloak-profile \
  --browser
```
> Kết quả mong đợi: `{"ok": true, "auth_status": "authenticated", "has_model_picker": true}`.

---

### 5. Cách Sử Dụng Tài Khoản Plus Cho Gateway & Claude Code CLI
Sau khi profile đã được xác thực:

1. **Khởi chạy Gateway Daemon**:
   ```bash
   python3 -m gpt.debug api-server \
     --port 8000 \
     --cdp-url http://127.0.0.1:9222 \
     --allow-authenticated \
     --max-workers 3 \
     --prewarm
   ```

2. **Chạy Claude Code CLI hoặc OpenAI Client trỏ về Gateway**:
   ```bash
   ANTHROPIC_BASE_URL="http://127.0.0.1:8000" \
   ANTHROPIC_API_KEY="dummy-key" \
   claude -p "Prompt của bạn ở đây"
   ```
   Gateway sẽ tự động nhận diện tài khoản Plus, tự khóa model **`GPT-5.5 Thinking (High Effort)`** và xử lý request mà không hiển thị cửa sổ trình duyệt.

---

## 📋 Task Checklist Tái Cấu Trúc (Đã Hoàn Thành)

- [x] **Task 1: Environment & Configuration Module (`gpt/config/settings.py`)**
- [x] **Task 2: Modularize Auth & TOTP Subsystem (`gpt/auth/`)**
- [x] **Task 3: Modularize Transport Layer (`gpt/transport/`)**
- [x] **Task 4: Modularize Gateway & Adapters (`gpt/gateway/`)**
- [x] **Task 5: Root Facade Compatibility & CLI Integration**
- [x] **Task 6: Live Manual Verification & Screenshot Evidence**
