"""The roofline as one light: on/off, the app's patterns as effects, or a solid color."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_EFFECT, ATTR_RGB_COLOR, ColorMode, LightEntity, LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EverLightsConfigEntry
from .api import EverLightsError
from .entity import EverLightsEntity


async def async_setup_entry(hass: HomeAssistant, entry: EverLightsConfigEntry,
                            async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([EverLightsLight(entry.runtime_data)])


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)


class EverLightsLight(EverLightsEntity, LightEntity):
    _attr_name = None
    _attr_translation_key = "roofline"
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "light")

    @property
    def _current(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("current") or {}

    @property
    def available(self) -> bool:
        return super().available and "current" in (self.coordinator.data or {})

    @property
    def is_on(self) -> bool:
        return bool(self._current.get("pattern"))

    @property
    def effect_list(self) -> list[str]:
        return sorted({p["alias"] for p in self.coordinator.patterns if p.get("alias")}, key=str.lower)

    @property
    def effect(self) -> str | None:
        return (self._current.get("match") or {}).get("alias")

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        pattern = self._current.get("pattern") or []
        return _rgb(pattern[0]) if pattern else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        api = self.coordinator.api
        try:
            if ATTR_EFFECT in kwargs:
                pattern = self.coordinator.pattern_by_alias(kwargs[ATTR_EFFECT])
                if not pattern:
                    raise ServiceValidationError(f"No pattern called {kwargs[ATTR_EFFECT]}")
                await api.play(pattern["id"])
            elif ATTR_RGB_COLOR in kwargs:
                r, g, b = kwargs[ATTR_RGB_COLOR]
                await api.play_colors([f"{r:02X}{g:02X}{b:02X}"], "Home Assistant color")
            else:
                await api.turn_on()
        except EverLightsError as err:
            raise HomeAssistantError(f"EverLights app: {err}") from err
        await self.coordinator.async_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.api.turn_off()
        except EverLightsError as err:
            raise HomeAssistantError(f"EverLights app: {err}") from err
        await self.coordinator.async_refresh()
