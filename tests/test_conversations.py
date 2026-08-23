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


def test_conversation_store_persists_only_when_explicitly_enabled(tmp_path):
    state_path = tmp_path / "gateway-state.json"
    first = [{"role": "user", "content": "persist me"}]
    store = ConversationStore(state_path=state_path)
    record, _, _ = store.resolve(first, "chatgpt-web", [])
    store.commit(
        record,
        first,
        {"role": "assistant", "content": "saved"},
        {"choices": [{"message": {"role": "assistant", "content": "saved"}}]},
        "chatgpt-web",
        [],
        "web-1",
    )
    assert state_path.exists()
    assert state_path.stat().st_mode & 0o777 == 0o600

    restored = ConversationStore(state_path=state_path)
    assert restored.get(record.session_id) is not None
    same, tail, cached = restored.resolve(first, "chatgpt-web", [], record.session_id)
    assert same.conversation_id == "web-1"
    assert tail == []
    assert cached


def test_conversation_store_discards_expired_persisted_records(tmp_path):
    state_path = tmp_path / "expired.json"
    state_path.write_text(
        '{"version":1,"records":[{"session_id":"wgs_old","saved_at":0}]}',
        encoding="utf-8",
    )
    store = ConversationStore(state_path=state_path, ttl_seconds=1)
    assert len(store) == 0


def test_pending_commit_unknown_state_survives_store_restart(tmp_path):
    state_path = tmp_path / "pending.json"
    messages = [{"role": "user", "content": "uncertain marker"}]
    store = ConversationStore(state_path=state_path)
    record, _, _ = store.resolve(messages, "chatgpt-web", [])
    record.conversation_id = "conv_pending"
    fingerprint = store.mark_pending(
        record,
        messages=messages,
        model="chatgpt-web",
        tools=[],
        tool_choice="auto",
        prompt="<WEBGPT_MESSAGE role=\"user\">uncertain marker</WEBGPT_MESSAGE>",
    )

    restored = ConversationStore(state_path=state_path)
    loaded = restored.get(record.session_id)
    assert loaded is not None
    assert loaded.conversation_id == "conv_pending"
    assert loaded.pending_request_fingerprint == fingerprint
    assert loaded.pending_prompt is not None
    assert restored.pending_matches(
        loaded,
        messages,
        "chatgpt-web",
        [],
        "auto",
    )
