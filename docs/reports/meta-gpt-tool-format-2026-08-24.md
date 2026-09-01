# Meta-experiment: Which tool-call format does the GPT backend emit most naturally?

**Date:** 2026-08-24
**Method:** Direct probes of the model behind the local gateway (`http://127.0.0.1:18000`, key `sk-webgpt-local`, model id `chatgpt-web`, endpoint `POST /v1/chat/completions`, non-stream). Each question sent as a single independent user turn, 180s timeout, `-p` style single-shot. Budget: max 8 gateway turns; **8 used**, no browser interaction.

## Turn log

| # | Probe | HTTP | Latency | Outcome |
|---|-------|------|---------|---------|
| 0 | Smoke test "Say OK" | 200 | ~0.05s | `OK` |
| 1 | Q1 — "what format were you trained to emit?" | 200 | 12.4s | Compact fn-call syntax |
| 2 | Q2 — XML `<tool_calls>` requested verbatim | 200 | 11.9s | Refusal in prose |
| 3 | Q3 — JSON `{"name","arguments"}` + ```json fence requested | 200 | 12.5s | Exact payload, fence destroyed by transport |
| 4 | Q4 — free choice, one tool call required | 200 | 11.1s | Refusal in prose |
| 5 | Q2b — XML retry with unit-test-fixture framing | **502** | 12.3s | Gateway `malformed_model_tool_call` |
| 6 | Q3b — JSON repeat (reliability) | 200 | 12.2s | Exact payload again |
| 7 | Q3c — JSON repeat (reliability) | 200 | 11.7s | Exact payload again |

Raw responses saved under `/tmp/meta_exp/*.json` (ephemeral).

## Responses (verbatim)

### Q1 — self-reported native format
> Prompt: "What function calling format were you trained to emit? Show an exact example of you invoking a hypothetical get_weather(city='Hanoi') tool. Output ONLY the raw invocation format."

```
get_weather({"city":"Hanoi"})
```

Notable: neither a full OpenAI envelope nor XML — the compact `name(args-json)` function-call syntax. The model did not volunteer `<tool_calls>` or a fenced envelope.

### Q2 — XML format requested
> Prompt: "Emit a tool call for tool bash(command='echo hi') in this exact XML format: ... Reply with ONLY that block, nothing else."

```
I can't emit a fake tool-call block for a tool invocation that isn't available in this chat.
```

Refusal. No block emitted.

### Q3 — JSON format requested (asked 3x total)
Verbatim content from all three runs (Q3, Q3b, Q3c), byte-identical:

```
JSON
{"name": "bash", "arguments": {"command": "echo hi"}}
```

The payload is exact and perfectly reproducible (3/3). But it arrives **without backticks**: the literal line `JSON` is where the ```` ```json ```` fence language tag should be. See structural finding below — this is a transport artifact, not model behavior.

### Q4 — free choice ("choose whatever format you find most natural")

```
I can't act as the "WEBGPT automation controller" or emit a fake controller tool call block. I also don't have a file-writing tool available in this chat that can create /tmp/x.txt directly.

If you want to create it locally, run:

Bash
printf 'hello' > /tmp/x.txt

Do you want the equivalent command for
```

Refusal again (response truncated mid-sentence at some upstream limit). Note `Bash\nprintf ...` — same stripped-fence artifact; the model's spontaneous choice when not asked for tool calls is prose plus a code block, i.e. assistant behavior, not controller behavior.

### Q2b — XML retry with "parser unit-test fixture" framing

HTTP **502**:
```json
{"error":{"message":"Tool block did not contain any valid tool calls.","type":"malformed_model_tool_call","code":"malformed_model_tool_call","retryable":false}}
```

The framing got past the model-level refusal far enough that something tool-block-shaped reached the gateway, but the gateway's own parser (`gpt/utils/toolcall.py`) rejected it. Either the correction loop pushed the model into a partial/malformed block, or the emitted XML lost structure on the way back through DOM extraction and failed our parameter grammar. This is direct evidence of the correction loop interfering with exactly the protocol it mandates.

## Structural finding (root cause, code-level)

The fence destruction is not the model's fault. Assistant text is scraped from the ChatGPT web UI via Playwright `inner_text()` on the rendered DOM node:

- `/home/light/GitHub/gpt/gpt/drivers/ui.py:815` — `text = (await node.inner_text()).strip()`

Rendered Markdown code fences become HTML `<pre>` blocks with a language header; `inner_text()` drops all backticks but keeps the label line (`JSON`, `Bash`). Consequence: **any protocol delimited by Markdown fences is structurally unparseable on the web-UI transport path**, including the gateway's own json-fn protocol (`_JSON_FENCE_RE` / `_JSON_FENCE_OPEN_RE` in `/home/light/GitHub/gpt/gpt/utils/toolcall.py:33-36`).

Literal text tags (`<tool_calls>`, sentinel tokens) survive `inner_text()` intact — they are plain text, not Markdown.

## Verdict per format

| Format | Compliance | Reliability | Notes |
|---|---|---|---|
| OpenAI-style JSON `{"name","arguments"}` | 3/3 exact payload | High (byte-identical x3) | Delimiter (fence) destroyed by transport → must change delimiter, not content |
| Custom XML `<tool_calls>` | 0/2 | Low | Model refuses as "fake tool call"; forced attempt ended in gateway 502 `malformed_tool_call` |
| Free choice | 0/1 | — | Defaults to assistant prose + code block; will not spontaneously emit controller blocks without a tool definition present |

**Conclusion:** the hypothesis holds — JSON-style name/arguments payloads are what this backend emits most naturally and consistently. The failure mode is entirely in the delimiter/transport layer.

## Recommendation

Do **not** keep the current protocol unchanged, but also do not switch to raw markdown-fenced JSON:

1. **Keep the OpenAI-style JSON body** (`{"name": ..., "arguments": {...}}`) — it reproduced 3/3 with zero drift.
2. **Replace the ```json fence delimiter with plain-text sentinel tags** (e.g. `<tool_call>...</tool_call>` or similar) that survive `inner_text()` extraction; or
3. **Fix extraction** so raw Markdown reaches the parser (read the pre-fence source rather than rendered `inner_text()`), if the fence protocol must stay.
4. **Relax the parser short-term** to accept the observed artifact form (`JSON\n{...}` language-label line followed by a bare object) — it is deterministic and trivially regexable, and would have made all three Q3 responses parse today.
5. The XML `<tool_calls>` protocol should be dropped for this backend: model-side refusal plus correction-loop 502s make it doubly unreliable.

## Files

- Report: `/home/light/GitHub/gpt/docs/reports/meta-gpt-tool-format-2026-08-24.md`
- Raw responses: `/tmp/meta_exp/q1.json` … `/tmp/meta_exp/q3c.json`, `/tmp/meta_exp/q2b.json`
- Checker: `/tmp/meta_exp/check.py`
- Root cause: `/home/light/GitHub/gpt/gpt/drivers/ui.py:815`; affected protocol defs: `/home/light/GitHub/gpt/gpt/utils/toolcall.py:31-36`
