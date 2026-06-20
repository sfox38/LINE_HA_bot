"""Tests for the LINE webhook view in __init__.py.

These exercise the registered HTTP view end to end via the HA test client, so
they cover JSON parsing, signature verification, event dispatch, and pending
capture exactly as a real LINE request would hit them.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.line_ha_bot.const import (
    DOMAIN,
    EVENT_MESSAGE_RECEIVED,
    LINE_GROUP_SUMMARY_URL,
    LINE_PROFILE_URL,
    LINE_TEST_REPLY_TOKEN,
    LINE_WEBHOOK_PATH,
    MAX_PENDING_USERS,
)

from .conftest import (
    GROUP_ID,
    SECRET,
    USER_ID,
    USER_ID_2,
    sign,
    webhook_body,
)

UNKNOWN_USER = "U" + "a" * 32
UNKNOWN_GROUP = "C" + "a" * 32


def listen(hass: HomeAssistant) -> list:
    """Collect line_bot_message_received events."""
    events: list = []
    hass.bus.async_listen(EVENT_MESSAGE_RECEIVED, events.append)
    return events


async def post(client, body: bytes, *, secret: str = SECRET, signature=None):
    """POST a signed body to the webhook path."""
    headers = {"X-Line-Signature": sign(secret, body) if signature is None else signature}
    return await client.post(LINE_WEBHOOK_PATH, data=body, headers=headers)


@pytest.fixture
async def client(hass: HomeAssistant, init_integration, hass_client_no_auth):
    """Return an unauthenticated HTTP client with the integration set up."""
    return await hass_client_no_auth()


async def test_invalid_json_returns_400(hass: HomeAssistant, client) -> None:
    """A body that is not valid JSON is rejected with 400."""
    resp = await post(client, b"not json{")
    assert resp.status == 400


async def test_non_dict_json_returns_400(hass: HomeAssistant, client) -> None:
    """A JSON payload that is not an object is rejected with 400."""
    resp = await post(client, b"[1, 2, 3]")
    assert resp.status == 400


async def test_malformed_events_array_returns_400(hass: HomeAssistant, client) -> None:
    """An events value that is not a list of dicts is rejected with 400."""
    resp = await post(client, b'{"events": ["nope"]}')
    assert resp.status == 400


async def test_empty_events_returns_200(hass: HomeAssistant, client) -> None:
    """LINE's Verify health check (empty events) returns 200 without a signature."""
    resp = await post(client, webhook_body([]), signature="")
    assert resp.status == 200


async def test_test_reply_token_returns_200(hass: HomeAssistant, client) -> None:
    """LINE's internal test event (all-zero reply token) returns 200."""
    body = webhook_body(
        [{"type": "message", "replyToken": LINE_TEST_REPLY_TOKEN, "source": {}}]
    )
    resp = await post(client, body, signature="")
    assert resp.status == 200


async def test_no_loaded_entry_returns_200(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """With the component set up but no config entry, real events return 200."""
    await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    no_entry_client = await hass_client_no_auth()

    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(no_entry_client, body)
    assert resp.status == 200


async def test_invalid_signature_returns_400(hass: HomeAssistant, client) -> None:
    """A real event with a bad signature is rejected with 400."""
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body, signature="wrongsignature")
    assert resp.status == 400


async def test_known_user_text_message_fires_event(
    hass: HomeAssistant, client
) -> None:
    """A text message from a known user fires line_bot_message_received."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt123",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "msg1", "text": "hello"},
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert len(events) == 1
    data = events[0].data
    assert data["type"] == "text"
    assert data["user_id"] == USER_ID
    assert data["group_id"] is None
    assert data["entity_id"] == "notify.line_bot_david"
    assert data["recipient_name"] == "david"
    assert data["message_text"] == "hello"
    assert data["message_id"] == "msg1"
    assert data["content_url"] is None
    assert data["reply_token"] == "rt123"
    assert data["timestamp"] == 1700000000


async def test_known_user_media_message_sets_content_url(
    hass: HomeAssistant, client
) -> None:
    """An image message exposes a content_url and null message_text."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "image", "id": "img99"},
            }
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    data = events[0].data
    assert data["type"] == "image"
    assert data["message_text"] is None
    assert data["content_url"].endswith("/img99/content")


async def test_known_user_postback_fires_event(hass: HomeAssistant, client) -> None:
    """A postback from a known user fires with postback_data."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "postback",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "postback": {"data": "lights=on"},
            }
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    data = events[0].data
    assert data["type"] == "postback"
    assert data["postback_data"] == "lights=on"
    assert data["message_text"] is None


async def test_group_message_fires_event(hass: HomeAssistant, client) -> None:
    """A message in a known group matches on group_id."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "group", "groupId": GROUP_ID, "userId": USER_ID_2},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "g1", "text": "yo"},
            }
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    data = events[0].data
    assert data["group_id"] == GROUP_ID
    assert data["user_id"] == USER_ID_2
    assert data["entity_id"] == "notify.line_bot_family"
    assert data["recipient_name"] == "family"


async def test_unsupported_event_type_ignored(hass: HomeAssistant, client) -> None:
    """A non-message/postback event from a known user fires nothing."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "follow",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert events == []


async def test_room_event_ignored(hass: HomeAssistant, client) -> None:
    """Room-source events are not supported and fire nothing."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "room", "roomId": "R123", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert events == []


async def test_unknown_user_captured_to_pending(
    hass: HomeAssistant,
    client,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unknown sender is captured into pending_users with their display name."""
    aioclient_mock.get(
        LINE_PROFILE_URL.format(user_id=UNKNOWN_USER),
        json={"displayName": "Stranger"},
    )
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": UNKNOWN_USER},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert events == []  # capture does not fire a message event
    pending = init_integration.runtime_data.pending_users
    assert pending[UNKNOWN_USER] == "Stranger"
    # The capture is persisted to the Store (not to config entry data).
    stored = await init_integration.runtime_data.store.async_load()
    assert stored[UNKNOWN_USER] == "Stranger"


async def test_pending_cap_evicts_oldest(
    hass: HomeAssistant,
    client,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Capturing past the cap evicts the oldest pending entry."""
    pending = init_integration.runtime_data.pending_users
    for i in range(MAX_PENDING_USERS):
        pending[f"Uold{i:032d}"] = f"Old {i}"

    aioclient_mock.get(
        LINE_PROFILE_URL.format(user_id=UNKNOWN_USER),
        json={"displayName": "Newcomer"},
    )
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": UNKNOWN_USER},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    assert len(pending) == MAX_PENDING_USERS
    assert "Uold" + "0" * 32 not in pending  # oldest evicted
    assert pending[UNKNOWN_USER] == "Newcomer"


async def test_already_pending_user_not_recaptured(
    hass: HomeAssistant,
    client,
    init_integration: MockConfigEntry,
) -> None:
    """A sender already in pending is left untouched (no profile lookup)."""
    pending = init_integration.runtime_data.pending_users
    pending[UNKNOWN_USER] = "Existing"
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": UNKNOWN_USER},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert pending[UNKNOWN_USER] == "Existing"


async def test_missing_signature_returns_400(hass: HomeAssistant, client) -> None:
    """A real event with an empty signature header is rejected."""
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body, signature="")
    assert resp.status == 400


async def test_mixed_test_token_event_skipped(hass: HomeAssistant, client) -> None:
    """When events are mixed, the internal test event is skipped per-event."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": LINE_TEST_REPLY_TOKEN,
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "test"},
            },
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "2", "text": "real"},
            },
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["message_text"] == "real"


async def test_malformed_sources_ignored(hass: HomeAssistant, client) -> None:
    """Events with unusable sources fire nothing and still return 200."""
    events = listen(hass)
    body = webhook_body(
        [
            {"type": "message", "source": "not-a-dict", "message": {}},
            {"type": "message", "source": {"type": "group"}, "message": {}},
            {"type": "message", "source": {"type": "user"}, "message": {}},
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert events == []


async def test_bad_timestamp_and_non_dict_message(
    hass: HomeAssistant, client
) -> None:
    """A bad timestamp falls back to 0 and a non-dict message becomes empty."""
    events = listen(hass)
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": USER_ID},
                "timestamp": "not-a-number",
                "message": "not-a-dict",
            }
        ]
    )
    await post(client, body)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["timestamp"] == 0
    assert events[0].data["type"] == ""
    assert events[0].data["message_text"] is None


async def test_unknown_group_captured(
    hass: HomeAssistant,
    client,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An unknown group is captured using its group summary name."""
    aioclient_mock.get(
        LINE_GROUP_SUMMARY_URL.format(group_id=UNKNOWN_GROUP),
        json={"groupName": "Book Club"},
    )
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "group", "groupId": UNKNOWN_GROUP},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            }
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    assert init_integration.runtime_data.pending_users[UNKNOWN_GROUP] == "Book Club"


async def test_multiple_unknown_senders_captured_in_one_request(
    hass: HomeAssistant,
    client,
    init_integration: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Several new senders in one batch are all captured (names fetched together)."""
    other_user = "U" + "b" * 32
    aioclient_mock.get(
        LINE_PROFILE_URL.format(user_id=UNKNOWN_USER), json={"displayName": "First"}
    )
    aioclient_mock.get(
        LINE_PROFILE_URL.format(user_id=other_user), json={"displayName": "Second"}
    )
    body = webhook_body(
        [
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": UNKNOWN_USER},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "1", "text": "hi"},
            },
            {
                "type": "message",
                "replyToken": "rt",
                "source": {"type": "user", "userId": other_user},
                "timestamp": 1700000000000,
                "message": {"type": "text", "id": "2", "text": "yo"},
            },
        ]
    )
    resp = await post(client, body)
    await hass.async_block_till_done()

    assert resp.status == 200
    pending = init_integration.runtime_data.pending_users
    assert pending[UNKNOWN_USER] == "First"
    assert pending[other_user] == "Second"
    # Both captures are persisted to the Store in a single write.
    stored = await init_integration.runtime_data.store.async_load()
    assert stored[UNKNOWN_USER] == "First"
    assert stored[other_user] == "Second"
