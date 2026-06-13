"""LINE Bot sensor platform for Home Assistant.

Provides two diagnostic sensors per config entry:
  - Monthly message limit (from LINE plan, rarely changes)
  - Monthly message consumption (messages sent so far this month)

Both sensors poll the LINE Messaging API once per hour via a shared
DataUpdateCoordinator. Plans with no limit (quota type "none") report
no limit value rather than 0.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DOMAIN,
    CONF_CHANNEL_ACCESS_TOKEN,
    LINE_QUOTA_URL,
    LINE_QUOTA_CONSUMPTION_URL,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(hours=1)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LINE Bot quota sensors from a config entry."""
    coordinator = LineQuotaCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    async_add_entities([
        LineQuotaLimitSensor(coordinator, entry),
        LineQuotaConsumptionSensor(coordinator, entry),
    ])


class LineQuotaCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches LINE quota and consumption once per hour."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="LINE Bot quota",
            update_interval=SCAN_INTERVAL,
        )
        self._token = entry.data[CONF_CHANNEL_ACCESS_TOKEN]

    async def _fetch_json(self, url: str, label: str) -> dict:
        """GET one LINE API endpoint, raising UpdateFailed on a non-200 status."""
        session = async_get_clientsession(self.hass)
        headers = {"Authorization": f"Bearer {self._token}"}
        async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                raise UpdateFailed(f"LINE {label} API returned HTTP {resp.status}")
            return await resp.json()

    async def _async_update_data(self) -> dict:
        """Fetch quota and consumption from the LINE API in parallel."""
        try:
            quota_data, consumption_data = await asyncio.gather(
                self._fetch_json(LINE_QUOTA_URL, "quota"),
                self._fetch_json(LINE_QUOTA_CONSUMPTION_URL, "consumption"),
            )
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"LINE Bot quota fetch error: {err}") from err

        # Quota type "none" means the plan has no limit; "value" is then absent.
        return {
            "limit": quota_data.get("value"),
            "type": quota_data.get("type", "unknown"),
            "consumption": consumption_data.get("totalUsage", 0),
        }


class LineQuotaLimitSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing the monthly message limit for the LINE channel."""

    _attr_icon = "mdi:message-badge-outline"
    _attr_native_unit_of_measurement = "messages"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LineQuotaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Monthly message limit"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_quota_limit"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LINE HA Bot",
            manufacturer="sfox38",
            model="Messaging API",
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("limit")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        return {"plan_type": self.coordinator.data.get("type")}


class LineQuotaConsumptionSensor(CoordinatorEntity, SensorEntity):
    """Sensor showing messages sent so far this month."""

    _attr_icon = "mdi:message-text-clock-outline"
    _attr_native_unit_of_measurement = "messages"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LineQuotaCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "Monthly message consumption"
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_quota_consumption"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LINE HA Bot",
            manufacturer="sfox38",
            model="Messaging API",
        )

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("consumption")

    @property
    def extra_state_attributes(self) -> dict:
        if self.coordinator.data is None:
            return {}
        limit = self.coordinator.data.get("limit")
        consumption = self.coordinator.data.get("consumption", 0)
        # remaining is None on plans with no limit (quota type "none")
        remaining = max(0, limit - consumption) if limit else None
        return {"remaining": remaining}
