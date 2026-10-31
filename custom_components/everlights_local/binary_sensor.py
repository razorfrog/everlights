"""Is the bridge answering the app, and is it reaching the EverLights cloud."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EverLightsConfigEntry
from .entity import EverLightsEntity


async def async_setup_entry(hass: HomeAssistant, entry: EverLightsConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    c = entry.runtime_data
    async_add_entities([BridgeOnline(c), BridgeCloud(c)])


class BridgeOnline(EverLightsEntity, BinarySensorEntity):
    _attr_translation_key = "bridge_online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "bridge_online")

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        return "bridge" in data and not data.get("bridgeError")

    @property
    def extra_state_attributes(self) -> dict:
        err = (self.coordinator.data or {}).get("bridgeError")
        return {"error": err} if err else {}


class BridgeCloud(EverLightsEntity, BinarySensorEntity):
    _attr_translation_key = "bridge_cloud"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "bridge_cloud")

    @property
    def is_on(self) -> bool | None:
        return ((self.coordinator.data or {}).get("bridge") or {}).get("cloud")
