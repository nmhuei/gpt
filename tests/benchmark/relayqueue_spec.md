# RelayQueue benchmark specification

Implement a Python 3.10+ standard-library-only package named `relayqueue`. The package implements a durable local SQLite task queue. Runtime dependencies outside Python standard library are forbidden.

## Commands

~~~
python -m relayqueue --db <path> [--max-attempts <positive-int>] init
python -m relayqueue --db <path> [--max-attempts <positive-int>] enqueue --payload <json-object> [--key <idempotency-key>]
python -m relayqueue --db <path> [--max-attempts <positive-int>] claim --worker <name> [--lease-seconds <positive-int>]
python -m relayqueue --db <path> [--max-attempts <positive-int>] ack --task <id> --lease-token <token>
python -m relayqueue --db <path> [--max-attempts <positive-int>] fail --task <id> --lease-token <token> --retry-after <non-negative-int>
python -m relayqueue --db <path> [--max-attempts <positive-int>] list [--state ready|leased|succeeded|dead]
python -m relayqueue --db <path> [--max-attempts <positive-int>] stats
~~~

The default max attempts must be documented in README. Successful commands write exactly one JSON object to stdout and nothing else. Invalid input/errors write a JSON object to stderr and return non-zero.

## Required JSON shapes

- init: `{"ok": true, "schema_version": 1}`
- enqueue: `{"ok": true, "deduplicated": <bool>, "task": {"id": <str>, "state": "ready", "attempts": 0, "payload": <object>}}`
- claim empty: `{"ok": true, "task": null}`
- claim success task includes `id`, `state: "leased"`, `lease_token`, `worker`, and `attempts`.
- ack success: `{"ok": true, "task": {"id": <str>, "state": "succeeded"}}`
- fail success task includes `id`, `state` (`ready` or `dead`), and `attempts`.
- list: `{"ok": true, "tasks": [ ... ]}`
- stats: `{"ok": true, "counts": {"ready": <int>, "leased": <int>, "succeeded": <int>, "dead": <int>}}`

Additional fields are allowed. Required fields and their meanings cannot change.

## Required behavior

1. `init` is idempotent and records schema version 1.
2. `enqueue` accepts only JSON object payloads. Same idempotency key with same payload returns the original task and `deduplicated: true`; same key with different payload fails without mutation.
3. `claim` atomically leases the oldest eligible ready task. It returns an unguessable lease token and respects lease expiry. No eligible task is a successful empty result.
4. `ack` requires current lease token. Wrong, duplicate, and expired acknowledgements fail without state mutation.
5. `fail` requires current lease token, increments attempts, delays retry by `retry-after`, and moves task to `dead` at max attempts.
6. State survives process restart. Expired lease becomes eligible without losing attempt accounting.
7. Competing workers cannot both successfully claim the same task.
8. Use parameterized SQLite queries. Do not call `eval`, a shell, or subprocesses.
9. Include README, unit tests, type annotations where practical, and no external runtime dependencies.

