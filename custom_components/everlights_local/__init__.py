"""EverLights Local: Home Assistant entities for the everlights app."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EverLightsApi
from .const import CONF_TOKEN
from .coordinator import EverLightsCoordinator

PLATFORMS = [Platform.BINARY_SENSOR, Platform.LIGHT, Platform.SENSOR]

type EverLightsConfigEntry = ConfigEntry[EverLightsCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EverLightsConfigEntry) -> bool:
    api = EverLightsApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_TOKEN))
    coordinator = EverLightsCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EverLightsConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
