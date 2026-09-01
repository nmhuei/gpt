1. Medium — correction telemetry is still inconsistent on the anti-repeat abort path. [gpt/gateway/server.py:197](/home/light/GitHub/gpt/gpt/gateway/server.py:197) and [gpt/api/server.py:355](/home/light/GitHub/gpt/gpt/api/server.py:355) derive `request_completed.correction_count` by counting `tool_correction` events. That event is emitted before the repeat check, while [runtime.py:2188](/home/light/GitHub/gpt/gpt/gateway/runtime.py:2188) correctly decrements the actual terminal count when it aborts before sending. Thus request-level telemetry overcounts by one whenever session attribution succeeds (and error responses can undercount to zero when no session header is emitted). Derive it from terminal runtime metadata, or subtract `persistent_correction_repeat`.

Area results:

- (1) CLEAN. The raw paired-span shield preserves offsets, allows an unmatched backtick inside a legitimate `<cmd>` body, and still masks tags inside normal fenced and inline-code echoes.
- (2) No double-decrement in runtime itself; one increment has exactly one abort-path rollback. Finding 1 remains for request telemetry.
- (3) CLEAN for late-fail behavior, ping termination, 529 mapping, header insertion, and 512-character JSON chunks. Unicode is sliced by Python code points, so multibyte UTF-8 characters are not split across SSE frames; headers are only added when absent.
- (4) CLEAN. Codex retry forces one refresh, latches distrust after the second 401, clears it only after accepted status, and fconv conduit 401/403 invalidates access credentials.
- (5) CLEAN. Backup rotation snapshots before replacement with private permissions, retains three generations, and missing-registry warning is appropriately conditional.

`git diff --check` passed. Focused pytest could not run because the read-only sandbox has no writable temporary directory.