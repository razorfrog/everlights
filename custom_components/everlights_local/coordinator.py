"""Polls the app for state, and now and then for the list of patterns."""

from __future__ import annotations

from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EverLightsApi, EverLightsAuthError, EverLightsError
from .const import DOMAIN, PATTERNS_REFRESH_SECONDS, SCAN_INTERVAL_SECONDS

_LOGGER = logging.getLogger(__name__)


class EverLightsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, api: EverLightsApi) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN,
                         update_interval=timedelta(seconds=SCAN_INTERVAL_SECONDS))
        self.api = api
        self.patterns: list[dict[str, Any]] = []
        self._patterns_at = 0.0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self.patterns or time.monotonic() - self._patterns_at > PATTERNS_REFRESH_SECONDS:
                self.patterns = await self.api.patterns()
                self._patterns_at = time.monotonic()
            return await self.api.state()
        except EverLightsAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except EverLightsError as err:
            raise UpdateFailed(str(err)) from err

    def pattern_by_alias(self, alias: str) -> dict[str, Any] | None:
        return next((p for p in self.patterns if p.get("alias") == alias), None)

    @property
    def serial(self) -> str:
        return ((self.data or {}).get("bridge") or {}).get("serial") or self.api.url
