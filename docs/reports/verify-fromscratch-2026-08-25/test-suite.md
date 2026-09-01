# Test Suite Health Verify — 2026-08-25 (phân tầng, read-only)

Agent: verify-only. Không sửa file source nào. Không chạy full suite một lệnh. Không đụng http://127.0.0.1:18000.
Môi trường: `.venv/bin/python` (pytest 9.1.1), mỗi cụm một lệnh với `PYTEST_ADDOPTS="--basetemp=/tmp/pytest-v2-<cum>"`, timeout 90s/cụm (`timeout -k 5 90`).

## Bỏ qua do đang được edit

- `tests/test_api_server.py`
- `tests/test_session.py`
- `tests/test_conversations.py`

(2 agent khác đang edit các file này + code liên quan — chạy bây giờ chỉ ra kết quả nhiễu.)

## Kết quả theo cụm (70 file / 697 test)

| # | Cụm | Files | Pass/Fail | Thời gian |
|---|------|-------|-----------|-----------|
| 1 | config | test_config_settings, test_profile, test_runtime_paths | 6/0 | 0.28s |
| 2 | auth | test_auth, test_auth_totp, test_authenticator_fixes, test_accounts, test_account_default, test_debug_login | 31/0 | 0.63s |
| 3 | transport-core | test_curl_transport, test_factory, test_stream_delta_v1, test_stream_hygiene, test_token_manager, test_token_cache_disk | 52/0 | 0.37s |
| 4 | sentinel/token-mint | test_sentinel_cache, test_sentinel_flow, test_sentinel_sdk_mint | 23/0 | 0.27s |
| 5 | multi-account/failover/cf | test_multi_account, test_account_health, test_failover, test_cf_resilience | 37/0 | 0.98s |
| 6 | orchestrator | test_orchestrator, test_orchestrator_deadlines, test_cooperative_cancel | 21/0 | 5.41s |
| 7 | gateway-runtime | test_gateway_agent_loop, test_worker_affinity, test_server_leakfix | 26/0 | 2.23s |
| 8 | gateway-behavior | test_correction_context, test_discover_policy, test_refusal_detection, test_prompt_budget, test_prompt_intent_matrix | 83/0 | 0.52s |
| 9 | gateway-streaming | test_delta_tooluse_and_handshake, test_prose_correction_live, test_stealth_protocol, test_stream_close_and_crash | 51/0 | 0.76s |
| 10 | api-adapters | test_protocol_adapters, test_requests, test_client_fixtures, test_usage_estimation, test_messages | 53/0 | 0.46s |
| 11 | tools/transpiler | test_tool_transpiler, test_tool_protocol_variants, test_toolstream, test_claude_bootstrap_full_tools, test_claude_code_conformance | 163/0 | 1.08s |
| 12 | fault/tracing | test_fault_injection, test_tracing, test_review_gate | 27/0 | 5.94s |
| 13 | ui/drivers/perf | test_ui_errors, test_ui_stream_hygiene, test_model_effort, test_perf_quickwins | 35/0 | 2.84s |
| 14 | reverse/probe | test_dom_probe, test_normalize, test_redaction, test_trace_diff, test_trace_replay, test_stream_parser, test_streaming_contract | 17/0 | 0.39s |
| 15 | state/store/misc | test_state, test_verification, test_persist_async_store, test_runtime_stress | 12/0 | 0.92s |
| 16 | scripts/bench | test_e2e_benchmark_harness, test_soak_runner, test_practical_cli_bench, test_pick_ctf_challenge, test_pcap_analysis_pipeline | 86/0 | 6.06s |

## Tổng hợp

- **Tổng test đo được: 697** (trong 70 file, loại 3 file bị loại trừ).
- **Tỷ lệ xanh: 100%** (697/697 pass).
- **Permanent fail: 0** — không có fail nào để phân loại.
- **Flaky/transient: 0** — toàn bộ cụm pass ngay lần chạy đầu, không cần re-run.
- **Treo (timeout >90s): 0** — không cụm nào bị kill.
- Tổng thời gian đo ~28s (cộng dồn từng cụm).

## Ghi chú

- Không chạy lại test fail nào vì không có fail.
- Cụm 15 (`test_persist_async_store`, `test_runtime_stress`) và cụm 8 (`test_correction_context`) import `ConversationStore`/`gpt.conversations` — vùng code liên quan đang được agent khác edit; hiện vẫn xanh, nhưng kết quả có thể đổi sau khi edit xong.
- Log thô từng cụm: `/tmp/v2-{config,auth,transport,sentinel,multacct,orch,gwrt,gwbeh,gwstr,api,tools,fault,ui,reverse,misc,bench}.log`.
