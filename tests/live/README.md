# Opt-in live acceptance matrix

Live tests are intentionally not part of the offline suite. They act on a real
ChatGPT Web session and are valid only in the account mode below.

| Matrix | Account mode | Allowed checks |
| --- | --- | --- |
| `free_anonymous` | Free account, not authenticated | Browser/composer baseline, chat completion, streaming, tool loop, model/effort observations that are visible anonymously, reload/recovery, rate-limit mapping, Claude Code, OpenCode, soak, and final certification |

Any authenticated browser session (Free, Plus, Pro, or otherwise) makes the live
run invalid. Terminate it instead of falling back to an authenticated profile.
Every retained artifact must record `account_mode=free_anonymous`, fresh-browser
evidence, and the visible auth/composer state.

Suggested bounded command:

```bash
# free_anonymous: use an isolated ephemeral browser profile
gpt-web api-server --ephemeral --headful --max-workers 1
```

Before recording a result, include the exact UI label/selector evidence when
relevant, browser mode, and whether the browser session was fresh. If anonymous
quota/rate-limit is reached before a logical workflow begins, the harness may
replace the ephemeral anonymous session exactly once for that run. If the fresh
session is also limited, record the typed block and stop. Never loop identities,
and never rotate in the middle of a multi-turn/coding conversation.
