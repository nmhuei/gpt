import json

import pytest

from gpt.reverse.replay import TraceReplay


def _events():
    return [
        {
            "sequence": 1,
            "monotonic_ns": 100,
            "source": "playwright",
            "kind": "request",
            "experiment_id": "E_SEND",
            "metadata": {"marker": "abc"},
        },
        {
            "sequence": 2,
            "monotonic_ns": 200,
            "source": "cdp",
            "kind": "response",
            "experiment_id": "E_SEND",
            "metadata": {},
        },
        {
            "sequence": 3,
            "monotonic_ns": 300,
            "source": "dom",
            "kind": "assistant",
            "experiment_id": "E_SEND",
            "metadata": {},
        },
    ]


@pytest.mark.anyio
async def test_trace_replay_preserves_order_and_reports_summary(tmp_path):
    trace = tmp_path / "events.ndjson"
    trace.write_text(
        "\n".join(json.dumps(event) for event in _events()) + "\n",
        encoding="utf-8",
    )
    replay = TraceReplay.from_ndjson(trace)
    observed = []

    async def handler(event):
        observed.append((event["sequence"], event["source"], event["kind"]))

    summary = await replay.replay(handler)
    assert observed == [
        (1, "playwright", "request"),
        (2, "cdp", "response"),
        (3, "dom", "assistant"),
    ]
    assert summary.event_count == 3
    assert summary.sources == {"playwright": 1, "cdp": 1, "dom": 1}
    assert summary.experiments == {"E_SEND": 3}
    assert (summary.first_sequence, summary.last_sequence) == (1, 3)


def test_trace_replay_rejects_out_of_order_capture():
    events = _events()
    events[1]["sequence"] = 1
    with pytest.raises(ValueError, match="strictly increasing"):
        TraceReplay(events)


def test_trace_replay_normalization_redacts_auth_and_symbolizes_ids():
    events = _events()
    events[0]["headers"] = {"Authorization": "Bearer secret"}
    events[0]["url"] = "https://example.test/c/123e4567-e89b-12d3-a456-426614174000"
    normalized = TraceReplay(events).normalized().events
    assert normalized[0]["headers"]["Authorization"] == "<REDACTED>"
    assert "123e4567-e89b-12d3-a456-426614174000" not in normalized[0]["url"]
