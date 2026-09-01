from unittest.mock import AsyncMock

from openai import OpenAI
from starlette.testclient import TestClient

from gpt.api.server import WebChatAPIServer as APIServer
from gpt.api.server import create_api_app
from gpt.conversations import ConversationRecord
from gpt.gateway.server import WebChatAPIServer as GatewayServer
from gpt.types import TurnResult

STEP_TOOL = {
    "type": "function",
    "function": {
        "name": "next_step",
        "description": "Return the result for one deterministic step",
        "parameters": {
            "type": "object",
            "properties": {"step": {"type": "integer"}},
            "required": ["step"],
        },
    },
}


def test_tool_result_can_match_an_assistant_call_in_request_history():
    call_id = "call_from_claude_history"
    messages = [
        {"role": "user", "content": "Use the tool."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": "next_step", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": "done"},
    ]

    for server in (APIServer, GatewayServer):
        server._validate_tool_result_correlation(
            ConversationRecord(), [messages[-1]], messages
        )


class FakeWebSession:
    """Deterministic browser substitute; gateway mapping remains real."""

    def __init__(self, steps: int = 10):
        self.steps = steps
        self.send_count = 0
        self.conversation_id = None
        self.new_conversation = AsyncMock(side_effect=self._new)
        self.open = AsyncMock(side_effect=self._open)
        self.select_model = AsyncMock()
        self.select_reasoning_effort = AsyncMock()

    async def _new(self):
        self.conversation_id = None

    async def _open(self, conversation_id):
        self.conversation_id = conversation_id

    async def send(self, prompt, *args, **kwargs):
        self.send_count += 1
        self.conversation_id = "web-conversation-1"
        if self.send_count <= self.steps:
            text = (
                "<WEBGPT_TOOL_CALL>\n"
                f'{{"name":"next_step","arguments":{{"step":{self.send_count}}}}}\n'
                "</WEBGPT_TOOL_CALL>"
            )
        else:
            text = f"Completed {self.steps} correlated tool steps."
        return TurnResult(
            turn_id=f"turn-{self.send_count}",
            conversation_id=self.conversation_id,
            text=text,
        )

    def drain_events(self):
        return []


def test_standard_openai_client_completes_ten_step_tool_loop(monkeypatch):
    app = create_api_app()
    server = app.state.server
    fake = FakeWebSession(steps=10)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    messages = [{"role": "user", "content": "Complete ten steps."}]

    with TestClient(app) as http_client:
        client = OpenAI(
            base_url="http://testserver/v1",
            api_key="local",
            http_client=http_client,
        )
        first = client.chat.completions.create(
            model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
        )
        assert first.choices[0].finish_reason == "tool_calls"
        response = first

        for expected_step in range(1, 11):
            call = response.choices[0].message.tool_calls[0]
            assert call.function.name == "next_step"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [call.model_dump()],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"step {expected_step} verified",
                }
            )
            response = client.chat.completions.create(
                model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
            )

        assert response.choices[0].finish_reason == "stop"
        assert response.choices[0].message.content == "Completed 10 correlated tool steps."
        assert fake.send_count == 11
        assert len(server.conversations) == 1


def test_wrong_tool_call_id_is_rejected(monkeypatch):
    app = create_api_app()
    server = app.state.server
    fake = FakeWebSession(steps=1)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [STEP_TOOL],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assistant = first.json()["choices"][0]["message"]
        payload["messages"].extend(
            [
                assistant,
                {"role": "tool", "tool_call_id": "call_wrong", "content": "fake"},
            ]
        )
        response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conversation_conflict"
    assert response.json()["error"]["retryable"] is False


# ---------------------------------------------------------------------------
# P1-3-BOUNDED-MULTI-TOOL: up to WEBGPT_MAX_TOOL_CALLS_PER_TURN invokes per
# web turn pass through to the CLI as parallel tool_use blocks; only overflow
# triggers the MULTI_TOOL correction. limit=1 restores strict behavior.
# (Appended-only section: no pre-existing test above was modified.)
# ---------------------------------------------------------------------------


def _p13_step_block(*steps: int) -> str:
    """A <tool_calls> block with one invoke per requested step."""
    import json as _json

    invokes = "".join(
        '<invoke name="next_step">'
        f'<parameter name="step"><![CDATA[{_json.dumps(s)}]]></parameter>'
        "</invoke>"
        for s in steps
    )
    return f"<tool_calls>{invokes}</tool_calls>"


class _P13ScriptedWebSession:
    """Returns scripted reply texts in order; gateway mapping remains real."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.sent_prompts = []
        self.conversation_id = None
        self.new_conversation = AsyncMock(side_effect=self._new)
        self.open = AsyncMock(side_effect=self._open)
        self.select_model = AsyncMock()
        self.select_reasoning_effort = AsyncMock()

    async def _new(self):
        self.conversation_id = None

    async def _open(self, conversation_id):
        self.conversation_id = conversation_id

    async def send(self, prompt, *args, **kwargs):
        self.sent_prompts.append(prompt)
        index = len(self.sent_prompts) - 1
        text = self.texts[index] if index < len(self.texts) else self.texts[-1]
        self.conversation_id = "web-conversation-p13"
        return TurnResult(
            turn_id=f"turn-p13-{len(self.sent_prompts)}",
            conversation_id=self.conversation_id,
            text=text,
        )

    def drain_events(self):
        return []


def _p13_post_completion(http_client, fake):
    import json as _json

    from openai import OpenAI as _OpenAI

    client = _OpenAI(
        base_url="http://testserver/v1",
        api_key="local",
        http_client=http_client,
    )
    response = client.chat.completions.create(
        model="chatgpt-web",
        messages=[{"role": "user", "content": "run the scripted steps"}],
        tools=[STEP_TOOL],
    )
    assert fake.sent_prompts, "expected at least one web send"
    return response, _json


def _p13_correction_reasons(server):
    return [
        event
        for event in server.trace.snapshot()
        if event.component == "completionruntime"
        and event.kind == "tool_correction"
        and event.metadata.get("reason") == "MULTI_TOOL"
    ]


def test_p13_two_invokes_pass_through_at_default_limit(monkeypatch):
    """Default N=3: a 2-invoke web turn becomes 2 tool_use blocks, no correction."""
    monkeypatch.delenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", raising=False)
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession([_p13_step_block(1, 2)])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    with TestClient(app) as http_client:
        response, json = _p13_post_completion(http_client, fake)

    assert response.choices[0].finish_reason == "tool_calls"
    calls = response.choices[0].message.tool_calls
    assert len(calls) == 2
    assert [json.loads(call.function.arguments)["step"] for call in calls] == [1, 2]
    # Accepted on the first web turn -- no correction round burned.
    assert len(fake.sent_prompts) == 1
    accepted = [
        event
        for event in server.trace.snapshot()
        if event.kind == "multi_tool_turn_accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0].metadata["tool_calls"] == 2
    assert accepted[0].metadata["limit"] == 3


def test_p13_five_invokes_over_limit_three_get_corrected(monkeypatch):
    """5 invokes vs limit 3 -> MULTI_TOOL correction whose message states N."""
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession([_p13_step_block(1, 2, 3, 4, 5), _p13_step_block(9)])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    with TestClient(app) as http_client:
        response, json = _p13_post_completion(http_client, fake)

    assert response.choices[0].finish_reason == "tool_calls"
    calls = response.choices[0].message.tool_calls
    assert len(calls) == 1
    assert json.loads(calls[0].function.arguments)["step"] == 9
    assert len(fake.sent_prompts) == 2
    corrections = _p13_correction_reasons(server)
    assert len(corrections) == 1
    # The correction prompt must state the configured bound explicitly.
    assert "model returned 5 tool calls" in fake.sent_prompts[1]
    assert "at most 3 are allowed per turn" in fake.sent_prompts[1]


def test_p13_env_one_restores_strict_single_call(monkeypatch):
    """limit=1 reproduces the historical strict one-invoke correction verbatim."""
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "1")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession([_p13_step_block(1, 2), _p13_step_block(7)])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    with TestClient(app) as http_client:
        response, json = _p13_post_completion(http_client, fake)

    assert response.choices[0].finish_reason == "tool_calls"
    calls = response.choices[0].message.tool_calls
    assert len(calls) == 1
    assert json.loads(calls[0].function.arguments)["step"] == 7
    assert len(fake.sent_prompts) == 2
    corrections = _p13_correction_reasons(server)
    assert len(corrections) == 1
    assert "exactly one is allowed" in fake.sent_prompts[1]


def test_duplicate_tool_results_in_one_request_are_conversation_conflict(monkeypatch):
    app = create_api_app()
    server = app.state.server
    fake = FakeWebSession(steps=1)
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    payload = {
        "model": "chatgpt-web",
        "messages": [{"role": "user", "content": "use a tool"}],
        "tools": [STEP_TOOL],
    }
    with TestClient(app) as client:
        first = client.post("/v1/chat/completions", json=payload)
        assistant = first.json()["choices"][0]["message"]
        call_id = assistant["tool_calls"][0]["id"]
        payload["messages"].extend(
            [
                assistant,
                {"role": "tool", "tool_call_id": call_id, "content": "first"},
                {"role": "tool", "tool_call_id": call_id, "content": "duplicate"},
            ]
        )
        response = client.post("/v1/chat/completions", json=payload)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conversation_conflict"


# ---------------------------------------------------------------------------
# P13-UX: correction wording tracks the live per-turn batching cap
# ---------------------------------------------------------------------------


def test_p13ux_overflow_correction_teaches_batch_budget(monkeypatch):
    """N>1: MULTI_TOOL overflow teaches the batch budget in detail + guidance."""
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "3")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession([_p13_step_block(1, 2, 3, 4, 5), _p13_step_block(9)])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    with TestClient(app) as http_client:
        response, _json = _p13_post_completion(http_client, fake)

    assert response.choices[0].finish_reason == "tool_calls"
    prompt = fake.sent_prompts[1]
    # Overflow detail states the bound AND how to use it.
    assert "at most 3 are allowed per turn" in prompt
    assert (
        "you may batch up to 3 tool calls per turn using multiple invokes" in prompt
    )
    # Generic guidance is consistent with the cap, not contradicting it.
    assert "You may batch up to 3 tool calls per turn using multiple invokes" in prompt
    assert "Normally include exactly one invoke" not in prompt


def test_p13ux_env_one_keeps_strict_guidance_wording(monkeypatch):
    """limit=1: correction keeps the historical single-invoke wording verbatim."""
    monkeypatch.setenv("WEBGPT_MAX_TOOL_CALLS_PER_TURN", "1")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession([_p13_step_block(1, 2), _p13_step_block(7)])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    with TestClient(app) as http_client:
        response, _json = _p13_post_completion(http_client, fake)

    assert response.choices[0].finish_reason == "tool_calls"
    prompt = fake.sent_prompts[1]
    assert "exactly one is allowed" in prompt
    assert "Normally include exactly one invoke" in prompt
    assert "batch up to" not in prompt


# ---------------------------------------------------------------------------
# DEBUG-R9 regressions (docs/reports/debug-r9-2026-08-25.md).
# Appended-only section: no pre-existing test above was modified.
# ---------------------------------------------------------------------------

import json as _r9_json  # noqa: E402

import pytest as _r9_pytest  # noqa: E402

from gpt.gateway.runtime import (  # noqa: E402
    _fresh_tool_conversation,
    _looks_like_tool_directed_task,
    _tool_correction_issue,
)
from gpt.state import MalformedToolCall as _R9MalformedToolCall  # noqa: E402
from gpt.utils.toolcall import ToolTranspiler as _R9ToolTranspiler  # noqa: E402

# Same surface shape as the live session: internal OpenAI tools the harness
# exposes ([write_file, Bash]) while the soft handshake only teaches <cmd>.
_R9_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write text content to a file at an absolute path.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "Bash",
            "description": "Execute a shell command inside the task workdir.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
]

_R9_TASK = (
    "Use the write_file tool to create a file at exactly "
    "/tmp/verify_hybrid_flip_r9/t2_hello.txt whose entire content is exactly "
    "this string: HYBRID_FLIP_R9_OK\n"
    "Do not add anything else. Finish after the tool call."
)


def test_debug_r9_placeholder_cmd_body_never_becomes_a_tool_call():
    """BUG B: a quoted/ellipsised <cmd> body is protocol quotation, not a call."""
    allowed = set(_R9ToolTranspiler.validate_tools(_R9_TOOLS))
    placeholder_bodies = ("...", '"..."', "'…'", "<command>", "the exact shell command")
    for body in placeholder_bodies:
        text = f"Đã rõ. Khi cần chạy lệnh tôi sẽ trả về đúng một dòng dạng <cmd>{body}</cmd>."
        prose, calls = _R9ToolTranspiler.parse_tool_calls(
            text, allowed_tools=allowed, tool_definitions=_R9_TOOLS, protocol="soft"
        )
        assert calls == [], body  # never a phantom Bash("...") execution
        # Codex12 finding #5 fix (2026-08-26): the quoted protocol fragment is
        # excised from the visible prose (the span was always recorded for
        # exactly this), while the surrounding acknowledgment survives.
        assert prose is not None and "<cmd>" not in prose, body
        assert body not in prose, body
        assert "Đã rõ." in prose, body

    # Placeholder tags mixed with a genuine command: only the real one ships.
    _, calls = _R9ToolTranspiler.parse_tool_calls(
        '<cmd>"..."</cmd>\n<cmd>pwd</cmd>',
        allowed_tools=allowed,
        tool_definitions=_R9_TOOLS,
        protocol="soft",
    )
    assert [call["function"]["name"] for call in calls] == ["Bash"]
    assert _r9_json.loads(calls[0]["function"]["arguments"]) == {"command": "pwd"}

    # A genuinely empty body stays fail-closed exactly as before.
    with _r9_pytest.raises(_R9MalformedToolCall):
        _R9ToolTranspiler.parse_tool_calls(
            "<cmd></cmd>", allowed_tools=allowed, tool_definitions=_R9_TOOLS,
            protocol="soft",
        )

    # A real command still parses through to a canonical Bash call.
    _, calls = _R9ToolTranspiler.parse_tool_calls(
        "<cmd>printf '%s\\n' HYBRID_FLIP_R9_OK > /tmp/verify_hybrid_flip_r9/t2_hello.txt</cmd>",
        allowed_tools=allowed,
        tool_definitions=_R9_TOOLS,
        protocol="soft",
    )
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "Bash"


def _r9_bootstrapped_messages():
    """Round-2 request shape: prior Bash call plus its exit=0 tool result."""
    command = (
        "printf '%s\\n' HYBRID_FLIP_R9_OK > /tmp/verify_hybrid_flip_r9/t2_hello.txt"
    )
    return [
        {"role": "system", "content": "You complete tasks by calling the provided tools."},
        {"role": "user", "content": _R9_TASK},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_r9",
                    "type": "function",
                    "function": {"name": "Bash", "arguments": _r9_json.dumps({"command": command})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_r9", "content": "exit=0\nstdout:\n\nstderr:\n"},
    ]


_R9_COMPLETION_PROSE = (
    "Verified: the command completed successfully (exit=0), so "
    "/tmp/verify_hybrid_flip_r9/t2_hello.txt was created with the requested content."
)


def test_debug_r9_false_completion_guarded_after_real_tool_result():
    """BUG A: truthful prose after a real tool result must stay uncorrected."""
    messages = _r9_bootstrapped_messages()
    tail = messages[-1:]
    assert _fresh_tool_conversation(messages, tail) is False
    # The heuristic itself stops flagging the task once real work happened --
    # the tool-result exemption outranks the explicit write_file demand.
    assert _looks_like_tool_directed_task(tail, messages, _R9_TOOLS) is False
    issue = _tool_correction_issue(
        _R9_COMPLETION_PROSE,
        tail=tail,
        messages=messages,
        tools=_R9_TOOLS,
        tool_choice="auto",
    )
    assert issue is None


def test_debug_r9_false_completion_still_fires_on_fresh_conversation():
    """The new guard must not over-block: same prose before any tool call is
    still a fabricated completion on a fresh conversation."""
    messages = [{"role": "user", "content": _R9_TASK}]
    issue = _tool_correction_issue(
        _R9_COMPLETION_PROSE,
        tail=list(messages),
        messages=messages,
        tools=_R9_TOOLS,
        tool_choice="auto",
    )
    assert issue is not None
    assert issue[0] == "FALSE_COMPLETION"


def test_debug_r9_bootstrapped_correction_keeps_original_task_verbatim(monkeypatch):
    """Aggravator fix: corrections on a web_bootstrapped record still embed the
    original user task, extracted from the FULL transcript instead of the
    tool-result tail."""
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "xml")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession(
        [
            _p13_step_block(1),  # first turn commits -> record.web_bootstrapped
            # second turn: malformed block -> MALFORMED_TOOL correction
            '<WEBGPT_TOOL_CALL>{"broken"</WEBGPT_TOOL_CALL>',
            _p13_step_block(2),  # corrected turn succeeds
        ]
    )
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    task_text = (
        "ORIGINTASK-R9 create the launch codes file then run the verification suite"
    )
    messages = [{"role": "user", "content": task_text}]

    with TestClient(app) as http_client:
        client = OpenAI(
            base_url="http://testserver/v1",
            api_key="local",
            http_client=http_client,
        )
        first = client.chat.completions.create(
            model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
        )
        assert first.choices[0].finish_reason == "tool_calls"
        call = first.choices[0].message.tool_calls[0]
        messages.append(
            {"role": "assistant", "content": None, "tool_calls": [call.model_dump()]}
        )
        messages.append(
            {"role": "tool", "tool_call_id": call.id, "content": "step 1 verified"}
        )
        second = client.chat.completions.create(
            model="chatgpt-web", messages=messages, tools=[STEP_TOOL]
        )

    assert second.choices[0].finish_reason == "tool_calls"
    # Send #2 is the normal bootstrapped turn (prompt_messages == tool-result
    # tail); send #3 is the correction prompt built from that state.
    assert len(fake.sent_prompts) == 3
    assert "ORIGINAL USER TASK (for context):" in fake.sent_prompts[2]
    assert task_text in fake.sent_prompts[2]


# --- DEBUG-R8 RC1: soft-mode envelope echo at large tool surfaces ---------
# docs/reports/debug-r8-2026-08-25.md RC1: soft render of a 57-tool client
# surface produced a ~68k single-line escaped JSON envelope with no bootstrap;
# the web model echoed it verbatim (R7d passed at 24k / 24 tools). Fix: a
# plain-text framing line appended after the one-time handshake.


_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Executes a shell command inside the client workspace",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}


def _r8c_surface_tools(count: int = 57) -> list[dict]:
    """A claude-code-sized mock surface: real Bash plus filler client tools."""
    fillers = [
        {
            "type": "function",
            "function": {
                "name": f"SurfaceTool{i:02d}",
                "description": (
                    f"Mock client surface tool #{i}: inspects a workspace path "
                    "and reports a verbose summary of what it finds back."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        }
        for i in range(count - 1)
    ]
    return [_BASH_TOOL, *fillers]


def test_debug_r8c_soft_big_surface_prompt_ends_with_framing_once(monkeypatch):
    """(a) 57-tool soft render: framing text appears exactly once, at the very
    END of the prompt, after every <WEBGPT_MESSAGE> blob and the handshake."""
    from gpt.gateway.runtime import _SOFT_FRAMING_TEXT, _SOFT_HANDSHAKE_TEXT

    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession(["<cmd>echo surface-ok</cmd>"])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))
    tools = _r8c_surface_tools(57)

    with TestClient(app) as http_client:
        response = OpenAI(
            base_url="http://testserver/v1",
            api_key="local",
            http_client=http_client,
        ).chat.completions.create(
            model="chatgpt-web",
            messages=[{"role": "user", "content": "run the scripted steps"}],
            tools=tools,
        )

    assert response.choices[0].finish_reason == "tool_calls"
    assert len(fake.sent_prompts) == 1
    prompt = fake.sent_prompts[0]
    # Exactly once, and as the trailing plain text.
    assert prompt.count(_SOFT_FRAMING_TEXT) == 1
    assert prompt.rstrip().endswith(_SOFT_FRAMING_TEXT)
    # Ordered after the handshake and after every rendered message blob.
    assert prompt.count(_SOFT_HANDSHAKE_TEXT) == 1
    assert prompt.index(_SOFT_FRAMING_TEXT) > prompt.index(_SOFT_HANDSHAKE_TEXT)
    assert prompt.rindex("</WEBGPT_MESSAGE>") < prompt.index(_SOFT_FRAMING_TEXT)
    # The framing itself is plain text -- its tag literal must NOT be JSON-
    # escaped (that is exactly the failure shape RC1 diagnosed).
    assert "\\u003c" not in _SOFT_FRAMING_TEXT


def test_debug_r8c_soft_render_respects_max_prompt_chars(monkeypatch):
    """(2) The soft path honors WEBGPT_MAX_PROMPT_CHARS: an oversized transcript
    goes through deterministic compaction before the web send."""
    from gpt.gateway.runtime import _SOFT_FRAMING_TEXT

    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.setenv("WEBGPT_MAX_PROMPT_CHARS", "20000")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    app = create_api_app()
    server = app.state.server
    fake = _P13ScriptedWebSession(["<cmd>true</cmd>"])
    monkeypatch.setattr(server, "get_or_create_session", AsyncMock(return_value=fake))

    filler = "filler history line with plenty of padding characters. "
    messages = [
        {"role": "user", "content": "R8CBUDGET objective marker FIRSTUSER"},
    ]
    for i in range(12):
        messages.append({"role": "user", "content": f"{filler * 80}mid-{i}"})
    messages.append(
        {"role": "user", "content": "R8CBUDGET latest turn marker LASTUSER"}
    )
    total_chars = sum(len(m["content"]) for m in messages)
    assert total_chars > 20000

    with TestClient(app) as http_client:
        response = OpenAI(
            base_url="http://testserver/v1",
            api_key="local",
            http_client=http_client,
        ).chat.completions.create(model="chatgpt-web", messages=messages,
                                  tools=_r8c_surface_tools(3))

    assert response.choices[0].finish_reason == "tool_calls"
    assert len(fake.sent_prompts) == 1
    compacted = [
        event
        for event in server.trace.snapshot()
        if event.kind == "prompt_compacted"
    ]
    assert len(compacted) == 1
    prompt = fake.sent_prompts[0]
    # Compaction actually shrank the oversized transcript...
    assert len(prompt) < total_chars
    assert "mid-4" not in prompt
    # ...while the pinned first/latest user turns survive...
    assert "FIRSTUSER" in prompt
    assert "LASTUSER" in prompt
    # ...and the RC1 framing still closes the final prompt.
    assert prompt.rstrip().endswith(_SOFT_FRAMING_TEXT)


# ---------------------------------------------------------------------------
# CORRECTION-CIRCUIT-BREAKER (debug-r8 RC3): per-request correction budgets
# cannot see a livelock that spans requests (R1: 30 FALSE_COMPLETION <=>
# <cmd>true</cmd> metronome cycles, 34 corrections, zero budget trips).
# Appended-only section: no pre-existing test above was modified.
#
# These tests drive CompletionRuntime.execute_raw_on_session directly -- each
# call is one CLI request, and the module-level breaker state persists across
# them exactly like it does across requests in the live process.
# ---------------------------------------------------------------------------

import asyncio as _cb_asyncio  # noqa: E402

import pytest as _cb_pytest  # noqa: E402

from gpt.gateway.runtime import (  # noqa: E402
    CompletionRuntime as _CBCompletionRuntime,
)
from gpt.gateway.runtime import (  # noqa: E402
    _correction_breaker_states,
)
from gpt.state import MalformedToolCall as _CBMalformedToolCall  # noqa: E402
from gpt.tracing import RuntimeTraceBus as _CBTraceBus  # noqa: E402

_CB_FC_PROSE = (
    "Done — I've created the report file and everything checks out."
)
_CB_TRUE_CMD = "<cmd>true</cmd>"
_CB_CONV_A = "web-conversation-cb-a"
_CB_CONV_B = "web-conversation-cb-b"
_CB_CONV_C = "web-conversation-cb-c"


class _CBCorrectionFakeStore:
    """execute_raw_on_session only needs mark_pending / clear_pending."""

    def mark_pending(self, *args, **kwargs):
        return None

    def clear_pending(self, *args, **kwargs):
        return None


class _CBScriptedWebSession:
    """One web session per request; returns scripted replies in order."""

    def __init__(self, texts):
        self._texts = list(texts)
        self.send_count = 0
        self.conversation_id = None
        self.new_conversation = AsyncMock(side_effect=self._new)
        self.open = AsyncMock(side_effect=self._open)
        self.select_model = AsyncMock()
        self.select_reasoning_effort = AsyncMock()

    async def _new(self):
        self.conversation_id = None

    async def _open(self, conversation_id):
        self.conversation_id = conversation_id

    async def send(self, prompt, *args, **kwargs):
        index = self.send_count
        self.send_count += 1
        text = self._texts[index] if index < len(self._texts) else self._texts[-1]
        return TurnResult(
            turn_id=f"turn-cb-{self.send_count}",
            conversation_id=self.conversation_id or _CB_CONV_A,
            text=text,
        )

    def drain_events(self):
        return []


def _cb_runtime(monkeypatch):
    monkeypatch.setenv("WEBGPT_TOOL_PROTOCOL", "soft")
    monkeypatch.delenv("WEBGPT_PROMPT_DEBUG_DIR", raising=False)
    monkeypatch.delenv("WEBGPT_FALSE_COMPLETION_BREAKER", raising=False)
    monkeypatch.delenv("WEBGPT_NOOP_REPEAT_SKIP", raising=False)
    _correction_breaker_states.clear()
    return _CBCompletionRuntime(
        conversations=_CBCorrectionFakeStore(),
        lease_session=lambda: None,
        trace=_CBTraceBus(),
        generation_timeout_seconds=5.0,
    )


def _cb_run_request(runtime, record, texts):
    """One CLI request against a freshly leased scripted web session."""
    session = _CBScriptedWebSession(texts)
    result, _prompt = _cb_asyncio.run(
        runtime.execute_raw_on_session(
            session,
            record,
            [{"role": "user", "content": "Use Bash to create the report file now."}],
            [{"role": "user", "content": "Use Bash to create the report file now."}],
            model="chatgpt-web",
            ui_model=None,
            tools=[_BASH_TOOL, STEP_TOOL],
            tool_choice=None,
        )
    )
    return result, session


def test_correction_breaker_raises_after_twelve_false_completions_across_requests(
    monkeypatch,
):
    """(a) 12+ FALSE_COMPLETION repeats on ONE conversation across requests ->
    terminal raise instead of another correction round."""
    runtime = _cb_runtime(monkeypatch)
    record = ConversationRecord(session_id="wgs_cb_a", conversation_id=_CB_CONV_A)

    # Requests 1..11 each observe exactly one FALSE_COMPLETION; all succeed.
    for _request_index in range(11):
        result, _ = _cb_run_request(runtime, record, [_CB_FC_PROSE, _CB_TRUE_CMD])
        assert result.text  # committed turn (or armed skip) always has text

    # Request 12 pushes the cumulative counter to the threshold -> trip.
    with _cb_pytest.raises(_CBMalformedToolCall) as excinfo:
        _cb_run_request(runtime, record, [_CB_FC_PROSE, _CB_TRUE_CMD])
    assert "repeated false-completion livelock detected" in str(excinfo.value)

    tripped = [
        event
        for event in runtime.trace.snapshot()
        if event.kind == "correction_breaker_tripped"
    ]
    assert len(tripped) == 1
    assert tripped[0].metadata["reason"] == "FALSE_COMPLETION"
    assert tripped[0].metadata["repeats"] == 12
    assert tripped[0].metadata["threshold"] == 12


def test_noop_repeat_detector_stops_corrections_after_five_true_commits(monkeypatch):
    """(b) `true` x5 identical commits arm the no-op detector: the next
    FALSE_COMPLETION round returns text-only with a warning instead of
    burning another correction send."""
    runtime = _cb_runtime(monkeypatch)
    record = ConversationRecord(session_id="wgs_cb_b", conversation_id=_CB_CONV_A)

    for _ in range(5):
        result, session = _cb_run_request(runtime, record, [_CB_FC_PROSE, _CB_TRUE_CMD])
        assert session.send_count == 2  # prose + correction -> true commit

    state = _correction_breaker_states[_CB_CONV_A]
    assert state["noop_streak"] == 5

    # Sixth metronome cycle: correction skipped, single send, text-only reply.
    result, session = _cb_run_request(runtime, record, [_CB_FC_PROSE])
    assert session.send_count == 1
    assert "no-op tool call" in result.text
    assert "text-only" in result.text
    skipped = [
        event
        for event in runtime.trace.snapshot()
        if event.kind == "correction_skipped_noop_repeat"
    ]
    assert len(skipped) == 1
    assert skipped[0].metadata["noop_streak"] == 5


def test_breaker_counters_reset_when_reason_changes(monkeypatch):
    """(c) A different issue reason resets the cumulative counter; real work
    (a non-no-op commit) additionally clears the no-op streak."""
    runtime = _cb_runtime(monkeypatch)
    record = ConversationRecord(session_id="wgs_cb_c", conversation_id=_CB_CONV_C)

    for _ in range(4):
        _cb_run_request(runtime, record, [_CB_FC_PROSE, _CB_TRUE_CMD])
    state = _correction_breaker_states[_CB_CONV_C]
    assert state["reason"] == "FALSE_COMPLETION"
    assert state["reason_count"] == 4
    assert state["noop_streak"] == 4

    # One request whose first reply is an over-limit MULTI_TOOL batch corrected
    # into a real next_step invoke: reason change + real-work commit both reset.
    _cb_run_request(runtime, record, [_p13_step_block(1, 2, 3, 4, 5), _p13_step_block(9)])
    state = _correction_breaker_states[_CB_CONV_C]
    assert state["reason"] is None
    assert state["reason_count"] == 0
    assert state["noop_sig"] is None
    assert state["noop_streak"] == 0

    # FALSE_COMPLETION counting restarts from scratch afterwards.
    for _ in range(2):
        _cb_run_request(runtime, record, [_CB_FC_PROSE, _CB_TRUE_CMD])
    state = _correction_breaker_states[_CB_CONV_C]
    assert state["reason"] == "FALSE_COMPLETION"
    assert state["reason_count"] == 2  # restarted, NOT 4+2=6


def test_breaker_state_is_per_conversation_and_isolated(monkeypatch):
    """(d) Conversations count independently: B runs its own full window and
    trips on its own 12th repeat while A's counters are untouched."""
    runtime = _cb_runtime(monkeypatch)
    record_a = ConversationRecord(session_id="wgs_cb_d1", conversation_id=_CB_CONV_A)
    record_b = ConversationRecord(session_id="wgs_cb_d2", conversation_id=_CB_CONV_B)

    for _ in range(11):
        _cb_run_request(runtime, record_a, [_CB_FC_PROSE, _CB_TRUE_CMD])
    assert _correction_breaker_states[_CB_CONV_A]["reason_count"] == 11

    # B starts from zero: its 11th repeat must NOT trip early off A's count...
    for _ in range(11):
        _cb_run_request(runtime, record_b, [_CB_FC_PROSE, _CB_TRUE_CMD])
    assert _correction_breaker_states[_CB_CONV_B]["reason_count"] == 11

    # ...and B trips on its OWN 12th repeat.
    with _cb_pytest.raises(_CBMalformedToolCall) as excinfo:
        _cb_run_request(runtime, record_b, [_CB_FC_PROSE, _CB_TRUE_CMD])
    assert "repeated false-completion livelock detected" in str(excinfo.value)

    # A is unaffected by B's trip and keeps completing turns; a real command
    # even clears its accumulated counter.
    _result, session = _cb_run_request(runtime, record_a, ["<cmd>echo status-ok</cmd>"])
    assert session.send_count == 1
    state_a = _correction_breaker_states[_CB_CONV_A]
    assert state_a["reason_count"] == 0
    assert state_a["noop_streak"] == 0
