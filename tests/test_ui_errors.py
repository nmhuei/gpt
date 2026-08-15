from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.drivers.ui import UIDriver
from gpt.state import RateLimited


@pytest.mark.anyio
async def test_rate_limit_modal_maps_to_explicit_error():
    page = AsyncMock()
    visible = AsyncMock()
    visible.is_visible = AsyncMock(return_value=True)
    page.locator = MagicMock(return_value=MagicMock(first=visible))
    driver = UIDriver(page)
    with pytest.raises(RateLimited):
        await driver._raise_known_page_error()
