import json
from pathlib import Path

from gpt.streaming import MutableTextAccumulator


def test_mutable_dom_stream_fixture_records_revisions_without_false_deltas():
    fixture = Path(__file__).parent / "fixtures" / "dom_stream_revisions.json"
    events = json.loads(fixture.read_text(encoding="utf-8"))
    accumulator = MutableTextAccumulator()

    for expected in events:
        event = accumulator.update(expected["text"])
        assert event is not None
        assert event.text == expected["delta"]
        assert event.revision is expected["revision"]
        assert event.accumulated_text == expected["text"]

    assert accumulator.text == "Hello **world**!"


def test_mutable_dom_stream_ignores_duplicate_snapshots():
    accumulator = MutableTextAccumulator()
    assert accumulator.update("Answer") is not None
    assert accumulator.update("Answer") is None
