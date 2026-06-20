"""Tests for integration setup, teardown, the custom service, and the listener."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.setup import async_setup_component

from custom_components.line_ha_bot import _pending_store
from custom_components.line_ha_bot.const import (
    CONF_CHANNEL_ACCESS_TOKEN,
    CONF_CHANNEL_SECRET,
    DOMAIN,
    LINE_PUSH_URL,
    LINE_REPLY_URL,
    PENDING_USERS_KEY,
    RECIPIENTS_KEY,
    SERVICE_SEND_MESSAGE,
)

from .conftest import GROUP_ID, USER_ID, mock_quota_endpoints


def calls_to(aioclient_mock: AiohttpClientMocker, url: str) -> list:
    """Return the recorded calls made to a specific URL."""
    return [c for c in aioclient_mock.mock_calls if str(c[1]) == url]


# --- setup / teardown ------------------------------------------------------


async def test_setup_entry_loads_and_registers_service(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A successful setup loads the entry and registers the custom service."""
    assert init_integration.state is ConfigEntryState.LOADED
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE)
    assert hass.states.get("notify.line_bot_david") is not None


async def test_unload_entry_keeps_service(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Unloading tears down platforms but leaves the service registered."""
    assert await hass.config_entries.async_unload(init_integration.entry_id)
    await hass.async_block_till_done()

    assert init_integration.state is ConfigEntryState.NOT_LOADED
    state = hass.states.get("notify.line_bot_david")
    assert state is None or state.state == "unavailable"
    # The service is intentionally left registered across reloads.
    assert hass.services.has_service(DOMAIN, SERVICE_SEND_MESSAGE)


# --- custom send_message service -------------------------------------------


async def test_service_errors_when_not_set_up(hass: HomeAssistant) -> None:
    """Calling the service with no loaded entry raises a validation error."""
    await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="not set up"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {"entity_id": "notify.line_bot_david", "message": "hi"},
            blocking=True,
        )


async def test_service_sends_push(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """The service resolves the entity and posts to the push API."""
    aioclient_mock.post(LINE_PUSH_URL, status=200, text="{}")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {"entity_id": "notify.line_bot_david", "message": "hello"},
        blocking=True,
    )

    push = calls_to(aioclient_mock, LINE_PUSH_URL)
    assert len(push) == 1
    assert push[0][2]["to"] == USER_ID
    assert push[0][2]["messages"] == [{"type": "text", "text": "hello"}]


async def test_service_unresolvable_entity_skipped(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An entity_id that maps to no LINE id is skipped without error."""
    aioclient_mock.post(LINE_PUSH_URL, status=200, text="{}")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {"entity_id": "notify.line_bot_ghost", "message": "hi"},
        blocking=True,
    )

    assert calls_to(aioclient_mock, LINE_PUSH_URL) == []


async def test_service_reply_token_success_skips_push(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A successful reply send does not fall back to push."""
    aioclient_mock.post(LINE_REPLY_URL, status=200, text="{}")
    aioclient_mock.post(LINE_PUSH_URL, status=200, text="{}")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "entity_id": "notify.line_bot_david",
            "message": "hi",
            "reply_token": "rt123",
        },
        blocking=True,
    )

    reply = calls_to(aioclient_mock, LINE_REPLY_URL)
    assert len(reply) == 1
    assert reply[0][2]["replyToken"] == "rt123"
    assert calls_to(aioclient_mock, LINE_PUSH_URL) == []


async def test_service_reply_token_falls_back_to_push(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A failed reply send falls back to push for the same recipient."""
    aioclient_mock.post(LINE_REPLY_URL, status=400, text="expired")
    aioclient_mock.post(LINE_PUSH_URL, status=200, text="{}")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "entity_id": "notify.line_bot_david",
            "message": "hi",
            "reply_token": "rt123",
        },
        blocking=True,
    )

    assert len(calls_to(aioclient_mock, LINE_REPLY_URL)) == 1
    push = calls_to(aioclient_mock, LINE_PUSH_URL)
    assert len(push) == 1
    assert push[0][2]["to"] == USER_ID


async def test_service_reply_token_used_for_first_target_only(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """With multiple targets the reply token is used once, others use push."""
    aioclient_mock.post(LINE_REPLY_URL, status=200, text="{}")
    aioclient_mock.post(LINE_PUSH_URL, status=200, text="{}")

    await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        {
            "entity_id": ["notify.line_bot_david", "notify.line_bot_family"],
            "message": "hi",
            "reply_token": "rt123",
        },
        blocking=True,
    )

    assert len(calls_to(aioclient_mock, LINE_REPLY_URL)) == 1
    push = calls_to(aioclient_mock, LINE_PUSH_URL)
    assert len(push) == 1
    assert push[0][2]["to"] == GROUP_ID  # the group target used push


async def test_service_rejects_invalid_button_count(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
) -> None:
    """Invalid message content raises before any HTTP call is made."""
    with pytest.raises(ServiceValidationError, match="exactly 2 buttons"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_MESSAGE,
            {
                "entity_id": "notify.line_bot_david",
                "message": "Sure?",
                "template_type": "confirm",
                "buttons": [{"label": "Yes", "action": "message", "data": "y"}],
            },
            blocking=True,
        )


# --- update listener -------------------------------------------------------


async def test_update_listener_reloads_on_credential_change(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A credential change triggers a reload."""
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        new_data = dict(init_integration.data)
        new_data[CONF_CHANNEL_ACCESS_TOKEN] = "rotated-token"
        hass.config_entries.async_update_entry(init_integration, data=new_data)
        await hass.async_block_till_done()

    mock_reload.assert_called_once()


async def test_update_listener_reloads_on_recipient_change(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A recipients change triggers a reload."""
    with patch.object(
        hass.config_entries, "async_reload", new=AsyncMock()
    ) as mock_reload:
        new_data = dict(init_integration.data)
        new_recipients = dict(new_data[RECIPIENTS_KEY])
        new_recipients.pop("family")
        new_data[RECIPIENTS_KEY] = new_recipients
        hass.config_entries.async_update_entry(init_integration, data=new_data)
        await hass.async_block_till_done()

    mock_reload.assert_called_once()


# --- pending captures Store ------------------------------------------------


async def test_setup_migrates_legacy_pending_from_entry_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Captures persisted by an older version (in entry data) move to the Store."""
    legacy_id = "U" + "e" * 32
    mock_quota_endpoints(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CHANNEL_ACCESS_TOKEN: "tok",
            CONF_CHANNEL_SECRET: "sec",
            RECIPIENTS_KEY: {},
            PENDING_USERS_KEY: {legacy_id: "Legacy"},
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Loaded into runtime and the Store, and stripped from entry data.
    assert entry.runtime_data.pending_users[legacy_id] == "Legacy"
    assert PENDING_USERS_KEY not in entry.data
    stored = await entry.runtime_data.store.async_load()
    assert stored[legacy_id] == "Legacy"


async def test_remove_entry_deletes_pending_store(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Removing the config entry deletes its pending-captures Store file."""
    store = init_integration.runtime_data.store
    await store.async_save({"Uxyz": "Someone"})
    assert await store.async_load() == {"Uxyz": "Someone"}

    assert await hass.config_entries.async_remove(init_integration.entry_id)
    await hass.async_block_till_done()

    # A fresh handle on the same key finds nothing - the file was removed.
    assert await _pending_store(hass, init_integration).async_load() is None
