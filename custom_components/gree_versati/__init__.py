"""The Gree Versati integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from .client import GreeVersatiProtocolClient
from .constants import (
    CONF_DEVICE_ID,
    CONF_KEY,
    CONF_SCAN_INTERVAL,
    CONF_TIMEOUT,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DATA_ENTRIES,
    DATA_HEATING,
    DEFAULT_RETRIES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    SERVICE_SET_PARAM,
)
from .coordinator import GreeVersatiCoordinator
from .heating import async_remove_heating, async_setup_heating, heating_poll_groups
from .services import async_register_services, async_unregister_services


GreeVersatiConfigEntry = ConfigEntry


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up integration from YAML (not used)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_ENTRIES, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GreeVersatiConfigEntry) -> bool:
    """Set up Gree Versati from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_ENTRIES, {})

    client = GreeVersatiProtocolClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        device_id=entry.data[CONF_DEVICE_ID],
        key=entry.data[CONF_KEY],
        timeout=float(entry.data[CONF_TIMEOUT]),
        retries=DEFAULT_RETRIES,
    )

    scan_interval = int(entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    coordinator = GreeVersatiCoordinator(
        hass, client, scan_interval, heating_poll_groups(entry.options)
    )

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
        DATA_HEATING: await async_setup_heating(hass, entry, coordinator),
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_PARAM):
        await async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GreeVersatiConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN][DATA_ENTRIES].pop(entry.entry_id, None)

    if not hass.data[DOMAIN][DATA_ENTRIES]:
        await async_unregister_services(hass)

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: GreeVersatiConfigEntry) -> None:
    """Reload the config entry when options change."""
    runtime_data = hass.data[DOMAIN][DATA_ENTRIES].get(entry.entry_id)
    heating = runtime_data.get(DATA_HEATING) if runtime_data else None
    if heating is not None and heating.apply_options(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: GreeVersatiConfigEntry) -> None:
    """Clean up when the device is removed."""
    await async_remove_heating(hass, entry)
