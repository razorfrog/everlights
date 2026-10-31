"""Shared device info: one device per bridge."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EverLightsCoordinator


class EverLightsEntity(CoordinatorEntity[EverLightsCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: EverLightsCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.serial}_{key}"
        bridge = (coordinator.data or {}).get("bridge") or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.serial)},
            name="EverLights",
            manufacturer="EverLights",
            model="Bridge",
            sw_version=bridge.get("firmware"),
            configuration_url=coordinator.api.url + "/admin",
        )
