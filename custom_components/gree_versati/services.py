"""Service handlers for Gree Versati integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.service import async_register_admin_service

from .constants import (
    ATTR_KEY,
    ATTR_KEYS,
    ATTR_VALUE,
    CONF_DEVICE_ID,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_ENTRIES,
    DOMAIN,
    SERVICE_GET_PARAMS,
    SERVICE_SET_PARAM,
)

_LOGGER = logging.getLogger(__name__)

SET_PARAM_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KEY): str,
        vol.Required(ATTR_VALUE): vol.Any(str, int, bool, float),
        vol.Optional(CONF_DEVICE_ID): str,
    }
)

GET_PARAMS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_KEYS): [str],
        vol.Optional(CONF_DEVICE_ID): str,
    }
)

_DECIMAL = re.compile(r"[+-]?(\d+\.\d*|\.\d+)")


def _normalize_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    normalized = value.strip()
    lowered = normalized.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    try:
        return int(normalized)
    except ValueError:
        pass

    # Only plain decimals such as "35.0" or "-1.5"; "1e3", "nan" and "inf" stay text.
    if not _DECIMAL.fullmatch(normalized):
        return normalized
    number = float(normalized)
    return int(number) if number.is_integer() else number


def _matching_entries(
    hass: HomeAssistant,
    target_device_id: str | None,
) -> list[tuple[ConfigEntry, dict[str, Any]]]:
    entries_data = hass.data[DOMAIN][DATA_ENTRIES]
    results: list[tuple[ConfigEntry, dict[str, Any]]] = []

    for entry in hass.config_entries.async_entries(DOMAIN):
        if target_device_id and entry.data.get(CONF_DEVICE_ID) != target_device_id:
            continue
        runtime_data = entries_data.get(entry.entry_id)
        if runtime_data:
            results.append((entry, runtime_data))

    return results


async def async_register_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    async def async_handle_set_param(call: ServiceCall) -> None:
        key = call.data[ATTR_KEY]
        value = _normalize_value(call.data[ATTR_VALUE])
        target_device_id = call.data.get(CONF_DEVICE_ID)

        matches = _matching_entries(hass, target_device_id)
        if not matches:
            raise HomeAssistantError("No matching configured device entries found")

        errors: list[str] = []
        for entry, runtime_data in matches:
            try:
                await runtime_data[DATA_CLIENT].async_set({key: value})
                await runtime_data[DATA_COORDINATOR].async_request_refresh()
            except HomeAssistantError as err:
                errors.append(f"{entry.title}: {err}")

        if errors:
            raise HomeAssistantError("; ".join(errors))

    async def async_handle_get_params(call: ServiceCall) -> ServiceResponse:
        keys = call.data[ATTR_KEYS]
        target_device_id = call.data.get(CONF_DEVICE_ID)

        matches = _matching_entries(hass, target_device_id)
        if not matches:
            raise HomeAssistantError("No matching configured device entries found")

        results: dict[str, dict[str, Any]] = {}

        for entry, runtime_data in matches:
            try:
                data = await runtime_data[DATA_CLIENT].async_get(keys)
                results[entry.data[CONF_DEVICE_ID]] = data
            except HomeAssistantError as err:
                _LOGGER.warning(
                    "Failed get_params for device_id=%s: %s",
                    entry.data[CONF_DEVICE_ID],
                    err,
                )
                results[entry.data[CONF_DEVICE_ID]] = {"error": str(err)}

        if not getattr(call, "return_response", False):
            _LOGGER.info("gree_versati.get_params results: %s", results)

        return {"devices": results}

    # Raw writes bypass the entities' ranges and floor heating control's
    # limits, so only admins (or automations with no user) may make them.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_SET_PARAM,
        async_handle_set_param,
        schema=SET_PARAM_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_PARAMS,
        async_handle_get_params,
        schema=GET_PARAMS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unregister_services(hass: HomeAssistant) -> None:
    """Remove integration services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_PARAM):
        hass.services.async_remove(DOMAIN, SERVICE_SET_PARAM)
    if hass.services.has_service(DOMAIN, SERVICE_GET_PARAMS):
        hass.services.async_remove(DOMAIN, SERVICE_GET_PARAMS)
