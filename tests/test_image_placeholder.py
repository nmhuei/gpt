"""P1-2A: unsupported image content blocks must surface as placeholders.

Claude CLI sends multimodal message content (``{"type": "image", ...}``
blocks).  The renderer used to drop them silently, so the model never knew
an image had been sent and answered as if none existed.  These tests pin the
placeholder behavior: explicit stand-in text with mime/size, kill-switch
rollback, and no effect on text-only messages.
"""

from gpt.api.openai_types import estimate_openai_usage
from gpt.utils.promptcompat import (
    IMAGE_PLACEHOLDER_ENV,
    content_text,
    render_messages,
)


def _b64_image(media_type: str = "image/png", payload_chars: int = 4000) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            # Fake base64; size estimate is length*3//4 -> 3000 bytes -> ~3KB.
            "data": "A" * payload_chars,
        },
    }


def _content(message: dict) -> str:
    return content_text(message["content"])


def test_image_block_renders_placeholder_with_mime_and_size(monkeypatch):
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is in this picture?"},
            _b64_image("image/png"),
        ],
    }
    rendered = render_messages([message], initial=False, tools=[], tool_choice="auto")
    placeholder = "[image omitted: image/png ~3KB — image upload not supported yet]"
    assert placeholder in rendered
    assert "what is in this picture?" in rendered
    assert placeholder in _content(message)
    assert "<WEBGPT_MESSAGE role=\"user\">" in rendered


def test_kill_switch_restores_silent_drop(monkeypatch):
    monkeypatch.setenv(IMAGE_PLACEHOLDER_ENV, "0")
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "look at this"},
            _b64_image(),
        ],
    }
    rendered = render_messages([message], initial=False, tools=[], tool_choice="auto")
    assert "image omitted" not in rendered
    assert _content(message) == "look at this"


def test_text_only_message_unchanged(monkeypatch):
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "plain line one"},
            {"type": "text", "text": "plain line two"},
        ],
    }
    rendered = render_messages([message], initial=False, tools=[], tool_choice="auto")
    assert "image omitted" not in rendered
    assert _content(message) == "plain line one\nplain line two"


def test_multiple_images_placeholders_in_order(monkeypatch):
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    jpeg_small = {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": "B" * 800,  # 600 bytes -> ~1KB
        },
    }
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "first"},
            _b64_image("image/png"),
            {"type": "text", "text": "second"},
            jpeg_small,
        ],
    }
    rendered_text = _content(message)
    png_ph = "[image omitted: image/png ~3KB — image upload not supported yet]"
    jpeg_ph = "[image omitted: image/jpeg ~1KB — image upload not supported yet]"
    assert rendered_text.index(png_ph) < rendered_text.index("second")
    assert rendered_text.index(jpeg_ph) > rendered_text.index("second")
    assert rendered_text.count("[image omitted:") == 2
    rendered = render_messages([message], initial=False, tools=[], tool_choice="auto")
    assert png_ph in rendered and jpeg_ph in rendered


def test_image_url_data_uri_placeholder(monkeypatch):
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    block = {
        "type": "image_url",
        "image_url": {"url": "data:image/webp;base64," + "C" * 1600},
    }
    text = content_text([block])
    # 1600 chars b64 -> 1200 bytes -> ceil(1200/1024) = 2KB.
    assert text == "[image omitted: image/webp ~2KB — image upload not supported yet]"


def test_openai_image_url_renders_placeholder_in_prompt(monkeypatch):
    """OpenAI chat-branch image parts reach the rendered prompt as placeholders.

    ``parse_chat_completion_request`` keeps block-array content verbatim, so
    this is exactly the shape the OpenAI branch hands to ``render_messages``.
    """
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what do you see?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/cat.png"}},
        ],
    }
    rendered = render_messages([message], initial=False, tools=[], tool_choice="auto")
    assert "[image omitted: unknown — image upload not supported yet]" in rendered
    assert "what do you see?" in rendered
    monkeypatch.setenv(IMAGE_PLACEHOLDER_ENV, "0")
    assert "image omitted" not in render_messages(
        [message], initial=False, tools=[], tool_choice="auto"
    )


def test_placeholder_counts_toward_usage_estimate(monkeypatch):
    """P1-2A: the chars/4 estimator bills the placeholder text.

    The usage estimator consumes the rendered prompt string, so once the
    placeholder is part of that string its characters flow into the
    prompt_tokens estimate automatically -- pinned here so a future refactor
    cannot silently re-introduce free (invisible) content.
    """
    monkeypatch.delenv(IMAGE_PLACEHOLDER_ENV, raising=False)
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            _b64_image(),
        ],
    }
    with_placeholder = render_messages(
        [message], initial=False, tools=[], tool_choice="auto"
    )
    monkeypatch.setenv(IMAGE_PLACEHOLDER_ENV, "0")
    without_placeholder = render_messages(
        [message], initial=False, tools=[], tool_choice="auto"
    )

    assert len(with_placeholder) > len(without_placeholder)
    usage_with = estimate_openai_usage(prompt_text=with_placeholder)
    usage_without = estimate_openai_usage(prompt_text=without_placeholder)
    assert usage_with["prompt_tokens"] > usage_without["prompt_tokens"]
    # Exact arithmetic: tokens are ceil(chars/4), floored at one.
    assert usage_with["prompt_tokens"] == max(1, -(-len(with_placeholder) // 4))
    assert usage_without["prompt_tokens"] == max(1, -(-len(without_placeholder) // 4))
