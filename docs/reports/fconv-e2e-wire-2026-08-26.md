# FCONV-E2E-WIRE — Wire test qua gateway stack (2026-08-26)

## VERDICT: E2E FAIL — đứt ở tầng CREDENTIAL SNAPSHOT (TokenManager `oai-device-id`), KHÔNG phải protocol fconv

Turn không bao giờ chạm ChatGPT backend: fail tại `_build_headers` trước khi có bất kỳ
call sentinel-prepare / f/conversation/prepare nào. Protocol fconv vẫn giữ verdict ALIVE
của Tick 177 (replay 4/4) — replay đã tự vá đúng gap này ở mức script, gateway stack thì chưa.

## Setup đã dùng

- Instance TEST port **18001**: `.venv/bin/python -m gpt.debug api-server --port 18001
  --transport hybrid --headless --persistent --profile-dir ~/Downloads/webgpt-e2e-profile
  --allow-authenticated` + env `WEBGPT_FCONV_PREPARE=1`, conversation-store/trace-file tách
  riêng khỏi production (`~/.local/share/webgpt/tmp/conversations-fconv-e2e.json`,
  `fconv-e2e-trace.jsonl`). Production 18000 (pid 2541480) không bị đụng tới.
- Profile: `cp -a ~/.local/share/webgpt/profiles/personal ~/Downloads/webgpt-e2e-profile`.
- Harness: `scripts/verify_hybrid_flip.py --base-url http://127.0.0.1:18001 --level t1`
  (ANTHROPIC_BASE_URL unset). Budget live turn: 3/3 đã dùng, dừng trung thực.

## Kiến trúc xác nhận (nghiên cứu bước 1)

- `--transport hybrid` → `HybridWorkerFactory` → `CurlCffiTransport`; browser chỉ giữ
  token page. `--transport browser` → DOM thuần, KHÔNG đi qua curl/fconv.
- Gate fconv nằm trong luồng send của curl transport:
  `gpt/transport/curl_transport.py:378` — `fconv = (not codex) and fconv_prepare_enabled()`;
  flag đọc env runtime tại `gpt/transport/token_manager.py:428` (`WEBGPT_FCONV_PREPARE`,
  default OFF). Khi ON: `_prepare_fconv_turn` (:612) chạy bootstrap proof → chat-requirements
  → PoW → POST `/backend-api/f/conversation/prepare` lấy conduit token, rồi envelope SSE
  authed (:1704+).

## Timeline 3 lần chạy

| # | Kết quả | Tầng |
|---|---------|------|
| 1 | `BrowserDisconnected`: CloakBrowser `Failed to create a ProcessSingleton` | Môi trường: `cp -a` sao kê cả symlink `Singleton{Lock,Cookie,Socket}` trỏ về PID browser production → Chromium từ chối launch. **Đã xử lý**: xoá 3 stale lock trong BẢN COPY (không đụng nguồn) |
| 2 | HTTP 200 stream error: `authentication_error` — "Direct backend generation is missing required credentials: **oai-device-id**" | Credential snapshot |
| 3 | Giống #2 (sau khi `invalidate_access_token` buộc re-extract, page đã load ~vài phút) | Credential snapshot |

Trace (`~/.local/share/webgpt/tmp/fconv-e2e-trace.jsonl`, seq 18–20):
`completionruntime/submit_start` → `submit_failed_before_commit_unknown`
(`error_type: "AuthRequired"`) → `api/request_completed` (error). Không có event nào
sau điểm submit — chuẩn xác "fail pre-flight".

## Root cause chain

1. `TokenManager._extract_all_unlocked` (`gpt/transport/token_manager.py:537-542`) lấy
   device id từ `localStorage['oai-device-id']` hoặc cookie tên `oai-device-id` /
   `oai_device_id`.
2. Thực tế profile (đã query sqlite Cookies DB bản copy): ChatGPT đặt cookie device id
   tên **`oai-did`** (`.chatgpt.com` + `.openai.com`) — không tồn tại cookie nào tên
   `oai-device-id`. localStorage leveldb của profile GỐC cũng không chứa key đó (grep
   nhị phân leveldb: 0 match cho cả `oai-device-id` lẫn `oai-did`).
3. → `bundle.oai_device_id = None` trên MỌI cold start → `CurlCffiTransport._build_headers`
   (`curl_transport.py:1771`) raise `AuthRequired` → mọi turn hybrid chết trước khi ra mạng.
4. Chưa từng bị phát hiện vì **production đang chạy `--transport browser`** (check
   `/proc/2541480/cmdline` + systemd unit): gate này chưa từng được exercise trên account thật.

## Đối chiếu Tick 177 (replay ALIVE)

`scripts/fconv_replay.py:220-230` gặp ĐÚNG gap này và tự xử lý: nếu
`bundle.oai_device_id` rỗng thì mint UUID4 thay thế ("server có thể gán lại") — và
ChatGPT chấp nhận (4/4 bước 200, SSE thật). Tức là: protocol OK với synthetic device id;
thiếu piece là fallback đó trong `TokenManager`.

## Việc còn thiếu trước khi flip production

1. **[BLOCKER] Fix TokenManager device-id sourcing** — một trong hai:
   thêm fallback đọc cookie `oai-did`; hoặc mint + persist UUID4 (parity với replay
   workaround, đã có bằng chứng server chấp nhận). Ưu tiên đọc `oai-did` (dùng đúng
   identity server đã gán).
2. Re-run FCONV-E2E-WIRE sau fix: kỳ vọng thấy evidence đường fconv trong trace/log
   (bootstrap proof → prepare → conduit token → SSE authed), không phải browser fallback.
3. Quy trình hot-copy profile cần ghi chú: phải xoá `Singleton{Lock,Cookie,Socket}` trong
   bản copy trước khi launch (lỗi #1 sẽ tái diễn với mọi cp -a khi browser nguồn đang sống).
   Lưu ý thêm: Local Storage leveldb copy-nóng có thể mất state — mọi giá trị phụ thuộc
   localStorage cần có fallback bền vững (xem #1).
4. (Khuyến nghị) Sau T1 PASS: chạy T2/T3 tool_use qua hybrid+fconv trước khi flip, vì
   flip ảnh hưởng toàn unit.

## Evidence files

- Log gateway test: `~/Downloads/webgpt-e2e-gateway.log` (3× `anthropic_live_stream_error`)
- Trace JSONL: `~/.local/share/webgpt/tmp/fconv-e2e-trace.jsonl` (20 events)
- Ledger test: `~/.local/share/webgpt/tmp/conversations-fconv-e2e.json`

## Dọn dẹp

- Instance test 18001: đã kill sau khi ghi report.
- `~/Downloads/webgpt-e2e-profile`: đã xoá.
- Không commit, không đụng code/unit/systemd (chỉ read-only + chạy lệnh).
