"""The next scheduled change, plus radio and firmware details for troubleshooting."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EverLightsConfigEntry
from .entity import EverLightsEntity


def _zone(data: dict) -> dict:
    return (data.get("zones") or [{}])[0]


def _next(data: dict) -> dict:
    return (data.get("upcoming") or [{}])[0]


@dataclass(frozen=True, kw_only=True)
class EverLightsSensorDescription(SensorEntityDescription):
    value: Callable[[dict[str, Any]], Any]
    attrs: Callable[[dict[str, Any]], dict[str, Any]] | None = None


SENSORS = (
    EverLightsSensorDescription(
        key="next_change", translation_key="next_change", device_class=SensorDeviceClass.TIMESTAMP,
        value=lambda d: datetime.fromisoformat(_next(d)["at"]) if _next(d).get("at") else None,
        attrs=lambda d: {"action": _next(d).get("action"), "pattern": _next(d).get("name")},
    ),
    EverLightsSensorDescription(
        key="scheduled_pattern", translation_key="scheduled_pattern",
        value=lambda d: (d.get("desired") or {}).get("name") or ("off" if d.get("desired") else None),
    ),
    EverLightsSensorDescription(
        key="rssi", translation_key="rssi", device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT, entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: _zone(d).get("rssi"),
        attrs=lambda d: {"health": _zone(d).get("health")},
    ),
    EverLightsSensorDescription(
        key="bridge_restarts", translation_key="bridge_restarts", state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.get("bridge") or {}).get("reboots"),
    ),
    EverLightsSensorDescription(
        key="controller_restarts", translation_key="controller_restarts",
        state_class=SensorStateClass.TOTAL_INCREASING, entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: _zone(d).get("reboots"),
    ),
    EverLightsSensorDescription(
        key="firmware", translation_key="firmware", entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: (d.get("bridge") or {}).get("firmware"),
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: EverLightsConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities(EverLightsSensor(entry.runtime_data, d) for d in SENSORS)


class EverLightsSensor(EverLightsEntity, SensorEntity):
    entity_description: EverLightsSensorDescription

    def __init__(self, coordinator, description: EverLightsSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs
        return fn(self.coordinator.data or {}) if fn else None
