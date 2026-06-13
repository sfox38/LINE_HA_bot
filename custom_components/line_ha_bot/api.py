"""Shared helpers for calling the LINE Messaging API.

Used by both the line_ha_bot.send_message custom service (__init__.py) and the
notify entity platform (notify.py) so that send behaviour, error logging, and
line_bot_send_failed event payloads stay identical across both send paths.
"""

from __future__ import annotations

import logging
import time

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    EVENT_SEND_FAILED,
    LINE_GROUP_SUMMARY_URL,
    LINE_PROFILE_URL,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def fire_send_failed(
    hass: HomeAssistant,
    entity_id: str,
    recipient_name: str,
    error_type: str,
    error_message: str,
    http_status: int | None = None,
) -> None:
    """Fire a line_bot_send_failed event on the HA bus."""
    hass.bus.async_fire(
        EVENT_SEND_FAILED,
        {
            "entity_id": entity_id,
            "recipient_name": recipient_name,
            "error_type": error_type,
            "error_message": error_message,
            "http_status": http_status,
            "timestamp": int(time.time()),
        },
    )


async def async_send_line_message(
    hass: HomeAssistant,
    token: str,
    url: str,
    payload: dict,
    *,
    entity_id: str,
    recipient_name: str,
    is_reply: bool = False,
) -> bool:
    """POST one messages payload to the LINE API.

    Returns True on success. On any failure, logs the error and fires a
    line_bot_send_failed event, then returns False so the caller can decide
    whether to fall back (e.g. reply -> push).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT
        ) as resp:
            if resp.status == 200:
                return True
            body = await resp.text()
            if resp.status == 401:
                msg = (
                    "LINE Bot token is invalid or revoked. "
                    "Please update your Channel Access Token via the integration options."
                )
                _LOGGER.error("%s (entity %s)", msg, entity_id)
                fire_send_failed(hass, entity_id, recipient_name, "token_invalid", msg, 401)
            elif resp.status == 400 and is_reply:
                msg = (
                    "Reply token has expired or has already been used. "
                    "Reply tokens are valid for 30 seconds and single-use only."
                )
                _LOGGER.warning("LINE Bot reply failed for %s: %s", entity_id, msg)
                fire_send_failed(
                    hass, entity_id, recipient_name, "reply_token_expired", msg, 400
                )
            elif resp.status == 400:
                msg = f"Bad request (check user ID and message format): {body}"
                _LOGGER.error("LINE Bot bad request for %s: %s", entity_id, body)
                fire_send_failed(hass, entity_id, recipient_name, "bad_request", msg, 400)
            else:
                msg = f"HTTP {resp.status}: {body}"
                _LOGGER.error(
                    "LINE Bot %s failed for %s: %s",
                    "reply" if is_reply else "push",
                    entity_id,
                    msg,
                )
                fire_send_failed(
                    hass, entity_id, recipient_name, "http_error", msg, resp.status
                )
    except aiohttp.ClientError as err:
        _LOGGER.error("LINE Bot connection error for %s: %s", entity_id, err)
        fire_send_failed(hass, entity_id, recipient_name, "connection_error", str(err))
    return False


async def async_get_profile_name(
    hass: HomeAssistant, token: str, user_id: str
) -> str | None:
    """Fetch the LINE display name for a given user ID.

    Uses the Messaging API GET /v2/bot/profile/{userId} endpoint. Returns the
    displayName string on success, or None if the request fails (e.g. invalid
    token, network error, or user has not added the bot as a friend).
    """
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    url = LINE_PROFILE_URL.format(user_id=user_id)
    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("displayName", user_id)
            return None
    except (aiohttp.ClientError, ValueError):
        return None


async def async_get_group_name(
    hass: HomeAssistant, token: str, group_id: str
) -> str | None:
    """Fetch the display name for a LINE group.

    Uses GET /v2/bot/group/{groupId}/summary. Returns the groupName string
    on success, or None if the request fails (e.g. bot not yet a member).
    """
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Bearer {token}"}
    url = LINE_GROUP_SUMMARY_URL.format(group_id=group_id)
    try:
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("groupName")
            return None
    except (aiohttp.ClientError, ValueError):
        return None
