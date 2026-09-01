# ACCOUNTS-REGISTRY-RESILIENCE — 2026-08-26

Phản ứng cho sự cố `~/Downloads/webgpt/accounts/accounts.json` bị xoá sáng nay
(FAILURES.md entry 2026-08-26): gateway crash-loop
`Unknown account profile: personal` 94 lần respawn trước khi tái tạo thủ công.

## 1. Truy thủ phạm — KẾT LUẬN: KHÔNG tìm thấy trong repo

Timebox ~20' đã dùng. Toàn bộ bề mặt xoá đã được soát, **không có đường code
hoặc test nào trong repo có thể xoá `accounts/accounts.json`**:

### Caller của các hàm destructive (`accounts.py` unlink/rmtree)

| Điểm gọi | Target | Có trúng registry? |
|---|---|---|
| `gpt/debug.py:300` → `AccountStore.remove(delete_profile=...)` | profile_dir + .cred file | Không — chỉ CLI thủ công; registry bị *ghi đè* chứ không bị xoá |
| `accounts.py` `delete_credentials`/`remove` | `.cred` file | Không |
| `gpt/utils/profile.py:44` `create_ephemeral_profile.cleanup` | tmp dir tự tạo dưới `Downloads/webgpt/tmp/webgpt-anon-*` | Không |
| `gpt/orchestrator/session_runner.py:289` | `~/.claude/projects/*` khớp tên task | Không |
| `tests/test_review_gate.py:88` | pytest basetemp (`rt-*`) | Không |
| `scripts/run-claude-code-benchmark.sh:37` `rm -rf` | `$benchmark_root` = mktemp dưới `runs/claude/` | Không |

Không có script/.sh/systemd unit nào `rm` nhắm vào `accounts/` hay root webgpt.

### Nghi vấn env-rò trong tests — SẠCH

- `tests/test_preflight_quota.py`: 100% fake HTTP + token cache tmp_path; test
  subprocess duy nhất chỉ chạy `--help`. Không chạm registry thật.
- `tests/test_debug_login.py`: các test account set
  `WEBGPT_ACCOUNTS_FILE`/`WEBGPT_PROFILES_ROOT` vào tmp_path (:264-267, :322-325).
- `tests/test_account_default.py`: fixture `tmp_store` scrub env + tmp_path.
- **Không test nào** dựng bare `AccountStore()` (resolve DEFAULT_WEBGPT_ROOT).
- Lỗ hổng tiềm tàng ghi nhận để phòng ngừa sau: `tests/conftest.py`
  `_scrub_host_env` **chưa** scrub `WEBGPT_ACCOUNTS_FILE`/`WEBGPT_PROFILES_ROOT`
  — hiện vô hại vì không test nào phụ thuộc mặc định, nhưng nên bổ sung khi
  đụng lại conftest.

### Khoảng trống còn mở

Thủ phạm nằm ngoài bề mặt đã audit: khả năng cao là thao tác ngoài repo (xoá
thủ công, tool ngoài, hoặc process không thuộc source tree này). Không đủ
evidence để chốt. Phòng thủ phía code bên dưới đảm bảo sự kiện tương tự lần
sau (a) có bản sao khôi phục ngay trên đĩa, (b) được log lộ liễu ngay từ đầu
crash-loop thay vì respawn câm 94 lần.

## 2. Backup-on-write (`gpt/auth/accounts.py`)

- `_backup_registry()` mới: trước mỗi lần `_write()` ghi đè registry, copy
  trạng thái cũ thành `accounts.json.bak.1`, xoay vòng tối đa 3 bản
  (`.bak.1` mới nhất … `.bak.3` cũ nhất) ngay cạnh registry.
- Rotation bằng `Path.replace` (atomic cùng filesystem); backup giữ mode 0600.
- Lỗi backup chỉ log WARNING, không bao giờ chặn write chính.
- Write vẫn atomic như cũ (tmp file + `replace`). First-write khi registry chưa
  tồn tại thì không sinh backup (không có gì để snapshot).

## 3. Startup warn

- Chọn đặt trong `AccountStore.__init__` (`_warn_if_registry_missing`) thay vì
  sửa `gateway/server.py`: mọi điểm khởi tạo store — gateway multi-account
  bootstrap, health loop, api/server, CLI debug — đều được phủ, và tránh hoàn
  toàn việc đụng vùng stream/error-mapping dày code của server.py.
- Điều kiện: registry KHÔNG tồn tại AND profiles_root có entry con →
  `logger.warning("accounts registry missing but profiles exist — possible
  deletion, check .bak backups next to the registry (registry=… 
  profiles_root=… entries=[…])")`, kèm preview tối đa 8 tên profile còn sót.
- Registry tồn tại, hoặc profiles_root rừng hoang → im lặng.

## 4. Tests (`tests/test_accounts.py`, +6)

Backup-on-write:
1. `test_first_write_leaves_no_backups`
2. `test_write_snapshots_previous_registry_as_bak` (nội dung cũ + mode 0600)
3. `test_backup_rotation_keeps_at_most_three` → đúng tên `.bak.1..3`, thứ tự
   snapshot mới→cũ xác minh theo payload, state già nhất bị rotate out.

Startup warn:
4. `test_init_warns_when_registry_missing_but_profiles_exist`
5. `test_init_silent_when_registry_present`
6. `test_init_silent_when_no_profiles_yet`

## 5. Verify

- Targeted: `tests/test_accounts.py test_account_default.py
  test_account_health.py test_multi_account.py test_debug_login.py
  test_preflight_quota.py test_server_leakfix.py test_api_server.py` →
  **153 passed** (80+58 lần chạy đầu, 95 gộp sau lint-fix; tổng 2 lượt đầy đủ
  đều xanh).
- `ruff check` sạch trên cả 2 file (đã kèm fix 3 lỗi style pre-existing trong
  accounts.py: import sort, RUF036, UP037).
- `mypy --follow-imports=silent` trên 2 file: chỉ còn 1 lỗi pre-existing
  `cloakbrowser` import-not-found (package không phát hành stub; không phải
  regression của task này).
- Không commit, không restart gateway, không động registry thật.
