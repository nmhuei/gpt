from gpt.reverse.normalize import normalize_trace, normalize_value
from gpt.reverse.redact import Redactor


def test_normalize_value_replaces_uuids():
    redactor = Redactor()
    raw = {
        "id": "e3b0c442-98fc-1c14-9afb-f4c8996fb924",
        "nested": {
            "ref_id": "e3b0c442-98fc-1c14-9afb-f4c8996fb924",
            "other_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        },
    }
    normalized = normalize_value(raw, redactor)
    assert normalized["id"] == "<UUID_1>"
    assert normalized["nested"]["ref_id"] == "<UUID_1>"
    assert normalized["nested"]["other_id"] == "<UUID_2>"


def test_normalize_trace_processes_events():
    events = [
        {
            "sequence": 1,
            "kind": "request",
            "url": "https://chatgpt.com/backend-api/conversation",
            "metadata": {
                "headers": {"Authorization": "Bearer secret_jwt", "Cookie": "session_id=123"},
                "payload": {"conversation_id": "conv-1111-2222", "prompt": "Hello"},
            },
        }
    ]
    norm_trace = normalize_trace(events)
    assert len(norm_trace) == 1
    ev = norm_trace[0]
    meta = ev["metadata"]
    assert meta["headers"]["authorization"] == "<REDACTED>"
    assert meta["headers"]["cookie"] == "<REDACTED>"
    assert meta["payload"]["conversation_id"] == "<CONV_1>"
    assert meta["payload"]["prompt"] == "Hello"


def test_normalize_uuid_inside_url_without_destroying_url():
    redactor = Redactor()
    value = "https://chatgpt.com/c/e3b0c442-98fc-1c14-9afb-f4c8996fb924?x=1"
    assert normalize_value(value, redactor) == "https://chatgpt.com/c/<UUID_1>?x=1"
