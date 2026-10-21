"""Config flow for Gree Versati integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .client import GreeVersatiClientError, GreeVersatiProtocolClient
from .constants import (
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    DEFAULT_PORT,
    DEFAULT_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN,
    PARAM_POW,
)
from .heating.const import DEFAULT_WEATHER_ENTITY
from .heating.options import OPTIONAL_ENTITIES, heating_schema, validate_heating

_LOGGER = logging.getLogger(__name__)


class GreeVersatiConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Gree Versati."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            if not GreeVersatiProtocolClient.validate_key_string(user_input[CONF_KEY]):
                errors["key"] = "invalid_key"
            else:
                await self.async_set_unique_id(user_input[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()

                client = GreeVersatiProtocolClient(
                    host=user_input[CONF_HOST],
                    port=user_input[CONF_PORT],
                    device_id=user_input[CONF_DEVICE_ID],
                    key=user_input[CONF_KEY],
                    timeout=float(user_input[CONF_TIMEOUT]),
                    retries=DEFAULT_RETRIES,
                )

                try:
                    await client.async_get([PARAM_POW])
                except GreeVersatiClientError as err:
                    _LOGGER.warning(
                        "Connection test failed for host=%s port=%s device_id=%s: %s",
                        user_input[CONF_HOST],
                        user_input[CONF_PORT],
                        user_input[CONF_DEVICE_ID],
                        err,
                    )
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"Gree Versati {user_input[CONF_DEVICE_ID]}",
                        data=user_input,
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_DEVICE_ID): str,
                vol.Required(CONF_KEY): str,
                vol.Required(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): vol.All(
                    vol.Coerce(float),
                    vol.Range(min=1),
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Get options flow handler."""
        return GreeVersatiOptionsFlow(config_entry)


class GreeVersatiOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Gree Versati."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Choose which options to change."""
        return self.async_show_menu(step_id="init", menu_options=["polling", "heating"])

    async def async_step_polling(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Manage polling."""
        if user_input is not None:
            return self._async_save(user_input)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL,
                    default=self._config_entry.options.get(
                        CONF_SCAN_INTERVAL,
                        DEFAULT_SCAN_INTERVAL,
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=5)),
            }
        )

        return self.async_show_form(step_id="polling", data_schema=schema)

    async def async_step_heating(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> config_entries.FlowResult:
        """Manage floor heating control."""
        errors: dict[str, str] = {}
        if user_input is not None:
            # A field left empty means none, not "keep the previous one".
            user_input = {**dict.fromkeys(OPTIONAL_ENTITIES, ""), **user_input}
            errors = validate_heating(user_input)
            if not errors:
                return self._async_save(user_input)

        # Offer the forecast Home Assistant sets up out of the box, if it exists.
        weather = DEFAULT_WEATHER_ENTITY if self.hass.states.get(DEFAULT_WEATHER_ENTITY) else None
        return self.async_show_form(
            step_id="heating",
            data_schema=heating_schema(
                self._config_entry.options if user_input is None else user_input, weather
            ),
            errors=errors,
        )

    @callback
    def _async_save(self, user_input: dict[str, Any]) -> config_entries.FlowResult:
        # Each step covers some of the options; keep the others.
        return self.async_create_entry(
            title="", data={**self._config_entry.options, **user_input}
        )
