# Agent handoff: discover workspace before clarifying

Date: 2026-08-24

## Isolated implementation

- Branch: `fix/discover-before-clarify`
- Worktree: `/home/light/Downloads/gpt-discover-first`
- Base: `efcd95bc381b3e688a16dff5585e5475f2eccf30`
- Commit: `7c04ce9` (`fix(webgpt): discover workspace before clarifying CTF tasks`)

Do not overwrite or reset the dirty `main` working tree. The current `main` agent has substantial uncommitted work in `gpt/gateway/runtime.py` and `gpt/utils/toolcall.py`.

## Root cause

`_looks_like_tool_directed_task()` only recognized a narrow action vocabulary such as implement/create/write/run/read. A request like `solve bài pwn đi` with Bash/Read/Glob available returned `False`, so prose such as `Bài pwn nào? Bạn gửi binary/path...` was accepted as a final completion instead of being corrected to a discovery tool call.

The model-facing tool contract also said to call tools when needed but did not explicitly require discovering the current workspace before asking the user for filenames/paths.

## Patch semantics to integrate

1. Add `_looks_like_workspace_discovery_task(text, tool_names)` for imperative local CTF/Pwn requests (`solve/làm/giải/continue` + `pwn/ctf/challenge`) when discovery-capable tools are available.
2. In `_looks_like_tool_directed_task()`, return `True` for that intent before accepting prose completion.
3. Add a `WORKSPACE DISCOVERY POLICY` to the model-facing tool instructions:
   - inspect the current workspace before asking the user for filenames/paths;
   - do not guess paths or invent tool results;
   - use Glob/Grep/Read/shell discovery first;
   - ask only after tool-based discovery fails or leaves genuine ambiguity.
4. Bring over `tests/test_workspace_discovery_policy.py`.

## Important integration note for current main

Current dirty `main` is independently adding soft-refusal detection and `WEBGPT_TOOL_PROTOCOL=json-fn/both`. The isolated commit was based on clean `efcd95b`, which only had the XML instruction builder.

When integrating into current `main`, mirror the workspace-discovery policy in BOTH model-facing instruction paths:

- XML/both path in `ToolTranspiler.build_tool_instructions()`
- JSON function-call path in `ToolTranspiler._json_fn_instructions()`

The runtime intent helper should complement, not replace, the current agent's soft-refusal/repo-intent/fanout logic.

## Verification evidence

Red phase before implementation:

- `tests/test_workspace_discovery_policy.py`: 3 failed as expected.

Green/final:

- `python -m pytest tests/ -q --tb=line` -> `329 passed`
- `/home/light/.local/bin/ruff check gpt/gateway/runtime.py gpt/utils/toolcall.py tests/test_workspace_discovery_policy.py` -> clean
- `git diff --check` -> clean
- Manual Anthropic `/v1/messages` simulation:
  - user: `solve bài pwn đi`
  - fake web model first replies asking user for binary/path
  - gateway rejects that prose as false completion
  - correction prompt includes `WORKSPACE DISCOVERY POLICY`
  - second model response emits `Bash(pwd)`
  - final API response: `stop_reason=tool_use`
  - observed marker: `MANUAL_PASS solve-pwn-discovery`

## Suggested integration

Because `runtime.py` and `toolcall.py` overlap with current uncommitted work, inspect `git show 7c04ce9` and integrate the small semantic pieces manually if `git cherry-pick 7c04ce9` conflicts. Do not discard current main changes.
