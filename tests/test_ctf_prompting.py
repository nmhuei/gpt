from scripts.legacy.ctf_prompting import (
    EDUCATIONAL_LOCAL_PREFIX,
    frame_local_ctf_prompt,
    neutralize_ctf_text,
)


def test_frame_is_explicitly_authorized_and_local_first():
    prompt = frame_local_ctf_prompt("Reverse engineering this APK exploit payload")
    assert prompt.startswith(EDUCATIONAL_LOCAL_PREFIX)
    assert "workspace" in prompt.lower()
    assert "bên ngoài" in prompt.lower()
    assert "Android app archive" in prompt
    assert "reproduction/solve procedure" in prompt


def test_neutralize_preserves_non_sensitive_text():
    text = "Decode the custom encoding and compare the output bytes."
    assert neutralize_ctf_text(text) == text
