"""Set up with the app's address (and a Shortcut token if it's the remote entrance)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_URL
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import EverLightsApi, EverLightsAuthError, EverLightsError
from .const import CONF_TOKEN, DOMAIN


class EverLightsConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
            api = EverLightsApi(async_get_clientsession(self.hass), url, user_input.get(CONF_TOKEN))
            try:
                state = await api.state()
            except EverLightsAuthError:
                errors["base"] = "invalid_auth"
            except EverLightsError:
                errors["base"] = "cannot_connect"
            else:
                if "scheduler" not in state:
                    errors["base"] = "not_everlights"
                else:
                    serial = (state.get("bridge") or {}).get("serial") or url
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_configured(updates={CONF_URL: url})
                    return self.async_create_entry(
                        title="EverLights", data={**user_input, CONF_URL: url})
        schema = vol.Schema({
            vol.Required(CONF_URL, default=(user_input or {}).get(CONF_URL, "http://")): str,
            vol.Optional(CONF_TOKEN): str,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
