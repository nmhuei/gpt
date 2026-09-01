# FCONV-E2E-WIRE RE-RUN — Wire test qua gateway stack sau fix OAI-DID-FALLBACK (2026-08-26)

## VERDICT: E2E PASS

T1 streaming turn qua hybrid transport + `WEBGPT_FCONV_PREPARE=1` thành công end-to-end.
Điểm hỏng cũ (credential snapshot thiếu `oai-device-id` → `AuthRequired` pre-flight) đã được
fix `oai-did-fallback-2026-08-26.md` xử lý đúng; đường fconv (prepare chain → authed envelope)
chạy thật, KHÔNG phải browser fallback.

## Setup

- Profile: `cp -a ~/.local/share/webgpt/profiles/personal ~/Downloads/webgpt-e2e-profile`,
  xoá 3 symlink `Singleton{Lock,Cookie,Socket}` trong bản copy trước khi launch (bài học lần 1 —
  lần này không tái diễn lỗi ProcessSingleton).
- Instance TEST :18001, pid 3212797:

  ```
  WEBGPT_FCONV_PREPARE=1 WEBGPT_RUNTIME_ROOT=/tmp/e2e-fconv-runtime \
    .venv/bin/python -m gpt.debug api-server --port 18001 --transport hybrid --headless \
    --profile-dir /home/light/Downloads/webgpt-e2e-profile --allow-authenticated \
    --trace-file /tmp/e2e-fconv-runtime/tmp/fconv-e2e-trace.jsonl
  ```

  **Lệch so với lệnh parent**: dùng `--profile-dir` + `--allow-authenticated` thay vì
  `--account personal`, vì account registry (`~/.config/webgpt/accounts.json`) resolve
  `personal` về **profile production** (`~/.local/share/webgpt/profiles/personal`) đang bị
  browser sống giữ lock — chạy nguyên văn sẽ đụng production. Cần ghi vào runbook: mode
  `--account` luôn trỏ profile gốc; hot-copy profile phải đi kèm `--profile-dir`.
- Production :18000 (pid 2541480): không đụng, còn sống sau test (đã verify).

## Kết quả T1 (`scripts/verify_hybrid_flip.py --level t1 --timeout 120`, 1/3 turn budget)

```
[T1] PASS (11.032s)
http_status=200, delta_count=5, first_token_latency_s=11.03, message_stop=true, error=null
text tail: "…16 17 18 19 20"
```

OVERALL: PASS ở lần chạy đầu, không cần retry.

## Evidence đường fconv

1. **Process env** (`/proc/3212797/environ`): `WEBGPT_FCONV_PREPARE=1`; không có
   `WEBGPT_CODEX_SSE` (process env lẫn `.env`) → tại `curl_transport.py:377-378`
   `fconv = (not codex) and fconv_prepare_enabled()` = True. Hybrid không có browser
   fallback cho send: `_prepare_fconv_turn` (bootstrap proof → chat-requirements → PoW →
   POST `/backend-api/f/conversation/prepare` lấy conduit token) là điều kiện bắt buộc
   trước mọi POST; nó fail thì turn fail.
2. **Trace** `/tmp/e2e-fconv-runtime/tmp/fconv-e2e-trace.jsonl` (10 events):
   seq 7 `completionruntime/submit_start` → seq 8 `submit_completed`
   (`conversation_id=6a8ec3fc-f7f4-83ec-b347-2fca777504f0`) → seq 9 `assistantturn/parsed`
   → seq 10 `request_completed`. Không còn cặp
   `submit_failed_before_commit_unknown` + `error_type="AuthRequired"` của lần trước.
3. **Token cache** `<profile-copy>/webgpt-token-cache.json` (mode 0600, viết 17:46):
   `cookies["oai-did"]="84ab6…"`, `oai_device_id="84ab67f2-7cd7-40b9…58d884"` — khớp prefix
   cookie → device id lấy từ cookie thật `oai-did` theo ưu tiên mới của fix (không phải
   UUID mint), persist đúng schema cũ.
4. **Latency**: first token 11.0s gồm trọn prepare chain (proof + requirements + PoW +
   prepare) rồi SSE — hợp lý cho đường authed-fconv, khác hẳn fail tức thì (<2s) lúc
   pre-flight chết.

Lưu ý: stdout log gateway chỉ 6 dòng uvicorn (path fconv chỉ `logger.warning` khi fail,
success im lặng) — evidence dựa trên env + trace + cache như trên, đủ chốt nhưng nếu muốn
observability tốt hơn trước flip có thể thêm một dòng log INFO khi prepare chain xong.

## Việc còn thiếu trước flip production

1. **T2/T3 tool_use qua hybrid+fconv** trên account thật (report trước mục #4) — flip ảnh
   hưởng toàn unit, chưa nên flip chỉ với T1 text-only.
2. Runbook: chế độ `--account NAME` trỏ profile gốc — E2E/test với profile hot-copy phải
   dùng `--profile-dir` + `--allow-authenticated`; xoá Singleton* trong bản copy là bắt buộc.
3. (Khuyến nghị nhỏ) Log INFO một dòng khi `_prepare_fconv_turn` hoàn tất (kèm conduit
   present/absent) để lần sau có evidence dương tính trực tiếp thay vì suy diễn env+trace.
4. Production vẫn đang chạy `--transport browser`; flip = đổi unit systemd sang hybrid
   (+ giữ `WEBGPT_TOOL_PROTOCOL=soft`), làm sau khi T2/T3 xanh.

## Dọn dẹp

- Instance 18001 đã kill (:18001 closed, không còn process).
- Đã xoá `~/Downloads/webgpt-e2e-profile` và `/tmp/e2e-fconv-runtime`.
- Giữ lại: `~/Downloads/webgpt-e2e-gateway-rerun.log` (6 dòng, user tự xoá).
- Không sửa code, không commit, không restart gateway production, không đụng :18000.
