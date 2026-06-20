"""Tests for the config flow and options flow."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from homeassistant.helpers import network

from custom_components.line_ha_bot.config_flow import (
    CONF_ADD_ANOTHER,
    LineMessagingOptionsFlow,
    _get_external_url,
    _is_emoji,
    _is_valid_name,
    _name_slug_conflicts,
    _sanitize_name,
)
from custom_components.line_ha_bot.const import (
    CLEAR_PENDING,
    CONF_CHANNEL_ACCESS_TOKEN,
    CONF_CHANNEL_SECRET,
    CONF_FRIENDLY_NAME,
    CONF_RECIPIENT_NAME,
    CONF_USER_ID,
    DOMAIN,
    KEY_PENDING_EVENT,
    LINE_TOKEN_VERIFY_URL,
    RECIPIENTS_KEY,
)

from .conftest import USER_ID, mock_quota_endpoints

UNKNOWN_USER = "U" + "b" * 32
UNKNOWN_USER_2 = "U" + "c" * 32
UNKNOWN_GROUP = "C" + "b" * 32

EXTERNAL_URL_PATH = "custom_components.line_ha_bot.config_flow._get_external_url"


# --- pure helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("David", True),
        ("david_1", True),
        ("with space", True),
        ("", False),
        ("🤓", False),
        ("สวัสดี", False),
    ],
)
def test_is_valid_name(name: str, expected: bool) -> None:
    """ASCII names pass; empty and non-ASCII names are rejected."""
    assert _is_valid_name(name) is expected


def test_name_slug_conflicts() -> None:
    """A name slugifying onto an existing recipient is flagged."""
    assert _name_slug_conflicts("Steve", {"steve": {}}) is True
    assert _name_slug_conflicts("Bob", {"steve": {}}) is False


def test_is_emoji() -> None:
    """Emoji code points are detected; plain letters are not."""
    assert _is_emoji("🤓") is True
    assert _is_emoji("A") is False


def test_get_external_url_none_when_unavailable(hass: HomeAssistant) -> None:
    """With no HTTPS external URL configured the helper returns None."""
    with patch.object(
        network, "get_url", side_effect=network.NoURLAvailableError
    ):
        assert _get_external_url(hass) is None


def test_get_external_url_returns_configured_url(hass: HomeAssistant) -> None:
    """A resolvable HTTPS external URL is returned."""
    with patch.object(network, "get_url", return_value="https://ha.example.com"):
        assert _get_external_url(hass) == "https://ha.example.com"


@pytest.mark.parametrize(
    ("display", "expected"),
    [
        ("David", "david"),
        ("🤓", "nerd"),
        ("David 🤓", "david_nerd"),
        ("สวัสดี 🤓 David", "swasdii_nerd_david"),
    ],
)
def test_sanitize_name(display: str, expected: str) -> None:
    """Display names are romanized and emoji are spelled out."""
    assert _sanitize_name(display) == expected


# --- config flow: credentials step -----------------------------------------


async def test_user_step_invalid_secret(hass: HomeAssistant) -> None:
    """An empty secret returns an inline error on the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "   "},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_CHANNEL_SECRET] == "invalid_secret"


async def test_user_step_invalid_token(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A token rejected by LINE returns an invalid_token error."""
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHANNEL_ACCESS_TOKEN: "bad", CONF_CHANNEL_SECRET: "sec"},
    )
    assert result["errors"]["base"] == "invalid_token"


async def test_user_step_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A network failure verifying the token returns cannot_connect."""
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, exc=aiohttp.ClientError("down"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "sec"},
    )
    assert result["errors"]["base"] == "cannot_connect"


async def test_full_config_flow_creates_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A valid token plus webhook confirmation creates the config entry."""
    await async_setup_component(hass, "http", {})
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=200, json={})
    mock_quota_endpoints(aioclient_mock)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    # The user step chains straight into webhook_info, which needs the external
    # URL, so the patch must cover both the credentials submit and the confirm.
    with patch(EXTERNAL_URL_PATH, return_value="https://ha.example.com"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "sec"},
        )
        assert result["step_id"] == "webhook_info"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirmed": True}
        )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CHANNEL_ACCESS_TOKEN] == "tok"
    assert result["data"][RECIPIENTS_KEY] == {}


async def test_webhook_step_requires_confirmation(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Submitting webhook_info without ticking the box returns an error."""
    await async_setup_component(hass, "http", {})
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=200, json={})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(EXTERNAL_URL_PATH, return_value="https://ha.example.com"):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "sec"},
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"confirmed": False}
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["confirmed"] == "confirm_required"


async def test_webhook_step_aborts_without_external_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """No HTTPS external URL aborts the flow."""
    await async_setup_component(hass, "http", {})
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=200, json={})

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(EXTERNAL_URL_PATH, return_value=None):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "sec"},
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_external_url"


# --- options flow ----------------------------------------------------------


async def test_options_menu_shown(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """The options flow opens on the action menu."""
    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {
        "add_recipient",
        "remove_recipient",
        "rotate_token",
    }


async def test_options_add_recipient_full_flow(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Adding a captured user walks select -> name and stores the recipient."""
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    # Pending already present -> jumps straight to the select form.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    assert result["step_id"] == "select_recipient"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    assert result["step_id"] == "name_recipient"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_RECIPIENT_NAME: "stranger",
            CONF_FRIENDLY_NAME: "Stranger",
            CONF_ADD_ANOTHER: False,
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "stranger" in init_integration.data[RECIPIENTS_KEY]
    added = init_integration.data[RECIPIENTS_KEY]["stranger"]
    assert added["user_id"] == UNKNOWN_USER
    assert added["type"] == "user"
    # The pending entry is consumed once named.
    assert UNKNOWN_USER not in init_integration.runtime_data.pending_users


async def test_options_add_group_detects_type(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A captured group id is stored with type 'group'."""
    init_integration.runtime_data.pending_users[UNKNOWN_GROUP] = "Book Club"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_GROUP}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECIPIENT_NAME: "book_club", CONF_ADD_ANOTHER: False},
    )
    await hass.async_block_till_done()

    assert init_integration.data[RECIPIENTS_KEY]["book_club"]["type"] == "group"


async def test_options_name_recipient_rejects_invalid_name(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A non-ASCII recipient name is rejected with an inline error."""
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "สวัสดี"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_RECIPIENT_NAME] == "invalid_name"


async def test_options_name_recipient_rejects_duplicate(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A name colliding with an existing recipient is rejected."""
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "david"}
    )
    assert result["errors"][CONF_RECIPIENT_NAME] == "duplicate_name"


async def test_options_name_recipient_rejects_empty_name(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """An empty (whitespace-only) recipient name is rejected."""
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "   "}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_RECIPIENT_NAME] == "name_required"


async def test_options_name_recipient_rejects_duplicate_user_id(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A captured id that is already a recipient is rejected on naming."""
    # USER_ID already belongs to the "david" recipient from the fixture.
    init_integration.runtime_data.pending_users[USER_ID] = "David again"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: USER_ID}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "dave2"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "duplicate_user_id"


async def test_options_add_another_loops_back_and_saves_both(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Ticking 'add another' saves the first recipient and returns for the second."""
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"
    init_integration.runtime_data.pending_users[UNKNOWN_USER_2] = "Newcomer"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    # Name the first one and ask to add another.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECIPIENT_NAME: "stranger", CONF_ADD_ANOTHER: True},
    )
    await hass.async_block_till_done()
    # Loops straight back to the select form because a second capture is pending.
    assert result["step_id"] == "select_recipient"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER_2}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_RECIPIENT_NAME: "newcomer", CONF_ADD_ANOTHER: False},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    recipients = init_integration.data[RECIPIENTS_KEY]
    assert "stranger" in recipients
    assert "newcomer" in recipients


async def test_options_add_recipient_waits_then_advances_on_capture(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """With nothing pending the spinner waits, and a capture wakes it via the event."""
    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    # No pending captures yet -> the spinner is shown.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    # The webhook would populate pending and set the shared event.
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"
    hass.data[DOMAIN][KEY_PENDING_EVENT].set()
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "select_recipient"

    # Complete the flow so no wait task lingers.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "stranger", CONF_ADD_ANOTHER: False}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_wait_for_pending_gives_up_after_timeout(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """_wait_for_pending returns (does not hang) once the timeout elapses."""
    flow = LineMessagingOptionsFlow()
    flow.hass = hass

    with (
        patch.object(flow, "_get_pending_users", return_value={}),
        patch("custom_components.line_ha_bot.config_flow._WAIT_TIMEOUT", 0.05),
    ):
        # A small positive timeout exercises the real wait-then-give-up path.
        # Wrapped in wait_for so a regression that hangs fails fast instead of stalling.
        await asyncio.wait_for(flow._wait_for_pending(), timeout=5)


async def test_options_select_clear_pending(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Selecting the clear sentinel wipes pending (and the Store) then waits anew.

    Clearing returns to the spinner, so to leave no lingering wait task we then
    drive the spinner the way the webhook would: a fresh capture wakes it and the
    flow advances to the select form, which we complete.
    """
    init_integration.runtime_data.pending_users[UNKNOWN_USER] = "Stranger"

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_recipient"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: CLEAR_PENDING}
    )

    assert init_integration.runtime_data.pending_users == {}
    # The cleared captures are wiped from the Store too.
    assert await init_integration.runtime_data.store.async_load() == {}
    # Back to the waiting spinner since nothing is left to select.
    assert result["type"] is FlowResultType.SHOW_PROGRESS

    # A new capture lands: mimic the webhook populating pending and waking the spinner.
    init_integration.runtime_data.pending_users[UNKNOWN_USER_2] = "Newcomer"
    hass.data[DOMAIN][KEY_PENDING_EVENT].set()
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "select_recipient"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_USER_ID: UNKNOWN_USER_2}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "newcomer", CONF_ADD_ANOTHER: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "newcomer" in init_integration.data[RECIPIENTS_KEY]


async def test_options_remove_recipient(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """Removing a recipient deletes it from the entry data."""
    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_recipient"}
    )
    assert result["step_id"] == "remove_recipient"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_RECIPIENT_NAME: "david"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "david" not in init_integration.data[RECIPIENTS_KEY]
    assert "family" in init_integration.data[RECIPIENTS_KEY]


async def test_options_remove_recipient_aborts_when_empty(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Remove aborts when there are no recipients."""
    mock_quota_endpoints(aioclient_mock)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_CHANNEL_ACCESS_TOKEN: "tok",
            CONF_CHANNEL_SECRET: "sec",
            RECIPIENTS_KEY: {},
        },
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_recipient"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_recipients"


async def test_options_rotate_token(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Updating credentials verifies and stores the new token and secret."""
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=200, json={})

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "rotate_token"}
    )
    assert result["step_id"] == "rotate_token"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_CHANNEL_ACCESS_TOKEN: "new-token",
            CONF_CHANNEL_SECRET: "new-secret",
        },
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert init_integration.data[CONF_CHANNEL_ACCESS_TOKEN] == "new-token"
    assert init_integration.data[CONF_CHANNEL_SECRET] == "new-secret"


async def test_options_rotate_token_invalid(
    hass: HomeAssistant,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """A rejected new token returns an inline error and does not save."""
    aioclient_mock.post(LINE_TOKEN_VERIFY_URL, status=401)

    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "rotate_token"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CHANNEL_ACCESS_TOKEN: "bad", CONF_CHANNEL_SECRET: "sec"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_token"


async def test_options_rotate_token_empty_secret(
    hass: HomeAssistant, init_integration: MockConfigEntry
) -> None:
    """A blank secret on rotate is rejected before the token is verified."""
    result = await hass.config_entries.options.async_init(
        init_integration.entry_id
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "rotate_token"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CHANNEL_ACCESS_TOKEN: "tok", CONF_CHANNEL_SECRET: "   "},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"][CONF_CHANNEL_SECRET] == "invalid_secret"
