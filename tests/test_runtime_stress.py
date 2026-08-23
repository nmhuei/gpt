from gpt.api.conversations import ConversationStore
from gpt.api.requests import parse_chat_completion_request
from gpt.streaming import MutableTextAccumulator


def test_normalized_runtime_stress_regression():
    store = ConversationStore(max_sessions=600)
    for index in range(500):
        request = parse_chat_completion_request(
            {
                "model": "chatgpt-web",
                "messages": [{"role": "user", "content": f"marker-{index}"}],
                "stream": index % 2 == 0,
            }
        )
        record, tail, cached = store.resolve(
            request.messages,
            request.requested_model,
            request.tools,
            tool_choice=request.tool_choice,
        )
        assert not cached
        assert tail == request.messages
        assistant = {"role": "assistant", "content": f"response-{index}"}
        store.commit(
            record,
            request.messages,
            assistant,
            {"choices": [{"message": assistant}]},
            request.requested_model,
            request.tools,
            f"conv-{index}",
            request.tool_choice,
        )
    assert len(store) == 500


def test_stream_revision_stress_regression():
    accumulator = MutableTextAccumulator()
    for index in range(100):
        prefix = f"answer-{index}"
        first = accumulator.update(prefix)
        assert first is not None
        second = accumulator.update(f"{prefix}-complete")
        assert second is not None
        revised = accumulator.update(f"replacement-{index}")
        assert revised is not None
        assert revised.revision
