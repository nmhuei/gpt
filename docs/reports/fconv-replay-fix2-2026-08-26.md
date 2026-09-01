# FCONV-NOTOKEN-REPLAY fix #2 — SSE read crash (2026-08-26)

## Symptom

Step 4 of `scripts/fconv_replay.py` got **HTTP 200 + `content-type: text/event-stream`**
(prepare chain + conduit token handshake succeeded) but crashed reading the body:

```
async for chunk in response.aiter_bytes():
AttributeError: 'Response' object has no attribute 'aiter_bytes' (did you mean: 'aiter_lines'?)
```

The response comes from a curl_cffi async session (`CurlCffiTransport._post_conversation`).

## Root cause

curl_cffi's async `Response` (`.venv/lib/python3.14/site-packages/curl_cffi/requests/models.py`)
exposes `aiter_content(chunk_size=None, decode_unicode=False)` and `aiter_lines(...)`
— there is **no `aiter_bytes`** (that name belongs to httpx). Verified by introspection:

```
aiter_bytes: False   aiter_content: True   aiter_lines: True
```

The production transport already handles this: `CurlCffiTransport._response_chunks`
(`gpt/transport/curl_transport.py:2010`) probes `aiter_bytes` → `aiter_content` →
`aiter_lines`. The replay script hardcoded the httpx-only name.

## Fix

`scripts/fconv_replay.py`, step 4 preview block: resolve the iterator via
`aiter_content` (fallback `aiter_lines`) instead of `aiter_bytes`; if a
`aiter_lines` str chunk arrives it is re-encoded as `chunk + "\n\n"` to keep SSE
event boundaries, mirroring `_response_chunks`. Evidence logic unchanged: stop at
500 chars, print first 500 chars, always close via `transport._close_quietly`.

## Verdict impact

None — this only unblocks evidence collection after the already-successful 200.
The branch is ALIVE pending a clean re-run with streaming evidence.

## Verification

- `py_compile`: OK
- `--help` exit 0; dry-run plan prints all 4 ladder steps, guard intact
- `--live` NOT fired from this session (coordinator owns the live window)
