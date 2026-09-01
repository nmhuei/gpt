from unittest.mock import AsyncMock, MagicMock

import pytest

from gpt.drivers.ui import UIDriver
from gpt.state import ModelUnavailable, UIChanged
from gpt.types import SendRequest


@pytest.mark.anyio
async def test_ui_driver_reasoning_effort_requires_exact_semantic_option():
    page = MagicMock()
    driver = UIDriver(page)

    picker_btn = MagicMock()
    picker_btn.inner_text = AsyncMock(return_value="High")
    picker_btn.click = AsyncMock()
    driver._first_visible = AsyncMock(return_value=picker_btn)

    def locator_side_effect(selector):
        loc = MagicMock()
        loc.count = AsyncMock(return_value=0)
        loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
        return loc

    page.locator.side_effect = locator_side_effect
    page.keyboard.press = AsyncMock()

    with pytest.raises(ModelUnavailable, match="not available"):
        await driver.select_reasoning_effort("medium")


@pytest.mark.anyio
async def test_ui_driver_reasoning_effort_menu_fallback():
    page = MagicMock()
    driver = UIDriver(page)

    picker_btn = MagicMock()
    picker_btn.inner_text = AsyncMock(return_value="High")
    picker_btn.click = AsyncMock()
    driver._first_visible = AsyncMock(return_value=picker_btn)

    adv_toggle = MagicMock()
    adv_toggle.is_visible = AsyncMock(return_value=True)
    adv_toggle.click = AsyncMock()

    effort_trigger = MagicMock()
    effort_trigger.is_visible = AsyncMock(return_value=True)
    effort_trigger.click = AsyncMock()

    med_opt = MagicMock()
    med_opt.inner_text = AsyncMock(return_value="Medium")
    med_opt.click = AsyncMock()
    med_opt.get_attribute = AsyncMock(side_effect=lambda name: "true" if name == "aria-checked" else None)

    options_locator = MagicMock()
    options_locator.count = AsyncMock(return_value=1)
    options_locator.nth.return_value = med_opt

    def locator_side_effect(selector):
        if "Tick" in selector:
            loc = MagicMock()
            loc.count = AsyncMock(return_value=0)
            return loc
        if "Advanced" in selector:
            loc = MagicMock()
            loc.first = adv_toggle
            loc.count = AsyncMock(return_value=1)
            return loc
        if "Effort" in selector:
            loc = MagicMock()
            loc.first = effort_trigger
            loc.count = AsyncMock(return_value=1)
            return loc
        if "menuitemradio" in selector:
            return options_locator
        loc = MagicMock()
        loc.count = AsyncMock(return_value=0)
        loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
        return loc

    page.locator.side_effect = locator_side_effect
    page.keyboard.press = AsyncMock()

    res = await driver.select_reasoning_effort("medium")
    assert res == "medium"
    med_opt.click.assert_awaited()


@pytest.mark.anyio
async def test_missing_picker_keeps_the_current_default_model():
    page = MagicMock()
    driver = UIDriver(page)

    # A missing picker is a capability observation, not a tier/model inference.
    driver._first_visible = AsyncMock(return_value=None)

    models = await driver.list_models()
    assert len(models) == 1
    assert models[0].id == "chatgpt-web"
    assert models[0].label == "ChatGPT Web default"

    # Selecting default model succeeds
    selected = await driver.select_model("chatgpt-web")
    assert selected.label == "ChatGPT Web default"

    with pytest.raises(ModelUnavailable) as exc_info:
        await driver.select_model("o3")
    assert "no model picker" in str(exc_info.value)


@pytest.mark.anyio
async def test_reasoning_effort_selection_requires_post_click_readback():
    page = MagicMock()
    driver = UIDriver(page)

    picker = MagicMock()
    picker.click = AsyncMock()
    picker.inner_text = AsyncMock(return_value="Unchanged")
    driver._first_visible = AsyncMock(return_value=picker)

    effort_trigger = MagicMock()
    effort_trigger.is_visible = AsyncMock(return_value=True)
    effort_trigger.click = AsyncMock()

    option = MagicMock()
    option.inner_text = AsyncMock(return_value="High")
    option.click = AsyncMock()
    option.get_attribute = AsyncMock(return_value=None)

    options = MagicMock()
    options.count = AsyncMock(return_value=1)
    options.nth.return_value = option

    def locator_side_effect(selector):
        if "Effort" in selector:
            loc = MagicMock()
            loc.first = effort_trigger
            return loc
        if "menuitemradio" in selector:
            return options
        loc = MagicMock()
        loc.first = MagicMock(is_visible=AsyncMock(return_value=False))
        loc.count = AsyncMock(return_value=0)
        return loc

    page.locator.side_effect = locator_side_effect
    page.keyboard.press = AsyncMock()

    with pytest.raises(UIChanged, match="did not read back"):
        await driver.select_reasoning_effort("high")


@pytest.mark.anyio
async def test_capability_snapshot_uses_observed_model_and_effort_state_only():
    from gpt.types import ModelInfo

    page = MagicMock()
    driver = UIDriver(page)
    picker = MagicMock()
    driver.auth_status = AsyncMock(return_value="authenticated")
    driver._first_visible = AsyncMock(return_value=picker)
    driver.list_models = AsyncMock(
        return_value=[
            ModelInfo(
                id="observed-model",
                label="Observed Model",
                selected=True,
                source="ui",
            )
        ]
    )
    driver._discover_reasoning_effort_state = AsyncMock(
        return_value=(["Fast", "Balanced", "Deep"], "Deep")
    )

    snapshot = await driver.capabilities()

    assert snapshot.auth_status == "authenticated"
    assert snapshot.has_model_picker is True
    assert snapshot.selected_model == "Observed Model"
    assert snapshot.reasoning_efforts == ["Fast", "Balanced", "Deep"]
    assert snapshot.selected_effort == "Deep"
    assert snapshot.models[0].selected_effort == "Deep"
    assert snapshot.models[0].reasoning_efforts == ["Fast", "Balanced", "Deep"]


@pytest.mark.anyio
async def test_send_does_not_silently_ignore_failed_55_high_selection():
    page = MagicMock()
    driver = UIDriver(page)
    driver._assistant_count = AsyncMock(return_value=0)
    driver.dismiss_popups = AsyncMock()
    driver._raise_known_page_error = AsyncMock()

    picker = MagicMock()
    picker.inner_text = AsyncMock(return_value="GPT-5.5")
    driver._first_visible = AsyncMock(return_value=picker)
    driver.select_reasoning_effort = AsyncMock(
        side_effect=UIChanged("effort readback failed")
    )

    with pytest.raises(UIChanged, match="effort readback failed"):
        await driver.send(SendRequest(text="hello"))

    driver.select_reasoning_effort.assert_awaited_once_with("high")


@pytest.mark.anyio
async def test_send_file_upload_failure_is_not_silently_dropped(tmp_path):
    attachment = tmp_path / "payload.bin"
    attachment.write_bytes(b"\x00\x01\x02")

    page = MagicMock()
    file_input = MagicMock()
    file_input.count = AsyncMock(return_value=1)
    file_input.set_input_files = AsyncMock(side_effect=RuntimeError("upload boom"))
    locator = MagicMock()
    locator.first = file_input
    page.locator.return_value = locator

    driver = UIDriver(page)
    driver._assistant_count = AsyncMock(return_value=0)
    driver.dismiss_popups = AsyncMock()
    driver._raise_known_page_error = AsyncMock()

    with pytest.raises(UIChanged, match="file attachment upload failed"):
        await driver.send(
            SendRequest(text="inspect this", files=(str(attachment),))
        )


@pytest.mark.anyio
async def test_send_binary_attachment_requires_file_input(tmp_path):
    attachment = tmp_path / "payload.bin"
    attachment.write_bytes(b"\x00\x01\x02")

    page = MagicMock()
    file_input = MagicMock()
    file_input.count = AsyncMock(return_value=0)
    locator = MagicMock()
    locator.first = file_input
    page.locator.return_value = locator

    driver = UIDriver(page)
    driver._assistant_count = AsyncMock(return_value=0)
    driver.dismiss_popups = AsyncMock()
    driver._raise_known_page_error = AsyncMock()

    with pytest.raises(UIChanged, match="file input is unavailable"):
        await driver.send(
            SendRequest(text="inspect this", files=(str(attachment),))
        )
