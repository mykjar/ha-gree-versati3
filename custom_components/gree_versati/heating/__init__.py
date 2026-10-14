"""Optional floor heating control for the Gree Versati integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..constants import STATUS_POLL_KEYS
from ..coordinator import GreeVersatiCoordinator
from .card import async_register_card
from .const import CONF_HEATING_ENABLED, STORAGE_VERSION, storage_key
from .controller import HeatingController

_LOGGER = logging.getLogger(__name__)


def heating_poll_groups(options: Mapping[str, Any]) -> tuple[tuple[str, ...], ...]:
    """Extra pump values to poll, only when floor heating control is enabled."""
    return (STATUS_POLL_KEYS,) if options.get(CONF_HEATING_ENABLED) else ()


async def async_setup_heating(
    hass: HomeAssistant,
    entry: ConfigEntry,
    pump: GreeVersatiCoordinator,
) -> HeatingController | None:
    """Start floor heating control if it is enabled in the options."""
    if not entry.options.get(CONF_HEATING_ENABLED):
        return None
    try:
        controller = HeatingController(hass, entry, pump)
        await controller.async_load()
        await controller.async_refresh()
    except Exception:  # Never take the pump's own entities down with it.
        _LOGGER.exception("Floor heating control failed to start and stays off")
        return None
    async_register_card(hass)
    return controller


async def async_remove_heating(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the controller's saved state when the device is removed."""
    await Store[dict[str, Any]](hass, STORAGE_VERSION, storage_key(entry.entry_id)).async_remove()
