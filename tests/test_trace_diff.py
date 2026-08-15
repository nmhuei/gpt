from gpt.reverse.diff import classify_field, diff_json


def test_diff_json_identifies_differences():
    a = {
        "action": "next",
        "model": "gpt-4o",
        "messages": [{"id": 1, "text": "hello"}],
    }
    b = {
        "action": "next",
        "model": "o3-mini",
        "messages": [{"id": 1, "text": "hello world"}],
    }
    diffs = diff_json(a, b)
    assert "$.model" in diffs
    assert diffs["$.model"] == {"a": "gpt-4o", "b": "o3-mini"}
    assert "$.messages[0].text" in diffs
    assert diffs["$.messages[0].text"] == {"a": "hello", "b": "hello world"}
    assert "$.action" not in diffs


def test_classify_field_categorizes_variance():
    run_a = {"prompt": "Prompt A", "model": "auto", "conv_id": "c1"}
    run_b = {"prompt": "Prompt B", "model": "auto", "conv_id": "c2"}
    run_c = {"prompt": "Prompt C", "model": "auto", "conv_id": "c3"}

    runs = [run_a, run_b, run_c]
    assert classify_field("$.model", runs, ["prompt", "prompt", "prompt"]) == "CONSTANT"
    assert classify_field("$.prompt", runs, ["prompt", "prompt", "prompt"]) == "CONTENT_DEPENDENT"
    assert classify_field("$.conv_id", runs, ["prompt", "prompt", "prompt"]) == "PER_CONVERSATION"
