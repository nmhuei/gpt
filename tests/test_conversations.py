from gpt.api.conversations import ConversationStore

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup",
            "parameters": {"type": "object"},
        },
    }
]


def test_conversation_store_matches_standard_message_prefix_and_caches_retry():
    store = ConversationStore()
    first = [{"role": "user", "content": "find it"}]
    record, tail, cached = store.resolve(first, "chatgpt-web", TOOLS)
    assert tail == first
    assert not cached
    assistant = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }
        ],
    }
    response = {"choices": [{"message": assistant, "finish_reason": "tool_calls"}]}
    store.commit(record, first, assistant, response, "chatgpt-web", TOOLS, "conv_1")

    same, _, cached = store.resolve(
        first, "chatgpt-web", TOOLS, explicit_id=record.session_id
    )
    assert same is record
    assert cached

    independent, independent_tail, independent_cached = store.resolve(
        first, "chatgpt-web", TOOLS
    )
    assert independent is not record
    assert independent_tail == first
    assert not independent_cached

    continued = [
        *first,
        assistant,
        {"role": "tool", "tool_call_id": "call_1", "content": "42"},
    ]
    matched, tail, cached = store.resolve(continued, "chatgpt-web", TOOLS)
    assert matched is record
    assert tail == [continued[-1]]
    assert not cached


def test_explicit_session_rejects_divergent_history():
    store = ConversationStore()
    initial = [{"role": "user", "content": "A"}]
    record, _, _ = store.resolve(initial, "chatgpt-web", [])
    assistant = {"role": "assistant", "content": "B"}
    store.commit(record, initial, assistant, {"ok": True}, "chatgpt-web", [], "conv")
    divergent = [{"role": "user", "content": "different"}]
    try:
        store.resolve(divergent, "chatgpt-web", [], record.session_id)
    except ValueError as exc:
        assert "diverge" in str(exc)
    else:
        raise AssertionError("divergent history must be rejected")
