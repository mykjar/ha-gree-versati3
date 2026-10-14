"""Climate platform for Gree Versati: the optional floor heating thermostat."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .constants import CONF_DEVICE_ID, DATA_ENTRIES, DATA_HEATING
from .heating.entities import FloorHeatingClimate


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the floor heating thermostat when control is enabled."""
    runtime_data = hass.data[entry.domain][DATA_ENTRIES][entry.entry_id]
    if (controller := runtime_data.get(DATA_HEATING)) is not None:
        async_add_entities([FloorHeatingClimate(controller, entry.data[CONF_DEVICE_ID])])
