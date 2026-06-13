"""LINE Bot notify platform for Home Assistant.

This module implements the HA notify entity platform. One LineMessagingNotifyEntity
is created per configured recipient. Each entity exposes the standard notify.send_message
action supporting message and title only.

For richer messages (images, stickers, flex cards, locations, reply tokens) use the
line_ha_bot.send_message service instead.

Example automation action:
  action: notify.send_message
  target:
    entity_id: notify.line_bot_david
  data:
    message: "Front door opened"
    title: "Security Alert"
"""

from __future__ import annotations

import logging

from homeassistant.components.notify import NotifyEntity, NotifyEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from .api import async_send_line_message
from .const import (
    DOMAIN,
    CONF_CHANNEL_ACCESS_TOKEN,
    LINE_PUSH_URL,
    RECIPIENTS_KEY,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LINE Bot notify entities from a config entry.

    Creates one LineMessagingNotifyEntity per recipient stored in the config
    entry. Also removes any stale notify entities from previous loads whose
    LINE user IDs are no longer in the recipients dict (e.g. after a recipient
    is removed via the options flow). Only notify entities are considered;
    the quota sensors share the same unique_id prefix and must not be touched.
    Called by HA when the entry is loaded or reloaded.
    """
    token = entry.data[CONF_CHANNEL_ACCESS_TOKEN]
    recipients = entry.data.get(RECIPIENTS_KEY, {})

    # Remove stale notify entities from the registry whose user IDs are no
    # longer in the recipients dict. Each entity has a unique_id of
    # DOMAIN_<user_id>.
    registry = er.async_get(hass)
    current_user_ids = {r["user_id"] for r in recipients.values()}
    prefix = f"{DOMAIN}_"
    for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity_entry.domain != Platform.NOTIFY:
            continue
        unique_id = entity_entry.unique_id or ""
        if unique_id.startswith(prefix):
            user_id = unique_id[len(prefix):]
            if user_id not in current_user_ids:
                registry.async_remove(entity_entry.entity_id)

    entities = [
        LineMessagingNotifyEntity(
            hass, entry, name, r.get("friendly_name", name), r["user_id"], token
        )
        for name, r in recipients.items()
    ]
    async_add_entities(entities)


class LineMessagingNotifyEntity(NotifyEntity):
    """A notify entity representing a single LINE recipient (user or group).

    Entity ID format:  notify.line_bot_<recipient_name_slugified>
    Unique ID format:  line_ha_bot_<line_user_id_or_group_id>
    Display name:      friendly_name (may contain emoji and unicode)

    The unique ID is based on the LINE ID (not the recipient name) so that
    renaming a recipient does not create a duplicate entity. The entity ID is
    suggested from the validated ASCII recipient name; the friendly_name is
    set as _attr_name so the HA UI shows the LINE display name rather than
    the ASCII entity name. For entities already in the registry, the
    registered entity ID always wins.
    """

    _attr_has_entity_name = True
    _attr_supported_features = NotifyEntityFeature.TITLE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        recipient_name: str,
        friendly_name: str,
        user_id: str,
        token: str,
    ) -> None:
        """Initialise the notify entity."""
        self.hass = hass
        self._recipient_name = recipient_name
        self._user_id = user_id
        self._token = token
        self._attr_name = friendly_name
        self._attr_unique_id = f"{DOMAIN}_{user_id}"
        # Suggest the entity ID from the validated ASCII recipient name so it
        # is stable and predictable (notify.line_bot_<name>) regardless of the
        # unicode display name.
        self.entity_id = f"{Platform.NOTIFY}.line_bot_{slugify(recipient_name)}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LINE HA Bot",
            manufacturer="sfox38",
            model="Messaging API",
        )

    async def async_send_message(
        self,
        message: str,
        title: str | None = None,
        **kwargs,
    ) -> None:
        """Send a plain text message to this LINE recipient via the Push API.

        Supports message and optional title only. For richer messages
        (images, audio, video, stickers, flex cards, locations, templates,
        reply tokens) use the line_ha_bot.send_message service instead.
        Fires line_bot_send_failed on any error.
        """
        text = f"{title}\n{message}" if title else message
        payload = {
            "to": self._user_id,
            "messages": [{"type": "text", "text": text}],
        }
        await async_send_line_message(
            self.hass,
            self._token,
            LINE_PUSH_URL,
            payload,
            entity_id=self.entity_id,
            recipient_name=self._recipient_name,
        )
