# MODEL-ROUTING-RESEARCH — Route model theo đặc thù request trên gateway (2026-08-26)

Research agent, READ-ONLY code + web. Mọi claim kèm nguồn + ngày lấy (tất cả
fetch ngày **2026-08-26** trừ khi ghi khác). Nguồn mâu thuẫn được ghi cả hai
phía. Bối cảnh: quota ChatGPT tính THEO TOKEN, thinking/context nặng đốt nhanh
gấp bội (docs/reports/quota-pattern-research-2026-08-26.md §B4) ⇒ route task
nhỏ → model/effort nhẹ vừa tăng turn/ngày vừa giữ chất lượng.

## 0. TL;DR

**KHẢ THI — và phần lớn hạ tầng đã có sẵn.** ChatGPT Web chọn model bằng root
field `"model": "<slug>"` trong POST `/backend-api/f/conversation` (+ optional
`"thinking_effort"`) — chính xác shape payload builder nhà mình đang build.
Gateway đã nhận `model` từ CLI nhưng **cố tình IGNORE mọi identifier
`claude-*`** (ModelRegistry giữ model hiện tại của browser). Cần: (1) env
alias-map CLI-model → slug, (2) precheck availability, (3) verify
`resolved_model_slug` mà SSE parser **đã capture sẵn**. Rủi ro số 1 không phải
lỗi mà là **silent fallback/downgrade server-side** (đang diễn ra thật với Plus
tháng 8/2026). Effort ước tính: Phase 1 = S (~0.5–1 ngày).

---

## A. Cơ chế ChatGPT Web chọn model (2025–2026)

### A1. Conversation POST có root field `model` slug ✅ (đối chứng 2 chiều)

- Bằng chứng cộng đồng: user Plus inspect DevTools thấy request
  `/backend-api/conversation` mang `"model": "gpt-5-6-thinking"` +
  `"thinking_effort": "extended"` trong body (community.openai.com, yuanzhe007,
  **2026-08-23**).
- Bằng chứng first-hand: `_build_conversation_payload`
  (`gpt/transport/curl_transport.py:1082-1093`) build đúng shape này —
  `"model": request.model.id or label or "auto"` + `"thinking_effort"` khi có
  reasoning_effort — và đường fconv đã live-verify trong repo (header comment
  curl_transport.py:90). Prepare body cũng mang `model` (token_manager.py:372,
  `"model": model or "auto"`).
- Format slug phía chat-web: **dash** (`gpt-5-6-thinking`, `gpt-5-5-mini`);
  phía codex `/backend-api/codex/responses`: **dot** (`gpt-5.2`), dash bị
  reject server-side (comment curl_transport.py:1102-1103, spec
  codex-sse-spec-2026-08-25.md §2). Mapping phải tách theo path.
- URL param `https://chatgpt.com/?model={slug}` cũng set model cho composer —
  UI driver nhà mình đã dùng trong prod (drivers/ui.py:554-561) với slug_map
  hardcoded ("gpt-5.5"→`gpt-5-5-thinking`, "gpt-5.6"→`gpt-5-6-thinking`,
  "5.6 sol"→`gpt-5-6`, o3, gpt-4o).

### A2. Server công bố model THẬT đã phục vụ ✅

Stream metadata trả `message.metadata.model_slug` /
`resolved_model_slug`; telemetry riêng `turn_analytics.server_ste_metadata.model_slug`
(community.openai.com 2026-08-23/26 — nhiều user dùng để bắt downgrade).
**Nhà mình đã parse sẵn**: curl_transport.py:1979, :2009-2012, :2087 đưa slug
thật vào TurnResult.model ⇒ cơ chế verify routing KHÔNG phải code mới.

### A3. Discovery models khả dụng

- Không tìm thấy endpoint `/backend-api/models` public-stable được các project
  reverse dùng cho chat-web 2026; nguồn discovery đáng tin nhất trong repo là
  **UI capability snapshot** (`drivers/ui.py capabilities()/list_models()` +
  MODEL_PICKER_SELECTORS) — đọc trực tiếp picker của account đang login, tự
  động đúng theo plan. (chat2api README dùng cách map cứng tên; không thấy
  endpoint models nào được nêu — fetch 2026-08-26.)
- Kết luận: precheck availability nên đi qua `capabilities()` có sẵn, đừng đuổi
  endpoint chưa chứng minh.

### A4. Khác biệt theo plan (Plus/Pro/team)

| Plan | Model mặc định/picker | Nguồn |
|---|---|---|
| Free | GPT-5.6 **Luna** (tier nhẹ nhất) | ai-toolbox.co tổng hợp picker 2026, fetch 2026-08-26 |
| Go $8 | Luna + giới hạn cao hơn Free | felloai.com cập nhật 2026-08-23 |
| Plus/Pro/Team | GPT-5.6 **Sol** + effort slider; Pro-mode (deeper compute) gated Pro | như trên |
| Team/Business API-wrap | cần thêm header `ChatGPT-Account-ID` | chat2api README, fetch 2026-08-26 |

- GPT-5.6 family GA 2026-07-09, tiers Sol/Terra/Luna (GitHub Changelog
  openai/codex v0.143.0, fetch 2026-08-26).
- Slug retire thì conversation cũ **tự migrate** sang model thay thế, ví dụ
  GPT-5.1 retire 2026-03-11 → auto tiếp tục trên GPT-5.3 Instant / GPT-5.4
  Thinking / GPT-5.4 Pro (help.openai.com Model Release Notes, fetch
  2026-08-26) ⇒ slug mapping cần bảng "retired → replacement" hoặc chấp nhận
  server tự migrate.

## B. Hành vi khi slug không khả dụng / sai

| Path | Hành vi | Nguồn + ngày |
|---|---|---|
| f/conversation, slug lạ/không có quyền | **Silent fallback** về model default (không lỗi). VD slug gpt-5.5 sai → serve `gpt-5.3-mini` im lặng | OmniRoute #4665, 2026-06-22 |
| f/conversation, đổi model giữa chừng cùng conversation | Message *"The previous model used in this conversation is unavailable. We've switched you to the latest default model."* | ChatGPT_Model_Switcher README (repo archived 2025-04-04, hành vi vẫn được cộng đồng báo cáo 2026) |
| codex/responses (OAuth), model không entitlement/client-version | **Explicit error**: HTTP 404 `"Model not found gpt-5.6-luna"` | anomalyco/opencode #36140, 2026-07-09 |
| codex/responses, model unsupported khác | HTTP 400 `"model is not supported"` | hermes-agent #17533, 2026-04-29 |

### B3. RỦI RO SỐ 1 — silent downgrade đang xảy ra thật (tháng 8/2026)

- Plus request `gpt-5-6-thinking` → resolve `gpt-5-5-mini` trên **100% tin
  nhắn**, không warning, UI vẫn hiển thị 5.6; effort selector vô hiệu; hai
  cluster region (germanynorth, westus3); clear browser data đôi khi hết ⇒
  routing theo session/cohort (community.openai.com 2 thread, 2026-08-22→26;
  moderator xác nhận "team is actively working… deployed a fix").
- Pro account bị route GPT-5.3 Mini 48h giữa tháng 8/2026 (thread liên quan,
  fetch 2026-08-26).
- Hệ quả thiết kế: **đặt slug KHÔNG đảm bảo chất lượng**. Mọi route phải đối
  chiếu `resolved_model_slug` (§A2) và coi mismatch là tín hiệu vận hành
  (log/metric, không fail-hard).

## C. Đối chiếu code hiện có (READ-ONLY)

1. **Ingress**: `requested_model = body.get("model") or "chatgpt-web"`
   (gpt/requests.py:140). Claude Code gửi `claude-*`.
2. **ModelRegistry** (gpt/model_registry.py:28-52):
   - `chatgpt-web`/`default` → giữ model hiện tại browser;
   - **`claude-*` không có alias → passthrough, ui_label=None — IGNORE có chủ
     đích**; comment dòng 36-40: *"Keep the browser's current model unless an
     operator installs an explicit alias; never infer a matching ChatGPT model
     or account tier."* ⇒ flip hành vi này cần entry DECISIONS.md.
   - Alias khác → `ui_label`, áp vào phiên tại
     `gateway/runtime.py:1613 position_session` → `session.select_model()` +
     `select_reasoning_effort()`.
3. **Nguồn alias duy nhất**: JSON file `load_model_aliases()`
   (`--model-aliases-file`, debug.py:1021,1035). CHƯA có env map trực tiếp;
   settings.py chỉ có `DEFAULT_MODEL` env (= `gpt-5-5-thinking`,
   settings.py:11,124) dùng làm model mặc định phiên + inject
   `CLAUDE_DEFAULT_MODEL` cho CLI con (orchestrator/session_runner.py:301,
   debug.py:830) — đây chính là nút khiến CLI request một model-name cụ thể.
4. **Ba đường áp model**:
   - Browser/UI: drivers/ui.py:517-633 (slug_map + `?model=` URL + click
     picker; raise `ModelUnavailable` nếu picker không có option);
   - Protocol/hybrid: hybrid.py:98-99 passthrough chuỗi thô vào ModelInfo;
   - Payload: curl_transport.py:1059-1093 (fconv: dash-slug + thinking_effort)
     và :1095-1128 (codex: dot-slug, fallback `_DEFAULT_CODEX_MODEL="gpt-5"`,
     curl_transport.py:112).
5. **Effort**: parse được `reasoning_effort` / `reasoning.effort` OpenAI-style
   (requests.py:163-172); Anthropic `thinking:{type:"enabled"}` hiện bị chặn
   400 (protocol_adapters.py:379-383) ⇒ Claude Code CHƯA truyền effort được —
   muốn route effort theo task thì phải set ở gateway, không dựa CLI.
6. **Verify**: SSE parser đã capture `model_slug`/`resolved_model_slug` vào
   TurnResult.model (curl_transport.py:1979,2009-2012,2087) — chỉ còn thiếu
   so-sánh requested-vs-served.

## D. Thiết kế đề xuất

**Nguyên tắc ưu tiên: EFFORT-FIRST.** Picker 2026 đã gộp về ít model + effort
slider; effort là knob rẻ nhất (instant/low cho task nhỏ — turn nhanh, token
thinking ít; high cho task khó). Đổi slug chỉ dành cho phân tầng lớn
(mini-vs-thinking). Tránh pin slug hiếm (Sol/Pro-mode) — đúng vùng đang silent
downgrade.

### Phase 1 — Env alias map (effort S, ~0.5–1 ngày)

- Thêm `WEBGPT_MODEL_ALIASES` (JSON inline) hoặc `WEBGPT_MODEL_ALIASES_FILE`
  vào settings.py, merge vào ModelRegistry bên cạnh file alias hiện có.
- Mapping mặc định đề xuất (env-overridable, rỗng = giữ hành vi hiện tại):
  `claude-*-haiku* → <slug-mini-plan> + effort instant`;
  `claude-sonnet* (default) → giữ model phiên (chatgpt-web)`; task nặng
  (correction_count ≥ 1 hoặc prompt lớn) → `gpt-5-5-thinking` + effort high —
  policy classify đặt ở completion runtime, KHÔNG sửa protocol adapter 400.
- Tách format theo path: alias value dạng `{"fconv": "...", "codex": "..."}`
  hoặc quy ước dash/dot tự convert (chỉ khi ký tự đầu khớp pattern).
- Điều kiện: DECISIONS.md entry mới (registry hiện ignore claude-* một cách
  chủ ý); giữ opt-in env OFF để parity không đổi.

### Phase 2 — Precheck + fallback (effort M, ~1 ngày)

- Trước `position_session`: check slug ∈ `session.capabilities().models`
  (drivers/ui.py đã expose); thiếu → đi theo fallback chain env
  (`WEBGPT_MODEL_FALLBACKS`, JSON array), cuối chain = giữ nguyên phiên.
  Tránh raise `ModelUnavailable` giữa turn.
- Pin model per-conversation: không đổi slug giữa các turn của cùng web thread
  (tránh message "previous model unavailable", §B).

### Phase 3 — Verify served slug (effort M, ~0.5–1 ngày)

- So `TurnResult.model` (đã capture) vs requested sau mỗi turn; mismatch →
  telemetry event + metric; tùy chọn feed breaker/failover như tín hiệu phụ
  (liên hệ LIMIT-SIGNATURE-TAXONOMY, RESET-AWARE-COOLDOWN đã done 2026-08-26).
- Không fail-hard: silent downgrade là hành vi server-side ngoài kiểm soát
  (§B3).

### Rủi ro tổng hợp

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Slug sai format/path (dash↔dot) → fconv nuốt im lặng, codex 404/400 | CAO | Tách map per-path + test unit cả 2 builder |
| Silent downgrade server-side (Plus 8/2026) | CAO, ngoài tầm soát | Verify resolved_model_slug + metric, không tin UI label |
| `ModelUnavailable` giữa turn khi picker không có option | TB | Precheck capabilities + fallback chain |
| Đổi model giữa chừng conversation → banner switch-to-default | TB | Pin per-conversation |
| Parity regression: registry vốn ignore claude-* có chủ đích | TB | Opt-in env OFF mặc định + DECISIONS.md + goldens |
| Retired slug tự migrate bất ngờ | THẤP | Bảng replacement hoặc chấp nhận; log slug mỗi turn |

## E. Đề xuất row ROADMAP (không tự sửa ROADMAP.md)

```
| MODEL-ROUTING | Env alias-map CLI-model → ChatGPT slug (WEBGPT_MODEL_ALIASES, opt-in OFF;
  fconv dash / codex dot) + effort-first policy ở completion runtime; precheck
  capabilities() + fallback chain; verify resolved_model_slug đã capture sẵn trong
  TurnResult.model → telemetry mismatch (không fail-hard). Yêu cầu DECISIONS.md entry
  (ModelRegistry hiện ignore claude-* có chủ đích, model_registry.py:36-46) | transport | TODO (S→M) |
```

## Nguồn (toàn bộ fetch 2026-08-26)

- community.openai.com — "Plus users silently routed to GPT-5.5-mini when
  selecting GPT-5.6 Sol" (yuanzhe007, 2026-08-23; merged thread chạy
  2026-08-22→26): body `model`+`thinking_effort`, response
  `resolved_model_slug`, telemetry `server_ste_metadata.model_slug`.
- community.openai.com — "ChatGPT Web requests GPT-5.6 Sol but server resolves
  GPT-5.5-mini on Plus accounts" (2026-08-22→26): 100% messages mini, 2 region
  cluster, session-cohort routing, staff "fix deployed".
- github.com/diegosouzapw/OmniRoute #4665 (2026-06-22): slug lạ → silent
  fallback gpt-5.3-mini trên backend-api.
- github.com/anomalyco/opencode #36140 (2026-07-09): codex OAuth HTTP 404
  "Model not found gpt-5.6-luna".
- github.com/hermes-agents/hermes-agent #17533 (2026-04-29): HTTP 400 "model
  is not supported".
- github.com/hydrotho/ChatGPT_Model_Switcher (archived 2025-04-04): message
  "previous model … switched you to the latest default model"; ẩn model theo
  quyền account.
- github.com/lanqian528/chat2api README: map tên model → slug nội bộ
  (text-davinci-002-render-sha cho non-gpt-4), Team cần ChatGPT-Account-ID.
- help.openai.com Model Release Notes: GPT-5.1 retire 2026-03-11 → auto-migrate
  GPT-5.3 Instant / GPT-5.4 Thinking / GPT-5.4 Pro.
- ai-toolbox.co (2026-01-06) + felloai.com (2026-08-23): picker 2026, plan
  Free/Go Luna vs Plus/Pro Sol, Pro-mode gated Pro.
- GitHub Changelog openai/codex v0.143.0 (2026-07-09): GPT-5.6 family GA,
  tiers Sol/Terra/Luna.
- Repo (first-hand, read-only): curl_transport.py:90,1082-1093,1095-1128,112,
  1979,2009-2012,2087 · token_manager.py:372 · model_registry.py:19-52,55-69 ·
  requests.py:140,163-172 · drivers/ui.py:351-633 · hybrid.py:87-99 ·
  gateway/runtime.py:1613-1646 · api/server.py:1110-1134,2005-2030 ·
  config/settings.py:11,124 · orchestrator/session_runner.py:301 ·
  debug.py:830,1021-1035 · protocol_adapters.py:293,362-383.
