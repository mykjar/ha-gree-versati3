"""End-to-end tests for the measured temperature sensors."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.gree_versati.constants import DOMAIN, MEASUREMENT_POLL_KEYS

DEVICE_ID = "502cc62fc0be"

MAIN_VALUES = {"HeWatOutTemSet": 35, "Pow": 1, "Mod": 1, "AllErr": 0, "TemUn": 0}
MEASUREMENT_VALUES = {
    "AllInWatTemHi": 132,
    "AllInWatTemLo": 4,
    "AllOutWatTemHi": 132,
    "AllOutWatTemLo": 2,
    "HepOutWatTemHi": 70,  # not fitted
    "HepOutWatTemLo": 0,
    "WatBoxTemHi": 148,
    "WatBoxTemLo": 6,
    # RmoHomTem deliberately missing from the response
}


async def _setup(hass: HomeAssistant, measurements: dict[str, Any]) -> MockConfigEntry:
    async def fake_get(self: Any, keys: list[str]) -> dict[str, Any]:
        source = measurements if keys == list(MEASUREMENT_POLL_KEYS) else MAIN_VALUES
        return {key: source[key] for key in keys if key in source}

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data={
            "host": "192.0.2.10",
            "port": 7000,
            "device_id": DEVICE_ID,
            "key": "1234567890abcdef",
            "timeout": 5,
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.gree_versati.client.GreeVersatiProtocolClient.async_get",
        fake_get,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant, param: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DEVICE_ID}_{param}"
    )
    assert entity_id is not None
    return entity_id


async def test_measured_temperatures(hass: HomeAssistant) -> None:
    await _setup(hass, MEASUREMENT_VALUES)

    outlet = hass.states.get(_entity_id(hass, "AllOutWatTem"))
    assert outlet is not None
    assert outlet.state == "32.2"
    assert outlet.attributes["unit_of_measurement"] == "°C"
    assert outlet.attributes["device_class"] == "temperature"
    assert outlet.attributes["state_class"] == "measurement"

    assert hass.states.get(_entity_id(hass, "AllInWatTem")).state == "32.4"
    assert hass.states.get(_entity_id(hass, "WatBoxTem")).state == "48.6"


async def test_optional_sensors_disabled_by_default(hass: HomeAssistant) -> None:
    await _setup(hass, MEASUREMENT_VALUES)
    registry = er.async_get(hass)

    for param in ("HepOutWatTem", "RmoHomTem"):
        entry = registry.async_get(_entity_id(hass, param))
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(entry.entity_id) is None


async def test_unsupported_measurements_are_unavailable(hass: HomeAssistant) -> None:
    await _setup(hass, {})

    assert hass.states.get(_entity_id(hass, "AllOutWatTem")).state == STATE_UNAVAILABLE
    # Existing entities keep working.
    setpoint = hass.states.get(
        er.async_get(hass).async_get_entity_id(
            "number", DOMAIN, f"{DEVICE_ID}_HeWatOutTemSet"
        )
    )
    assert setpoint is not None
    assert setpoint.state == "35.0"
