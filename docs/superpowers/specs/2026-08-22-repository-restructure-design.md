# Repository Restructure Design

## Goal

Make the repository easier to navigate without changing gateway behavior, public
CLI commands, HTTP routes, or the existing test contract. The frozen pre-change
worktree is archived at `~/Downloads/webgpt/backups/gpt-freeze-20260822-221318/`.

## Scope

The work reorganizes source, tests, scripts, and documentation around their
responsibilities. It does not add a direct ChatGPT transport, alter security
handling, or change the active browser/runtime behavior.

## Source layout

The public package remains `gpt`, and existing imports continue to work through
small compatibility modules while callers migrate.

```
gpt/
  api/                 # OpenAI, Responses, and Anthropic HTTP adapters
  cli/                 # CLI parser and command implementations
  runtime/             # transaction engine, factory, persistence, tracing
  tools/               # tool schema, parser, stream sieve, assistant turns
  web/                 # browser manager, session, auth, profiles, UI drivers
  reverse/             # redacted observation, capture, normalization, replay
  models/              # model registry and request model selection
```

`gpt.__init__` remains the intentionally small public API. Legacy top-level
modules become temporary re-export shims only when an existing internal test,
documented public API, or package export imports them directly. New internal
code must import the destination module.

### Initial move map

| Current responsibility | Destination |
| --- | --- |
| `browser.py`, `session.py`, `auth.py`, `profile.py`, `drivers/` | `web/` |
| `completionruntime.py`, `conversations.py`, `factory.py`, `tracing.py`, `runtime_paths.py`, `verification.py` | `runtime/` |
| `toolcall.py`, `toolstream.py`, `assistantturn.py`, `mcp_bridge.py` | `tools/` |
| `model_registry.py`, `requests.py` | `models/` |
| `debug.py` | `cli/main.py`, then retain `gpt.debug` as CLI compatibility entrypoint |
| `protocol_fast.py`, `bqa_installer.py` | reviewed individually; move only after their callers are explicit |

The large `gpt/api/server.py` is split by responsibility before relocation:
request/error utilities, OpenAI streaming encoders, request dispatch, session
lease/lifecycle, and application routing. `create_api_app` and
`WebChatAPIServer` remain stable imports.

## Tests and scripts

Tests are grouped without changing pytest discovery:

```
tests/
  unit/                # pure parsing, state, redaction, model, path tests
  integration/         # API/runtime/factory/session behavior with fakes
  contracts/           # client fixtures and stream/protocol contracts
  live/                # opt-in browser acceptance material
  benchmark/           # PCAP and external-client harnesses
```

Scripts are grouped by invocation purpose:

```
scripts/
  verify/              # offline checks and focused verification
  live/                # opt-in browser/manual helpers
  benchmark/           # PCAP, Claude Code, and OpenCode runners
  reverse/             # lifecycle observation helpers
```

Every moved script keeps a thin compatibility launcher at its original path for
one migration cycle. Documentation commands migrate to the new paths in the
same change.

## Documentation layout

`README.md` becomes the concise entrypoint. Current material moves to:

```
docs/
  architecture.md
  operations.md
  verification.md
  status.md
  plans/               # historical and active implementation plans
  reference/           # compatibility and protocol notes
  superpowers/specs/   # approved design records
```

There is one current status file, `docs/status.md`; dated reports and session
logs are retained as historical evidence rather than competing sources of
truth. `HYBRID_PLAN.md` moves into `docs/plans/` and is labelled as a
non-implemented design, not an active architecture.

## Migration method

Moves occur in bounded slices: establish destination package and compatibility
shim, migrate imports/tests, run the offline gate, then proceed. No broad
search-and-replace or behavior rewrite is permitted. `git diff --check`, the
full offline suite, Ruff, MyPy, compileall, and a direct manual behavior check
from a clean browser/service start are required before completion.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Import breakage | Keep explicit shims; test public imports and CLI help. |
| Behavior change during extraction | Move first, split only under focused tests. |
| Documentation drift | Replace links in the same slice; maintain one status source. |
| Existing dirty worktree | Work only from the recorded backup; review each move with Git. |
| Manual verification unavailable | Record `BLOCKED_MANUAL_VERIFY`, never claim `MANUAL_PASS`. |

## Success criteria

1. The layout above is present and each directory has a single clear purpose.
2. Existing documented imports, CLI entrypoints, and HTTP routes remain usable.
3. `pytest -q`, `ruff check .`, `mypy gpt --ignore-missing-imports`, and
   `python -m compileall -q gpt` pass.
4. A human performs a direct clean-start behavior check after the automated
   gates; the result is recorded as `MANUAL_PASS` or
   `BLOCKED_MANUAL_VERIFY` with a concrete reason.
