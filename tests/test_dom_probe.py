from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.reverse.dom_probe import DOMProbe


@pytest.mark.anyio
async def test_dom_probe_finds_elements():
    mock_page = AsyncMock()
    mock_page.url = "https://chatgpt.com/"
    mock_page.title = AsyncMock(return_value="ChatGPT")
    mock_page.content = AsyncMock(return_value="<html><body><div id='prompt-textarea'></div></body></html>")

    mock_locator = AsyncMock()
    mock_locator.is_visible = AsyncMock(return_value=True)
    mock_locator.evaluate = AsyncMock(return_value="div")
    mock_locator.get_attribute = AsyncMock(side_effect=lambda attr: "textbox" if attr == "role" else None)

    mock_page.locator = MagicMock(return_value=MagicMock(first=mock_locator))

    probe = DOMProbe(mock_page)
    recon = await probe.probe_all()

    assert recon["title"] == "ChatGPT"
    assert "composer" in recon["elements"]
    assert recon["elements"]["composer"]["tag"] == "div"
    assert recon["cloudflare_challenge"] is False
    assert recon["auth_status"] == "anonymous_free"
