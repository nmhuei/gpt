"""Backward-compatible API import for protocol-neutral prompt compatibility."""

from gpt.promptcompat import canonical_messages, content_text, render_messages

__all__ = ["canonical_messages", "content_text", "render_messages"]
