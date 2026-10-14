"""Button platform for Gree Versati: floor heating control actions."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .constants import CONF_DEVICE_ID, DATA_ENTRIES, DATA_HEATING
from .heating.entities import CalibrateCurveButton


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up floor heating buttons when control is enabled."""
    runtime_data = hass.data[entry.domain][DATA_ENTRIES][entry.entry_id]
    if (controller := runtime_data.get(DATA_HEATING)) is not None:
        async_add_entities([CalibrateCurveButton(controller, entry.data[CONF_DEVICE_ID])])
