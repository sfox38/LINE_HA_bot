"""
LINE Bot integration for Home Assistant.

Allows Home Assistant to send messages to LINE users and groups via the LINE
Messaging API, and to react to incoming LINE messages via HA bus events.

Architecture overview:
- __init__.py   : Integration setup, teardown, custom service, and permanent webhook view.
- api.py        : Shared LINE API helpers (send, profile/group lookup, error events).
- config_flow.py: UI-driven setup and options flows (credentials, recipients).
- notify.py     : NotifyEntity platform - one entity per recipient, text and title only.
- sensor.py     : Quota sensors showing monthly message limit and consumption.
- const.py      : All constants (domain, URLs, config keys, attribute names).

Custom service:
  line_ha_bot.send_message supports the full LINE message feature set: text, title,
  image, sticker, audio, video, flex card, location, button/confirm templates, and
  reply tokens. Invalid input (e.g. wrong button count, missing audio duration)
  raises a ServiceValidationError; delivery failures fire a line_bot_send_failed
  HA bus event. The service is registered once at integration level so it stays
  available across config entry reloads.

Incoming events:
  The webhook fires line_bot_message_received for each message and postback event
  from a known recipient or group. Unknown senders are captured into pending_users
  (capped at MAX_PENDING_USERS, oldest evicted) for recipient setup via the
  options flow. Captures are persisted to config entry data in one batched write
  per webhook request so they survive HA restarts.

Webhook design:
  A permanent HomeAssistantView is registered at LINE_WEBHOOK_PATH on HA startup
  (via async_setup) and also defensively in async_setup_entry. The view handles:
    - Malformed JSON or non-dict payloads: reject with HTTP 400.
    - Empty events array: LINE's Verify button health check - return 200 immediately.
    - All-zeros reply token: LINE's internal test event - return 200 immediately.
    - No loaded config entry yet: return 200 to keep LINE happy during initial setup.
    - Known recipients (user or group): verify signature, fire line_bot_message_received.
    - Unknown senders: verify signature, capture to pending_users for options flow.
    - Multi-person "room" chats: not supported, ignored at debug level.

Group support:
  Group events use groupId as the recipient lookup key. The bot must be a member
  of the group. All messages in a registered group fire line_bot_message_received
  regardless of sender. The user_id field may be None when the sending member has
  not consented to sharing their LINE ID with the bot.

Signature verification:
  Every real event from LINE is signed with HMAC-SHA256 using the channel secret.
  The signature is base64-encoded and sent in the X-Line-Signature header. We
  verify this before processing any event. Requests that fail verification are
  rejected with HTTP 400.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field

import voluptuous as vol
from aiohttp.web import Request, Response
from aiohttp.web_exceptions import HTTPBadRequest

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .api import async_get_group_name, async_get_profile_name, async_send_line_message
from .const import (
    DOMAIN,
    CONF_CHANNEL_ACCESS_TOKEN,
    CONF_CHANNEL_SECRET,
    ATTR_IMAGE_URL,
    ATTR_STICKER_PACKAGE_ID,
    ATTR_STICKER_ID,
    ATTR_REPLY_TOKEN,
    ATTR_FLEX_MESSAGE,
    ATTR_FLEX_ALT_TEXT,
    ATTR_LOCATION_TITLE,
    ATTR_LOCATION_ADDRESS,
    ATTR_LOCATION_LATITUDE,
    ATTR_LOCATION_LONGITUDE,
    ATTR_TEMPLATE_TYPE,
    ATTR_TEMPLATE_TITLE,
    ATTR_TEMPLATE_DEFAULT_URL,
    ATTR_BUTTONS,
    ATTR_QUICK_REPLIES,
    ATTR_AUDIO_URL,
    ATTR_AUDIO_DURATION,
    ATTR_VIDEO_URL,
    ATTR_VIDEO_PREVIEW_URL,
    LINE_CONTENT_URL,
    LINE_PUSH_URL,
    LINE_REPLY_URL,
    LINE_TEST_REPLY_TOKEN,
    LINE_WEBHOOK_PATH,
    MAX_PENDING_USERS,
    PENDING_USERS_KEY,
    RECIPIENTS_KEY,
    EVENT_MESSAGE_RECEIVED,
    SERVICE_SEND_MESSAGE,
    KEY_VIEW_REGISTERED,
    DEFAULT_FLEX_ALT_TEXT,
    DEFAULT_LOCATION_TITLE,
    DEFAULT_ACTION_LABEL,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NOTIFY, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# LINE API hard limits enforced before sending
MAX_QUICK_REPLIES = 13
MAX_TEMPLATE_BUTTONS = 4
CONFIRM_TEMPLATE_BUTTONS = 2

_BUTTON_SCHEMA = vol.Schema(
    {
        vol.Required("label"): cv.string,
        vol.Required("action"): vol.In(["message", "postback", "uri"]),
        vol.Required("data"): cv.string,
        vol.Optional("display_text"): cv.string,
    }
)

SEND_MESSAGE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Optional("message"): cv.string,
        vol.Optional("title"): cv.string,
        vol.Optional(ATTR_IMAGE_URL): cv.string,
        vol.Optional(ATTR_STICKER_PACKAGE_ID): vol.Coerce(str),
        vol.Optional(ATTR_STICKER_ID): vol.Coerce(str),
        vol.Optional(ATTR_REPLY_TOKEN): cv.string,
        vol.Optional(ATTR_FLEX_MESSAGE): dict,
        vol.Optional(ATTR_FLEX_ALT_TEXT): cv.string,
        vol.Optional(ATTR_LOCATION_TITLE): cv.string,
        vol.Optional(ATTR_LOCATION_ADDRESS): cv.string,
        vol.Optional(ATTR_LOCATION_LATITUDE): vol.All(
            vol.Coerce(float), vol.Range(min=-90, max=90)
        ),
        vol.Optional(ATTR_LOCATION_LONGITUDE): vol.All(
            vol.Coerce(float), vol.Range(min=-180, max=180)
        ),
        vol.Optional(ATTR_TEMPLATE_TYPE): vol.In(["buttons", "confirm"]),
        vol.Optional(ATTR_TEMPLATE_TITLE): cv.string,
        vol.Optional(ATTR_TEMPLATE_DEFAULT_URL): cv.string,
        vol.Optional(ATTR_BUTTONS): [_BUTTON_SCHEMA],
        vol.Optional(ATTR_AUDIO_URL): cv.string,
        vol.Optional(ATTR_AUDIO_DURATION): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_VIDEO_URL): cv.string,
        vol.Optional(ATTR_VIDEO_PREVIEW_URL): cv.string,
        vol.Optional(ATTR_QUICK_REPLIES): [_BUTTON_SCHEMA],
    }
)


@dataclass
class LineBotRuntimeData:
    """Per-entry runtime state stored on entry.runtime_data.

    pending_users mirrors the PENDING_USERS_KEY dict in config entry data;
    the webhook mutates this live dict and persists it in batched writes.
    pending_event wakes the options flow spinner as soon as a capture lands.
    """

    pending_users: dict[str, str]
    config_snapshot: dict
    pending_event: asyncio.Event = field(default_factory=asyncio.Event)


LineBotConfigEntry = ConfigEntry  # runtime_data holds LineBotRuntimeData


def _async_register_webhook(hass: HomeAssistant) -> None:
    """Register the permanent webhook view exactly once per HA run."""
    hass.data.setdefault(DOMAIN, {})
    if not hass.data[DOMAIN].get(KEY_VIEW_REGISTERED):
        hass.http.register_view(LineMessagingWebhookView(hass))
        hass.data[DOMAIN][KEY_VIEW_REGISTERED] = True
        _LOGGER.debug("LINE Bot webhook registered at %s", LINE_WEBHOOK_PATH)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the LINE Bot integration.

    Runs once when the integration is first loaded. Registers the webhook view
    so it is available immediately, and registers the line_ha_bot.send_message
    service at integration level so it survives config entry reloads without a
    window where the service is missing.
    """
    _async_register_webhook(hass)

    async def _handle_send_message(call: ServiceCall) -> None:
        await _async_send_message_service(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_MESSAGE,
        _handle_send_message,
        schema=SEND_MESSAGE_SCHEMA,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up LINE Bot from a config entry.

    Called once per config entry on HA startup (or after a reload). Registers
    the webhook view if not already registered (defensive, in case async_setup
    was not called), initialises runtime data (loading any persisted
    pending_users from config entry data), sets up the notify and sensor
    platforms, and registers an update listener to reload on options changes.
    """
    _async_register_webhook(hass)

    # pending_users holds LINE user and group IDs captured by the webhook that
    # have not yet been confirmed as recipients. Loaded from config entry data
    # so captures survive HA restarts. config_snapshot is used by the update
    # listener to detect whether a reload is actually needed (vs a
    # pending_users-only write).
    entry.runtime_data = LineBotRuntimeData(
        pending_users=dict(entry.data.get(PENDING_USERS_KEY, {})),
        config_snapshot={
            CONF_CHANNEL_ACCESS_TOKEN: entry.data.get(CONF_CHANNEL_ACCESS_TOKEN),
            CONF_CHANNEL_SECRET: entry.data.get(CONF_CHANNEL_SECRET),
            RECIPIENTS_KEY: dict(entry.data.get(RECIPIENTS_KEY, {})),
        },
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a LINE Bot config entry.

    Tears down the notify and sensor platforms. The webhook view and the
    send_message service are intentionally left registered: HA does not
    support unregistering HTTP views at runtime, and the service raises a
    clear error when no entry is loaded.
    """
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when credentials or recipients change.

    Triggered whenever the config entry data is changed. Compares the new entry
    data against the snapshot taken at setup time. If only pending_users changed,
    skips the reload since pending_users does not affect platforms or the service.
    A full reload is only triggered when the token, secret, or recipients change.
    """
    runtime = getattr(entry, "runtime_data", None)
    snapshot = runtime.config_snapshot if runtime else {}
    if (
        entry.data.get(CONF_CHANNEL_ACCESS_TOKEN) == snapshot.get(CONF_CHANNEL_ACCESS_TOKEN)
        and entry.data.get(CONF_CHANNEL_SECRET) == snapshot.get(CONF_CHANNEL_SECRET)
        and entry.data.get(RECIPIENTS_KEY) == snapshot.get(RECIPIENTS_KEY)
    ):
        _LOGGER.debug("LINE Bot: config unchanged (pending_users write only), skipping reload")
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _build_action(item: dict) -> dict:
    """Build a LINE action object from a button or quick reply dict."""
    action_type = item.get("action", "message")
    if action_type == "uri":
        return {"type": "uri", "label": item["label"], "uri": item["data"]}
    if action_type == "postback":
        return {
            "type": "postback",
            "label": item["label"],
            "data": item["data"],
            "displayText": item.get("display_text", item["label"]),
        }
    return {"type": "message", "label": item["label"], "text": item["data"]}


def _build_messages(data: dict) -> list[dict]:
    """Build the LINE messages array from validated service call data.

    Message type priority order:
      1. template (buttons or confirm) - if template_type is set
      2. flex - if flex_message is set
      3. text only, then one of: image, location, audio, video, sticker

    Raises ServiceValidationError for invalid input combinations.
    """
    message = data.get("message")
    title = data.get("title")
    image_url = data.get(ATTR_IMAGE_URL)
    flex_alt_text = data.get(ATTR_FLEX_ALT_TEXT, DEFAULT_FLEX_ALT_TEXT)
    template_type = data.get(ATTR_TEMPLATE_TYPE)
    buttons = data.get(ATTR_BUTTONS, [])
    quick_replies = data.get(ATTR_QUICK_REPLIES, [])

    messages: list[dict] = []

    if template_type:
        if template_type == "confirm" and len(buttons) != CONFIRM_TEMPLATE_BUTTONS:
            raise ServiceValidationError(
                "A confirm template requires exactly 2 buttons"
            )
        if template_type == "buttons" and not 1 <= len(buttons) <= MAX_TEMPLATE_BUTTONS:
            raise ServiceValidationError(
                "A buttons template requires between 1 and 4 buttons"
            )
        built_buttons = [_build_action(btn) for btn in buttons]

        if template_type == "confirm":
            template = {
                "type": "confirm",
                "text": message or "",
                "actions": built_buttons,
            }
        else:
            template = {
                "type": "buttons",
                "text": message or "",
                "actions": built_buttons,
            }
            if data.get(ATTR_TEMPLATE_TITLE):
                template["title"] = data[ATTR_TEMPLATE_TITLE]
            if image_url:
                template["thumbnailImageUrl"] = image_url
            if data.get(ATTR_TEMPLATE_DEFAULT_URL):
                template["defaultAction"] = {
                    "type": "uri",
                    "label": DEFAULT_ACTION_LABEL,
                    "uri": data[ATTR_TEMPLATE_DEFAULT_URL],
                }

        messages.append({
            "type": "template",
            "altText": flex_alt_text,
            "template": template,
        })

    elif data.get(ATTR_FLEX_MESSAGE):
        if message:
            text = f"{title}\n{message}" if title else message
            messages.append({"type": "text", "text": text})
        messages.append({
            "type": "flex",
            "altText": flex_alt_text,
            "contents": data[ATTR_FLEX_MESSAGE],
        })
    else:
        if message:
            text = f"{title}\n{message}" if title else message
            messages.append({"type": "text", "text": text})
        if image_url:
            messages.append({
                "type": "image",
                "originalContentUrl": image_url,
                "previewImageUrl": image_url,
            })
        elif (
            data.get(ATTR_LOCATION_LATITUDE) is not None
            and data.get(ATTR_LOCATION_LONGITUDE) is not None
        ):
            messages.append({
                "type": "location",
                "title": data.get(ATTR_LOCATION_TITLE) or DEFAULT_LOCATION_TITLE,
                "address": data.get(ATTR_LOCATION_ADDRESS) or "",
                "latitude": data[ATTR_LOCATION_LATITUDE],
                "longitude": data[ATTR_LOCATION_LONGITUDE],
            })
        elif data.get(ATTR_AUDIO_URL):
            if not data.get(ATTR_AUDIO_DURATION):
                raise ServiceValidationError(
                    "audio_duration is required when sending audio "
                    "and must be a positive integer in milliseconds"
                )
            messages.append({
                "type": "audio",
                "originalContentUrl": data[ATTR_AUDIO_URL],
                "duration": data[ATTR_AUDIO_DURATION],
            })
        elif data.get(ATTR_VIDEO_URL):
            messages.append({
                "type": "video",
                "originalContentUrl": data[ATTR_VIDEO_URL],
                "previewImageUrl": data.get(ATTR_VIDEO_PREVIEW_URL) or "",
            })
        elif data.get(ATTR_STICKER_PACKAGE_ID) and data.get(ATTR_STICKER_ID):
            messages.append({
                "type": "sticker",
                "packageId": data[ATTR_STICKER_PACKAGE_ID],
                "stickerId": data[ATTR_STICKER_ID],
            })

    if not messages:
        raise ServiceValidationError("No message content provided")

    # Attach quick replies to the last message object if provided.
    # Quick reply chips appear above the LINE keyboard after the message
    # and disappear once tapped. Supported on all message types.
    if quick_replies:
        if len(quick_replies) > MAX_QUICK_REPLIES:
            raise ServiceValidationError("A maximum of 13 quick replies is allowed")
        messages[-1]["quickReply"] = {
            "items": [
                {"type": "action", "action": _build_action(qr)}
                for qr in quick_replies
            ]
        }

    return messages


async def _async_send_message_service(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the line_ha_bot.send_message service call.

    For reply token sends, uses the free Reply API for the first target only;
    the reply is delivered to the chat the token came from. The token is
    single-use, so it is consumed by the first attempt whether or not it
    succeeds. On reply failure the same target falls back to the Push API,
    and all remaining targets use the Push API as normal.
    """
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            "LINE Bot is not set up or is currently reloading"
        )
    entry = entries[0]

    messages = _build_messages(call.data)

    # Map this entry's notify entity_ids to LINE user/group IDs via the
    # unique_id (DOMAIN_<line_id>) in the entity registry.
    registry = er.async_get(hass)
    prefix = f"{DOMAIN}_"
    eid_to_user_id = {
        e.entity_id: e.unique_id[len(prefix):]
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.domain == Platform.NOTIFY
        and e.unique_id
        and e.unique_id.startswith(prefix)
    }

    token = entry.data.get(CONF_CHANNEL_ACCESS_TOKEN, "")
    user_id_to_name = {
        r["user_id"]: name
        for name, r in entry.data.get(RECIPIENTS_KEY, {}).items()
    }

    use_reply_token = call.data.get(ATTR_REPLY_TOKEN)
    for eid in call.data["entity_id"]:
        user_id = eid_to_user_id.get(eid)
        if not user_id:
            _LOGGER.error(
                "LINE Bot send_message: could not resolve entity_id %s to a LINE user ID",
                eid,
            )
            continue
        recipient_name = user_id_to_name.get(user_id, eid)

        if use_reply_token:
            reply_token = use_reply_token
            use_reply_token = None  # single-use: consumed whether it succeeds or not
            if await async_send_line_message(
                hass,
                token,
                LINE_REPLY_URL,
                {"replyToken": reply_token, "messages": messages},
                entity_id=eid,
                recipient_name=recipient_name,
                is_reply=True,
            ):
                continue
            _LOGGER.warning(
                "LINE Bot: reply failed for %s, falling back to push", eid
            )

        await async_send_line_message(
            hass,
            token,
            LINE_PUSH_URL,
            {"to": user_id, "messages": messages},
            entity_id=eid,
            recipient_name=recipient_name,
        )


class LineMessagingWebhookView(HomeAssistantView):
    """Permanent HTTP view that receives webhook events from the LINE Platform.

    Registered at LINE_WEBHOOK_PATH (/api/line_ha_bot/webhook). LINE must be
    configured to POST events to this URL via the LINE Developers Console.

    All real events are signature-verified before processing. Two special cases
    bypass entry and signature checks:
      1. Empty events array: LINE's Verify button health check - return 200.
      2. All-zeros reply token: LINE's internal test event - return 200.

    For known recipients (user or group):
      - message events fire line_bot_message_received with full payload including
        content_url and message_id for media types (image, video, audio, file).
      - postback events fire line_bot_message_received with postback_data.
      - Other event types (follow, unfollow, join, leave) are silently ignored.

    For unknown senders, the user ID or group ID is captured into the runtime
    pending_users dict (capped at MAX_PENDING_USERS, oldest evicted) so the
    options flow can present them as recipient candidates. Captures are
    persisted to config entry data once per request.
    """

    url = LINE_WEBHOOK_PATH
    name = "api:line_ha_bot:webhook"
    requires_auth = False  # Must be False - LINE cannot authenticate with HA credentials

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the view with a reference to hass."""
        self.hass = hass

    async def post(self, request: Request) -> Response:
        """Handle an incoming POST from the LINE Platform."""
        body = await request.read()
        signature = request.headers.get("X-Line-Signature", "")

        _LOGGER.debug(
            "LINE Bot webhook received: %d bytes, signature present: %s",
            len(body),
            bool(signature),
        )

        try:
            data = json.loads(body)
        except ValueError:
            _LOGGER.warning("LINE Bot webhook: could not parse JSON body")
            raise HTTPBadRequest from None

        if not isinstance(data, dict):
            _LOGGER.warning("LINE Bot webhook: unexpected JSON payload type")
            raise HTTPBadRequest

        events = data.get("events", [])
        if not isinstance(events, list) or not all(
            isinstance(event, dict) for event in events
        ):
            _LOGGER.warning("LINE Bot webhook: malformed events array")
            raise HTTPBadRequest

        # Empty events array = LINE's Verify button health check. Return 200 immediately.
        # This also fires before any config entry exists, so we must not look one up.
        if not events:
            _LOGGER.debug("LINE Bot webhook: empty events (Verify request), returning 200")
            return Response(text="OK", status=200)

        # All-zeros reply token = LINE internal test event. Return 200 immediately.
        if all(e.get("replyToken") == LINE_TEST_REPLY_TOKEN for e in events):
            _LOGGER.debug("LINE Bot webhook: test reply token event, returning 200")
            return Response(text="OK", status=200)

        entries = self.hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            # No loaded config entry yet (initial setup or mid-reload).
            # Return 200 to keep LINE happy.
            _LOGGER.debug("LINE Bot webhook: no loaded config entry, returning 200")
            return Response(text="OK", status=200)
        entry = entries[0]
        runtime: LineBotRuntimeData = entry.runtime_data

        channel_secret = entry.data.get(CONF_CHANNEL_SECRET, "")
        token = entry.data.get(CONF_CHANNEL_ACCESS_TOKEN, "")

        if not self._verify_signature(channel_secret, body, signature):
            _LOGGER.warning(
                "LINE Bot webhook signature verification failed. "
                "Check that your channel secret is correct."
            )
            raise HTTPBadRequest

        pending = runtime.pending_users
        _LOGGER.debug(
            "LINE Bot webhook: processing %d events, current pending count: %d",
            len(events), len(pending)
        )

        recipients = entry.data.get(RECIPIENTS_KEY, {})
        user_id_to_recipient = {
            r["user_id"]: {"name": name, "display_name": r.get("display_name", "")}
            for name, r in recipients.items()
        }
        registry = er.async_get(self.hass)
        prefix = f"{DOMAIN}_"
        user_id_to_entity_id = {
            e.unique_id[len(prefix):]: e.entity_id
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.domain == Platform.NOTIFY
            and e.unique_id
            and e.unique_id.startswith(prefix)
        }

        captured = False
        for event in events:
            if event.get("replyToken") == LINE_TEST_REPLY_TOKEN:
                _LOGGER.debug("LINE Bot webhook: skipping test event")
                continue

            source = event.get("source")
            if not isinstance(source, dict):
                continue
            source_type = source.get("type", "user")
            user_id = source.get("userId")
            group_id = source.get("groupId") if source_type == "group" else None

            if source_type == "room":
                # Multi-person "room" chats are not supported.
                _LOGGER.debug("LINE Bot webhook: ignoring unsupported room event")
                continue
            if source_type == "group" and not group_id:
                continue
            # userId may legitimately be absent in group events when the member
            # has not consented to ID sharing; only require it for user sources.
            if source_type != "group" and not user_id:
                continue

            event_type = event.get("type", "")
            try:
                timestamp = int(event.get("timestamp", 0)) // 1000
            except (TypeError, ValueError):
                timestamp = 0

            # For group events match on group_id; for user events match on user_id
            lookup_id = group_id or user_id
            if lookup_id in user_id_to_recipient:
                recipient = user_id_to_recipient[lookup_id]
                reply_token = event.get("replyToken")
                if event_type == "message":
                    msg = event.get("message")
                    if not isinstance(msg, dict):
                        msg = {}
                    msg_type = msg.get("type", "")
                    message_id = msg.get("id")
                    has_content = msg_type in ("image", "video", "audio", "file")
                    content_url = (
                        LINE_CONTENT_URL.format(message_id=message_id)
                        if has_content and message_id else None
                    )
                    self.hass.bus.async_fire(
                        EVENT_MESSAGE_RECEIVED,
                        {
                            "type": msg_type,
                            "user_id": user_id,
                            "group_id": group_id,
                            "entity_id": user_id_to_entity_id.get(lookup_id),
                            "recipient_name": recipient["name"],
                            "display_name": recipient["display_name"],
                            "message_text": msg.get("text") if msg_type == "text" else None,
                            "message_id": message_id,
                            "content_url": content_url,
                            "postback_data": None,
                            "reply_token": reply_token,
                            "timestamp": timestamp,
                        },
                    )
                    _LOGGER.debug(
                        "LINE Bot webhook: fired line_bot_message_received for %s (%s)",
                        recipient["name"],
                        msg_type,
                    )
                elif event_type == "postback":
                    self.hass.bus.async_fire(
                        EVENT_MESSAGE_RECEIVED,
                        {
                            "type": "postback",
                            "user_id": user_id,
                            "group_id": group_id,
                            "entity_id": user_id_to_entity_id.get(lookup_id),
                            "recipient_name": recipient["name"],
                            "display_name": recipient["display_name"],
                            "message_text": None,
                            "message_id": None,
                            "content_url": None,
                            "postback_data": event.get("postback", {}).get("data"),
                            "reply_token": reply_token,
                            "timestamp": timestamp,
                        },
                    )
                    _LOGGER.debug(
                        "LINE Bot webhook: fired line_bot_message_received (postback) for %s",
                        recipient["name"],
                    )
                else:
                    _LOGGER.debug(
                        "LINE Bot webhook: ignoring event type '%s' for known recipient %s",
                        event_type,
                        lookup_id,
                    )
                continue

            if lookup_id in pending:
                _LOGGER.debug(
                    "LINE Bot webhook: %s %s already in pending", source_type, lookup_id
                )
                continue

            if source_type == "group":
                _LOGGER.debug("LINE Bot webhook: capturing group %s", lookup_id)
                display_name = await async_get_group_name(self.hass, token, lookup_id)
            else:
                _LOGGER.debug("LINE Bot webhook: capturing user %s", lookup_id)
                display_name = await async_get_profile_name(self.hass, token, lookup_id)
            pending[lookup_id] = display_name or lookup_id
            # Cap pending captures; evict the oldest beyond MAX_PENDING_USERS.
            while len(pending) > MAX_PENDING_USERS:
                evicted = next(iter(pending))
                pending.pop(evicted)
                _LOGGER.debug("LINE Bot webhook: evicted oldest pending %s", evicted)
            runtime.pending_event.set()
            captured = True
            _LOGGER.debug(
                "LINE Bot webhook: captured %s %s (%s)",
                source_type,
                lookup_id,
                pending[lookup_id],
            )

        if captured:
            # Persist pending_users to config entry data in one write per request
            # so captures survive HA restarts. This writes only pending_users;
            # the update listener detects this and skips the reload.
            new_data = dict(entry.data)
            new_data[PENDING_USERS_KEY] = dict(pending)
            self.hass.config_entries.async_update_entry(entry, data=new_data)

        return Response(text="OK", status=200)

    def _verify_signature(self, secret: str, body: bytes, signature: str) -> bool:
        """Verify the X-Line-Signature header against the request body.

        LINE signs each webhook request body using HMAC-SHA256 with the channel
        secret as the key, then base64-encodes the result. We compute the same
        value and compare using hmac.compare_digest to prevent timing attacks.

        Returns True if the signature is valid, False otherwise.
        """
        if not signature or not secret:
            _LOGGER.warning(
                "LINE Bot webhook: missing signature (%s) or secret (%s)",
                bool(signature),
                bool(secret),
            )
            return False
        expected = hmac.new(
            secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected_b64 = base64.b64encode(expected).decode("utf-8")
        return hmac.compare_digest(expected_b64, signature)
