# PAYLOAD-BUDGET — Spec & kết quả (2026-08-25)

Mục tiêu PARITY: giảm size prompt gửi ChatGPT Web mà không mất khả năng tool-use của
claude CLI. Cơ sở: TRACE-FORENSICS 2026-08-24 — prompt ≤10k chars pass rate-limit ~94%,
>10k chỉ ~34.8%.

## 1. ĐO (pha 1) — 690 dump thật `wgs_*_pre_gpt` (prompt-debug)

Parser tách theo marker top-level (`<WEBGPT_MESSAGE role=…>` / `<WEBGPT_TOOL_RESULT>`;
payload JSON-encode nên marker không xung đột với nội dung).

Toàn bộ turns: p50 = 2,408; p90 = 42,414; max = 71,975 chars. **43.8% (280/639 dump chốt
ban đầu) vượt 10k**; trên bộ 690 dump cuối: 40.9% (282) — toàn bộ thuộc client
`claude-code` (47.5% turn của client này vượt).

| Thành phần      | p50  | p90   | max    | Vai trò ở turn vượt (>10k, n=280) |
|-----------------|------|-------|--------|-----------------------------------|
| system/developer| 0    | 28,200| 41,620 | **77.8% khối lượng**, trội ở 257/280 |
| tool contract   | 0    | 0     | 26,835 | 5.9% (chỉ protocol xml-era)       |
| bootstrap       | 0    | 0     | 163    | ~0%                               |
| lịch sử         | 663  | 2,392 | 48,160 | 7.6%                              |
| user cuối       | 0    | 336   | 14,682 | 8.3%                              |
| envelope        | 29   | 95    | 1,039  | 0.3%                              |

Ghi chú đo: escape overhead JSON chỉ ~2–15% (nhưng với payload nặng tag `<` có thể cao
hơn nhiều); `unknown` client không bao giờ vượt.

## 2. THIẾT KẾ (pha 2) — thứ tự trim an toàn

1. **Nén tool declarations**: JSON `Available tools: […]` — cắt description >48 ký tự,
   enum/default/title, giữ name + cấu trúc schema (type/properties/required/items) để
   model vẫn biết tên param khi emit XML.
2. **Cắt lịch sử cũ oldest-first** theo nhóm nguyên vẹn (assistant tool-call + results
   liền kề là một nhóm). Pin: system/developer, user đầu (objective), **user cuối**
   (gồm handshake), nhóm tool-call MỚI NHẤT + results tương ứng.
3. **Window head+tail cho system/developer quá cỡ** (65% head / tail, cắt tại biên
   dòng, chèn marker `[WEBGPT:BUDGET-TRIM]`). Budget tính trên chuỗi ENCODED, có hệ số
   tỷ lệ escape riêng từng payload; trailer phi-JSON bên trong block (dump đời cũ,
   chứa handshake) được giữ verbatim.
4. **Last resort**: window user đầu (objective). Bỏ qua nếu payload chứa sentinel
   protocol (`<cmd>`/`<json>`/`DISCOVER`/`WEBGPT`).

**Không bao giờ cắt**: bootstrap + controller contract (`<cmd>`/<json>, DISCOVER-FIRST —
đo được: DISCOVER chỉ nằm trong prefix gateway-injected, không bao giờ trong content),
user cuối, cặp tool-call/result mới nhất. Residual có chủ đích sau khi bật:
(a) turn có user cuối > ~4k (50 case), (b) turn xml-era mà prefix contract đã nén vẫn
>10k (19 case) — đúng hai nhóm được bảo vệ.

## 3. HIỆN THỰC (pha 3)

- File: `gpt/utils/promptcompat.py` — `enforce_prompt_budget()`,
  `get_prompt_budget_chars()`; tự kích hoạt trong `render_messages()` (điểm payload rời
  gateway, phủ cả `gateway/runtime.py` lẫn `mcp/bridge.py` mà không đụng file cấm).
- Flag: **`WEBGPT_PROMPT_BUDGET_CHARS`** (int; **default 0 = tắt**, byte-identical hiện
  trạng; khuyến nghị 10000). Không thêm vào `settings.py` vì pattern `WEBGPT_*` hiện có
  đều đọc `os.environ` trực tiếp (`conversations.py`, `multi_account.py`, …), còn
  `AppConfig` dùng tên không prefix.
- Test: `tests/test_prompt_budget.py` — 18 test: passthrough nguyên văn; thứ tự trim;
  bất khả xâm phạm handshake/user-cu/tool-pairing/DISCOVER; idempotent (kể cả case
  không thể đạt budget); trailer legacy; guard sentinel. **18/18 pass.**

## Kết quả chạy trên 690 dump thật (budget = 10,000)

| Tập                | >10k trước | >10k sau |
|--------------------|-----------|----------|
| Tất cả             | 40.9%     | **10.0%**|
| claude-code        | 47.5%     | **11.6%**|

Idempotent 100% (0/690 lệch khi áp 2 lần), 0 mất final-user/bootstrap, 0 lỗi cấu trúc
(re-parse round-trip khớp byte). Post p50 = 1,470; p90 = 12,412.
