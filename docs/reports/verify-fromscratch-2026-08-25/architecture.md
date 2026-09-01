# Verify from scratch — Kiến trúc end-to-end WebGPT Gateway (2026-08-25)

READ-ONLY audit. Không sửa code, không pytest, không đụng :18000. Mọi tham chiếu `file:line` lấy từ working tree tại thời điểm audit (branch main, dirty).

## 0. Kết luận nhanh

- Import graph: **0/53 module lỗi import**, không có vòng import gãy (shim 1 dòng ở `gpt/*.py` → `gpt/utils/*`, `gpt/gateway/*`).
- Production chạy **stack gateway** (`gpt/gateway/server.py`), KHÔNG phải `gpt/api/server.py` như nhiều tài liệu/tests vẫn nghĩ.
- **Protocol-first hiện KHÔNG active**: systemd chạy `--transport browser` ⇒ mọi generation đi DOM (`UIDriver.send`). Protocol path thật = hybrid (curl_cffi), nhưng không được bật.
- 2 điểm đứt nghiêm trọng: (1) worker poisoning khi SSE deadline/client-disconnect huỷ turn giữa chừng; (2) race stream-deadline vs correction loop khiến đứt (1) chắc chắn xảy ra với correction dài.
- `_conversation_locks` / `_response_sessions` trong gateway ĐÃ có eviction/LRU (khớp với agent đang sửa); bản legacy `gpt/api/server.py` vẫn chưa có.

## 1. Sơ đồ luồng dữ liệu chính (production)

```
claude CLI (subprocess, env ANTHROPIC_BASE_URL=http://127.0.0.1:18000)
  spawn bởi: gpt/orchestrator/session_runner.py:293 run_claude_turn (:311 cmd)
             scripts/auto_solver.py:75 send_to_claude
    │ POST /v1/messages (stream)
    ▼
gpt/debug.py:789 cmd_api_server ──uvicorn──► Starlette app
  entrypoint chọn app: debug.py:823 "from gpt.gateway import create_api_app"
    ▼
gpt/gateway/server.py:1183 anthropic_messages
  ├─ :1188 parse_anthropic_request          (api/protocol_adapters)
  ├─ :1197 stream? → _anthropic_live_stream :1421
  │     ├─ :1453 on_delta = sieve chặn emit-tag (<cmd>/<json>/<WEBGPT_TOOL_CALL>/DSML/XML)
  │     ├─ :1506 task = _complete_anthropic(...)
  │     ├─ :1523 asyncio.wait(idle=15s) → ping; :1530 deadline → GenerationTimeout
  │     └─ :1605 finally cancel task + delta_task   ◄── ĐỨT #1 (mục 2)
  └─ non-stream → :1272 _complete_anthropic
        ├─ :1292 _record_for_pending_tool_results (ghép transcript Claude Code resend)
        ▼
      :854 complete_normalized
        ├─ :879/:889 conversations.resolve (+re-resolve sau lock :886)
        ├─ :886 _conversation_lock(session_id)  [waiter-count eviction :455-473]
        ├─ :909 pending_matches → :916 reconcile_pending
        ├─ :936 failover guard (multi-account only, transport/failover.py)
        ▼
      :1811 _execute_turn → gateway/runtime.py:1740 CompletionRuntime.execute
        ├─ :1307 execute_raw → _lease_for_record (:1012, introspect signature)
        │     ▼ server.py:557 _lease_session
        │       ├─ MultiAccountWorkerFactory.lease (server.py:587)
        │       ├─ HybridWorkerFactory.lease (transport/hybrid.py:307)
        │       └─ ChatGPTWorkerFactory.lease (transport/factory.py:233, affinity LRU :186)
        ├─ runtime.py:1345 execute_raw_on_session
        │     ├─ :1383 render_messages (promptcompat) + compact nếu >250k (:1390)
        │     ├─ :1422 soft-handshake nếu WEBGPT_TOOL_PROTOCOL=soft (_SOFT_HANDSHAKE_TEXT :853)
        │     ├─ :1470 conversations.mark_pending  ← persist ĐỒNG BỘ lên disk (crash-safe)
        │     ├─ :1492 position_session (open conversation / new_chat / select model)
        │     ├─ :1513 session.send(prompt, timeout=120s)
        │     └─ :1528 while True: correction loop (budget WEBGPT_MAX_CORRECTIONS,
        │            raise-sớm persistent :1556-1597, re-send :1667)
        ▼
ChatGPTWebSession.send — transport/session.py:395
  ├─ :443 protocol_driver.available?
  │    └─ LUÔN FALSE: session.py:109 tạo ProtocolDriver(page) không fingerprint/replay;
  │       drivers/protocol.py:34-40 yêu cầu fingerprint.verified ∧ ≥2 experiments ∧ replay
  ├─ → luôn rẽ :457 ui_driver.send = DOM (drivers/ui.py:821 composer.fill/click)
  │    └─ :448 ProtocolChanged → fallback UI (DOM là chính, không phải "fallback cuối")
  └─ events/deltas ngược qua _handle_driver_event :248 → runtime stream_callback
```

Nhánh hybrid (protocol-first đúng DECISIONS, hiện tắt):
```
HybridWorkerFactory.start (hybrid.py:234): browser start → new_page → TokenManager.extract_all
  (token_manager.py: browser mint access_token+cookies+sentinel; sentinel cache TTL 480s :66)
CurlCffiSession.send (hybrid.py:84) → CurlCffiTransport.send (curl_transport.py:124)
  → POST backend-anon/conversation trực tiếp, SSE decode bằng reverse/stream_parser.SSEDecoder
```

## 2. Điểm đứt luồng (break)

### ĐỨT #1 — Worker poisoning khi cancel giữa generation (nghiêm trọng nhất)
Chuỗi:
1. `_anthropic_live_stream` hết `stream_deadline_seconds` raise GenerationTimeout (server.py:1530-1537) HOẶC client disconnect → `return` (server.py:1527-1528).
2. `finally` cancel `task` (server.py:1610-1612) → CancelledError bay vào giữa `session.send`.
3. `session.send` chỉ bắt `except Exception` (session.py:493) — **CancelledError là BaseException, không bị bắt** → state machine kẹt ở `SENDING/WAITING_RESPONSE/GENERATING`, không bao giờ về READY.
4. Factory release coi state đó là healthy-reusable: factory.py:241-247 loại trừ `{FATAL_ERROR, BROWSER_DISCONNECTED, PAGE_CRASHED, RATE_LIMITED}` — GENERATING/WAITING_RESPONSE không nằm trong set → worker quay lại idle pool.
5. Turn sau trên worker đó (đặc biệt khi affinity map trùng conversation — factory.py:176-184 ưu tiên pinned worker): position bị skip vì `affinity_hit` (runtime.py:1237-1246) → `send` chạm ngay guard `state not in {READY, RETRYABLE_ERROR}` (session.py:407) → `WebChatError("Cannot send while session is GENERATING")` → 502 lặp lại.
6. Hồi phục chỉ khi một conversation KHÁC lease được worker và đi qua `open()`/`new_conversation()` (transition_to vô điều kiện, utils/state.py:140-150). Với 1 user + affinity, cặp (conversation, worker) độc hại gần như permanent cho tới restart.

### ĐỨT #2 — Race stream-deadline vs correction loop (kích hoạt ĐỨT #1)
- Deadline SSE: `stream_deadline = queue_timeout(180) + generation_timeout(120) + 30 = 330s` (server.py:2135-2146).
- Correction loop: budget systemd `WEBGPT_MAX_CORRECTIONS=4`; mỗi send (gốc + tối đa 4 correction) tự mang timeout 120s riêng (runtime.py:1513 và :1667-1669) → worst case ~600s.
- 600 > 330 ⇒ mỗi lần correction loop dài, live-stream CHẮC CHẮN chết 504 ở phút 5.5 trong khi backend vẫn đang chạy → cancel → ĐỨT #1. Non-stream API không bị (chờ tới cùng).

### ĐỨT #3 — Hybrid không thể reconcile (fail-closed chủ đích nhưng đứt retry an toàn)
`CurlCffiSession.reconcile` luôn raise `CommitUnknown` (hybrid.py:163-169). Hệ quả: đường `pending_matches → reconcile_pending` (server.py:909-929, gateway/runtime.py:1789) trên hybrid luôn không xác định; `maybe_failover` với CommitUnknown fail-closed (failover.py docstring) ⇒ crash giữa turn trên hybrid buộc client tự resend mù.

### ĐỨT #4 — MasterAgentOrchestrator không có cooperative cancel
`ClaudeCodeSessionRunner.solve_challenge` gọi `run_claude_turn(prompt)` / `execute_solve_script(timeout=35)` KHÔNG truyền `stop_event` (session_runner.py:526, :546, :587) ⇒ cơ chế SIGTERM/SIGKILL (B3-full) chỉ hoạt động trên đường `SwarmRaceSolver` (race_solver.py:179-217 truyền stop_event qua `_call_supporting_stop`). Trên master_agent path, hủy race chỉ có tác dụng ở ranh giới attempt.

### ĐỨT #5 — Legacy fork drift (bẫy người sửa nhầm file)
`gpt/api/server.py` là snapshot cũ của `gateway/server.py`: thiếu multi-account, failover, mock-backend, LRU `_response_sessions`, eviction `_conversation_locks`, sieve emit-openers trong live stream. Vẫn được `gpt/api/__init__.py:2` export và 10 test file import ⇒ ai sửa bug trên bản này sẽ tưởng đã fix mà production không thấy gì.

## 3. Dead code / module mồ côi

| Thành phần | Trạng thái | Bằng chứng |
|---|---|---|
| `gpt/api/server.py` (1757 dòng) | DEAD trong prod (fork cũ) | chỉ `gpt/api/__init__.py` + tests import; debug.py dùng gateway |
| `gpt/protocol_fast.py` + `gpt/drivers/protocol_fast.py` | DEAD | 0 importer ngoài nhau |
| `gpt/factory.py` (shim) | Gần-dead | chỉ tests/test_factory.py |
| `scripts/auto_solver.py` | Superseded | bản sync cũ của session_runner.solve_challenge, hardcode `/home/light/.local/bin/claude` |
| `solve_fast.py`, `solve_v2_fast.py` (root) | DEAD artifact CTF | 0 ref |
| `gpt/reverse/replay.py`, `normalize.py`, `diff.py`, `recorder.py`, `cdp_recorder.py`, `js_probe.py` | Chỉ tests + reverse/experiment dùng | ProtocolDriver không bao giờ được inject replay ⇒ `replay.py` không còn đường vào prod |
| `ProtocolDriver.send/history/models/select_model` | Unreachable | `available` luôn False (drivers/protocol.py:34-40) |
| `pcap_analyzer/`, orchestrator cli/race_cli | ALIVE | scripts + pyproject dùng |

Import graph: chạy `.venv/bin/python -c import` cho 53 module — 0 failure. Vòng `gpt/__init__ → gateway → api.* → utils.*` phân tầng shim nên load sạch.

## 4. Entry points

1. **systemd `webgpt-gateway.service`** (user unit, active): `python -m gpt.debug api-server --trace-file ... --port 18000 --transport browser --account personal --max-workers 8 --warm-workers 4 --queue-timeout 180 --headless --allow-authenticated` → `debug.py:789` → `gateway/server.create_api_app`. Đây là server duy nhất phục vụ claude CLI.
2. **`gpt/api/server.py`**: legacy standalone cùng tên class/hàm — TRÙNG CHỨC NĂNG gây nhầm (mục 2, ĐỨT #5). Khuyến nghị: xoá hoặc redirect tests sang gateway.
3. **`scripts/auto_solver.py`**: vòng CTF sync độc lập, trùng logic `orchestrator.session_runner.solve_challenge` (async). Hai vòng solve khác semantics (cancel, strategy rotate, instance-live wait) — dễ nhầm khi debug kết quả CTF.
4. Orchestrator clients: `orchestrator/cli.py` (MasterAgent, multi-challenge) và `race_cli.py` (SwarmRaceSolver 8 workers) — chỉ là *client* của gateway, không đụng browser.
5. Watchdog/timer: `webgpt-watchdog.timer` mỗi 5' gọi `scripts/webgpt-watchdog.sh`; auto-review daily 04:17.

## 5. Transport factory vs DECISIONS "Protocol-first"

- Flag quyết định: `--transport {hybrid|browser}` (debug.py:1112-1123, default **browser**) và `WEBGPT_TOOL_PROTOCOL` (xml/json-fn/both/soft — runtime.py:700-715).
- `hybrid` = đúng DECISIONS: browser chỉ làm máy mint token (`TokenManager.extract_all`, sentinel SDK in-page TTL 480s, token_manager.py:47-96), generation là curl_cffi POST thẳng backend-anon.
- **Production đang `--transport browser`** ⇒ toàn bộ generation là DOM scraping (fill composer + click send, ui.py:892-904) — trái memory-note "Protocol over DOM". Không có auto-switch browser→hybrid theo health; chiều ngược lại có (`ProtocolChanged`→UI fallback, session.py:448-455) tức DOM đang là *primary*, không phải fallback cuối.
- Path tự rơi DOM: mọi request qua `ChatGPTWebSession` (toàn bộ browser transport). Hybrid không có DOM scraping lúc serve (chỉ lúc mint token).

## 6. Orchestrator: correction/cancel/deadline khóa nhau thế nào

- **Correction (tầng gateway)**: budget `WEBGPT_MAX_CORRECTIONS=4`; kiểm tra tại runtime.py:1552 (`correction_count >= max_corrections` → MalformedToolCall). Raise sớm: soft-refusal tái diễn sau 1 correction (1556-1575); hard reason lặp y nguyên TOOL_REFUSAL/MALFORMED_TOOL (1576-1597). Mỗi correction embed `task_context` (LIVE-R3) và re-teach handshake khi web thread đổi (`_soft_handshake_needed` runtime.py:1272-1305).
- **Cooperative cancel (tầng orchestrator)**: global stop_event → per-worker relay (race_solver.py:20-35,141-142) → subprocess SIGTERM, grace 5s, SIGKILL, thu output tàn lưu (session_runner.py:87-163). Rollback `WEBGPT_COOPERATIVE_CANCEL=0`. Race solver set stop khi bắt được flag (race_solver.py:228) ⇒ các worker khác bị giết subprocess giữa chừng, phân biệt abort-vs-instance-chết bằng `InstanceNotLiveError` + check `stop_event.is_set()` (196-212).
- **Deadline**: worker deadline 3600s chỉ kiểm tra ranh giới attempt (race_solver.py:151-158); một turn claude ≤300s (session_runner.py:306) nên overshoot tối đa 1 turn. Instance-wait deadline 1800s có stop_event gate (374-461).
- **Race deadline↔correction**: tầng gateway có (ĐỨT #2). Tầng orchestrator không có race nguy hiểm vì deadline không cắt giữa attempt; nhưng chính vì vậy một attempt treo ≤300s vẫn giữ nguyên swarm.

## 7. TOP-5 nguy cơ RAM (chạy dài)

Xác nhận danh sách agent khác: `_conversation_locks` (gateway/server.py:455-473 waiter-count eviction — ĐÃ fix) và `_response_sessions` (OrderedDict LRU cap 512, server.py:395-497 — ĐÃ fix). Bản legacy `gpt/api/server.py` chưa có cả hai (:304/:382, :326/:740) nhưng không chạy prod. Thêm 5 chỗ:

1. **`ChatGPTWebSession._history_cache`** — list KHÔNG giới hạn (session.py:112). Mỗi `send` append `user_turn.text` = toàn bộ prompt rendered (≤ `WEBGPT_MAX_PROMPT_CHARS`=250_000 chars) + assistant text (session.py:461-471). Warm worker sống suốt tiến trình, chỉ clear khi `new_conversation` — một conversation claude code dài hàng trăm turns × 250KB × 8 workers ⇒ hàng trăm MB–GB trong python process.
2. **CloakBrowser/Chromium tree** — consumer lớn nhất hệ thống. Đo live lúc audit: 1 renderer 393MB @21% CPU, tổng chrome ~1GB mới với 1 context; `max_workers=8` ⇒ scale thẳng ×8 renderer + gpu/utility/zygote. OOM host hôm nay khả năng cao đến từ đây (python RSS chỉ 104MB lúc đo). Watchdog restart cả service sẽ dọn, nhưng giữa 2 lần restart không có reap theo RSS.
3. **ConversationStore + memo nhân bản transcript** — `record.messages` giữ full canonical transcript không cap per-record (conversations.py:106,:206), store giữ 64 records; `_canonical_memo` deep-copy 256 canonical lists (conversations.py:61-96); mỗi commit serialize toàn store ra JSON peak-RAM + disk (conversations.json tăng vô hạn về disk).
4. **`CurlCffiSession._events` queue không giới hạn (nhánh hybrid)** — `_emit` put mọi ResponseDelta vào `asyncio.Queue` (hybrid.py:51,:144-146); khi `stream_callback=None` (turn tool-call — case chính của claude code) không ai consume, worker warm tái sử dụng tích tụ vô hạn. `_event_history` có drain, queue thì không. Chỉ ảnh hưởng khi bật hybrid — cân nhắc fix trước khi switch protocol-first.
5. **Disk gián tiếp + prompt-debug/trace** — `prompt-debug` ghi .txt+.json cho MỌI correction turn (runtime.py:1098-1219, systemd bật dir này), `trace.jsonl` append vô hạn (utils/tracing.py:88-91; bus in-memory có bound 2000 events nên RAM ổn). Disk đầy ⇒ hành vi phụ. Subprocess zombie: KHÔNG phải risk — SIGKILL+wait reap đủ (session_runner.py:87-97); ephemeral profile /tmp chỉ tạo khi anonymous mode và BrowserManager không copy profile.

## 8. Đề xuất thứ tự xử lý (cho agent kế tiếp)

1. Fix ĐỨT #1: trong `finally` của `session.send` (hoặc wrapper runtime) reset state machine về READY/RETRYABLE_ERROR khi bắt CancelledError; hoặc factory release coi `{SENDING, WAITING_RESPONSE, GENERATING}` là not-reusable.
2. Fix ĐỨT #2: tính `stream_deadline` ≥ worst-case correction ((1+max_corrections)×generation_timeout + queue_timeout + slack), hoặc cấp correction-loop một overall deadline chung thay vì per-send.
3. Bật `--transport hybrid` trên systemd để thật sự protocol-first (sau khi vá mục 4-risk #4).
4. Xoá/neuter `gpt/api/server.py` fork + `auto_solver.py` sau khi chuyển tests.
