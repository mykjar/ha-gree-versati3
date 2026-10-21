"""Floor heating control running inside the integration, against a fake pump."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.climate import (
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_TEMPERATURE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.gree_versati.client import (
    GreeVersatiClientError,
    GreeVersatiProtocolClient,
)
from custom_components.gree_versati.constants import (
    CONF_SCAN_INTERVAL,
    DATA_COORDINATOR,
    DATA_ENTRIES,
    DATA_HEATING,
    DOMAIN,
    PARAM_SV_ST,
)
from custom_components.gree_versati.heating.const import (
    CONF_CURVE_COLD_FLOW,
    CONF_CURVE_COLD_OUTDOOR,
    CONF_CURVE_MILD_FLOW,
    CONF_CURVE_MILD_OUTDOOR,
    CONF_HEATING_ENABLED,
    CONF_INDOOR_SENSOR,
    CONF_KI,
    CONF_KP,
    CONF_MANUAL_PAUSE_HOURS,
    CONF_MAX_FLOW,
    CONF_MIN_FLOW,
    CONF_OUTDOOR_SENSOR,
    CONF_OUTDOOR_SMOOTHING_HOURS,
    CONF_WEATHER_ENTITY,
)
from custom_components.gree_versati.heating.controller import HeatingController

DEVICE_ID = "502cc62fc0be"
INDOOR = "sensor.indoor_average"
OUTDOOR = "sensor.outdoor_average"
HEATING_OPTIONS = {
    CONF_HEATING_ENABLED: True,
    CONF_INDOOR_SENSOR: INDOOR,
    CONF_OUTDOOR_SENSOR: OUTDOOR,
    CONF_OUTDOOR_SMOOTHING_HOURS: 0,
}


class FakePump:
    """Answers reads from a dict of parameters and records writes."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {
            "HeWatOutTemSet": 25,
            "CoWatOutTemSet": 18,
            "WatBoxTemSet": 48,
            "TemUn": 0,
            "AllErr": 0,
            "Mod": 1,
            "Pow": 1,
            "AllOutWatTemHi": 125,  # 25.0 °C
            "AllOutWatTemLo": 0,
            "AllInWatTemHi": 122,  # 22.0 °C
            "AllInWatTemLo": 0,
            "SvSt": 0,
            "SyAnFroRunSta": 0,
        }
        self.requested: list[str] = []
        self.writes: list[dict[str, Any]] = []

    async def get(self, keys: list[str]) -> dict[str, Any]:
        self.requested.extend(keys)
        return {key: self.values[key] for key in keys if key in self.values}

    async def set(self, params: dict[str, Any]) -> None:
        self.writes.append(params)
        self.values.update(params)


@pytest.fixture
def pump() -> Iterator[FakePump]:
    fake = FakePump()

    async def get(self: GreeVersatiProtocolClient, keys: list[str]) -> dict[str, Any]:
        return await fake.get(keys)

    async def set_(self: GreeVersatiProtocolClient, params: dict[str, Any]) -> None:
        await fake.set(params)

    with (
        patch.object(GreeVersatiProtocolClient, "async_get", get),
        patch.object(GreeVersatiProtocolClient, "async_set", set_),
    ):
        yield fake


async def setup(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    *,
    indoor: str = "20.5",
    outdoor: str = "0.0",
) -> MockConfigEntry:
    hass.states.async_set(INDOOR, indoor, {"unit_of_measurement": "°C"})
    hass.states.async_set(OUTDOOR, outdoor, {"unit_of_measurement": "°C"})
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
        options=HEATING_OPTIONS if options is None else options,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def runtime(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return hass.data[DOMAIN][DATA_ENTRIES][entry.entry_id]


def controller(hass: HomeAssistant, entry: MockConfigEntry) -> HeatingController:
    heating = runtime(hass, entry)[DATA_HEATING]
    assert heating is not None
    return heating


async def poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Read the pump, then run one control step."""
    await runtime(hass, entry)[DATA_COORDINATOR].async_refresh()
    await controller(hass, entry).async_refresh()
    await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DEVICE_ID}_{key}")


def state(hass: HomeAssistant, platform: str, key: str) -> Any:
    found = entity_id(hass, platform, key)
    assert found is not None
    current = hass.states.get(found)
    assert current is not None
    return current


async def test_off_unless_enabled(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass, options={})

    assert runtime(hass, entry)[DATA_HEATING] is None
    assert entity_id(hass, "climate", "floor_heating") is None
    assert entity_id(hass, "sensor", "heating_status") is None
    assert PARAM_SV_ST not in pump.requested


async def test_advisory_mode_suggests_but_never_writes(
    hass: HomeAssistant, pump: FakePump
) -> None:
    await setup(hass)

    climate = state(hass, "climate", "floor_heating")
    assert climate.state == HVACMode.HEAT
    assert climate.attributes["current_temperature"] == 20.5
    assert climate.attributes[ATTR_TEMPERATURE] == 21.0
    assert climate.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING

    # 0 °C outside: curve 25.4 °C, plus 2.5 × 0.5 °C because the room is cold.
    assert state(hass, "sensor", "heating_status").state == "advisory"
    assert float(state(hass, "sensor", "heating_curve_water_temperature").state) == (
        pytest.approx(25.43, abs=0.01)
    )
    assert float(state(hass, "sensor", "heating_target_water_temperature").state) == (
        pytest.approx(26.68, abs=0.01)
    )
    assert state(hass, "sensor", "heating_suggested_setpoint").state == "26"
    assert pump.writes == []
    assert PARAM_SV_ST in pump.requested


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"Pow": 0}, "pump_off"),
        ({"Mod": 2}, "not_heating"),
        ({"AllErr": 5}, "pump_error"),
        ({"SvSt": 1}, "w_depend_on"),
    ],
)
async def test_pump_state_is_reported(
    hass: HomeAssistant, pump: FakePump, change: dict[str, int], status: str
) -> None:
    entry = await setup(hass)
    pump.values.update(change)

    await poll(hass, entry)

    assert state(hass, "sensor", "heating_status").state == status


async def test_missing_sensor_is_reported(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass)
    hass.states.async_set(INDOOR, STATE_UNAVAILABLE)

    await poll(hass, entry)

    assert state(hass, "sensor", "heating_status").state == "sensor_unavailable"
    assert state(hass, "climate", "floor_heating").attributes["current_temperature"] is None


async def test_thermostat_target_and_off(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass)
    climate_id = entity_id(hass, "climate", "floor_heating")

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: climate_id, ATTR_TEMPERATURE: 22},
        blocking=True,
    )
    await poll(hass, entry)
    assert state(hass, "climate", "floor_heating").attributes[ATTR_TEMPERATURE] == 22
    # Curve 26.7 °C for a 22 °C room, plus 2.5 × 1.5 °C: capped at 30 °C.
    assert float(state(hass, "sensor", "heating_target_water_temperature").state) == 30
    assert state(hass, "sensor", "heating_suggested_setpoint").state == "26"

    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_HVAC_MODE,
        {ATTR_ENTITY_ID: climate_id, ATTR_HVAC_MODE: HVACMode.OFF},
        blocking=True,
    )
    climate = state(hass, "climate", "floor_heating")
    assert climate.state == HVACMode.OFF
    assert climate.attributes[ATTR_HVAC_ACTION] == HVACAction.OFF
    await poll(hass, entry)
    assert state(hass, "sensor", "heating_status").state == "paused"


async def test_state_survives_a_reload(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass)
    await hass.services.async_call(
        CLIMATE_DOMAIN,
        SERVICE_SET_TEMPERATURE,
        {ATTR_ENTITY_ID: entity_id(hass, "climate", "floor_heating"), ATTR_TEMPERATURE: 22.5},
        blocking=True,
    )

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert state(hass, "climate", "floor_heating").attributes[ATTR_TEMPERATURE] == 22.5


async def test_calibration_fits_the_curve_to_todays_setpoint(
    hass: HomeAssistant, pump: FakePump
) -> None:
    pump.values["HeWatOutTemSet"] = 27
    entry = await setup(hass, indoor="21.0")
    before = controller(hass, entry)

    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, "button", "heating_calibrate")},
        blocking=True,
    )
    await hass.async_block_till_done()
    await poll(hass, entry)

    # Shifted by 27 - 25.43 at both points, retuned without a reload.
    assert entry.options[CONF_CURVE_COLD_FLOW] == pytest.approx(31.6)
    assert entry.options[CONF_CURVE_MILD_FLOW] == pytest.approx(23.6)
    assert controller(hass, entry) is before
    assert float(state(hass, "sensor", "heating_curve_water_temperature").state) == (
        pytest.approx(27, abs=0.05)
    )


async def test_failed_start_leaves_the_pump_entities_working(
    hass: HomeAssistant, pump: FakePump
) -> None:
    with patch.object(HeatingController, "async_load", side_effect=RuntimeError("boom")):
        entry = await setup(hass)

    assert runtime(hass, entry)[DATA_HEATING] is None
    assert entity_id(hass, "climate", "floor_heating") is None
    assert state(hass, "number", "HeWatOutTemSet").state == "25.0"


async def test_failed_start_keeps_the_saved_state(
    hass: HomeAssistant, pump: FakePump, hass_storage: dict[str, Any]
) -> None:
    key = "gree_versati.heating.saved"
    saved = {"version": 1, "minor_version": 1, "key": key, "data": {"learned": 1.2}}
    hass_storage[key] = saved

    with (
        patch(
            "custom_components.gree_versati.heating.controller.storage_key",
            return_value=key,
        ),
        patch.object(HeatingController, "async_load", side_effect=RuntimeError("boom")),
    ):
        entry = await setup(hass)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert hass_storage[key] == saved


# Options


async def _open_options(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> str:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": step}
    )
    assert result["type"] is FlowResultType.FORM
    return result["flow_id"]


def _heating_form(**changes: Any) -> dict[str, Any]:
    form = {
        CONF_HEATING_ENABLED: True,
        CONF_INDOOR_SENSOR: INDOOR,
        CONF_OUTDOOR_SENSOR: OUTDOOR,
        CONF_CURVE_COLD_OUTDOOR: -20.0,
        CONF_CURVE_COLD_FLOW: 30.0,
        CONF_CURVE_MILD_OUTDOOR: 15.0,
        CONF_CURVE_MILD_FLOW: 22.0,
        CONF_MIN_FLOW: 20.0,
        CONF_MAX_FLOW: 30.0,
        CONF_KP: 2.5,
        CONF_KI: 0.1,
        CONF_OUTDOOR_SMOOTHING_HOURS: 0.0,
        CONF_MANUAL_PAUSE_HOURS: 6.0,
    }
    form.update(changes)
    return {key: value for key, value in form.items() if value is not None}


async def test_polling_options_keep_heating_options(
    hass: HomeAssistant, pump: FakePump
) -> None:
    entry = await setup(hass)
    flow_id = await _open_options(hass, entry, "polling")

    result = await hass.config_entries.options.async_configure(
        flow_id, {CONF_SCAN_INTERVAL: 45}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {**HEATING_OPTIONS, CONF_SCAN_INTERVAL: 45}
    assert runtime(hass, entry)[DATA_HEATING] is not None


async def test_enabling_control_in_the_options(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass, options={})
    flow_id = await _open_options(hass, entry, "heating")

    result = await hass.config_entries.options.async_configure(flow_id, _heating_form())
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert state(hass, "climate", "floor_heating").state == HVACMode.HEAT


async def test_retuning_does_not_reload(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass)
    before = controller(hass, entry)
    flow_id = await _open_options(hass, entry, "heating")

    await hass.config_entries.options.async_configure(flow_id, _heating_form(**{CONF_KP: 1.5}))
    await hass.async_block_till_done()

    assert controller(hass, entry) is before
    assert before.core.settings.kp == 1.5


@pytest.mark.parametrize(
    ("changes", "field", "error"),
    [
        ({CONF_INDOOR_SENSOR: None}, CONF_INDOOR_SENSOR, "sensor_required"),
        ({CONF_OUTDOOR_SENSOR: INDOOR}, CONF_OUTDOOR_SENSOR, "same_sensor"),
        (
            {CONF_CURVE_COLD_OUTDOOR: 5.0, CONF_CURVE_MILD_OUTDOOR: 0.0},
            CONF_CURVE_MILD_OUTDOOR,
            "curve_outdoor_order",
        ),
        ({CONF_CURVE_MILD_FLOW: 31.0}, CONF_CURVE_MILD_FLOW, "curve_flow_order"),
        ({CONF_MAX_FLOW: 20.0}, CONF_MAX_FLOW, "flow_range"),
    ],
)
async def test_heating_options_are_checked(
    hass: HomeAssistant,
    pump: FakePump,
    changes: dict[str, Any],
    field: str,
    error: str,
) -> None:
    entry = await setup(hass, options={})
    flow_id = await _open_options(hass, entry, "heating")

    result = await hass.config_entries.options.async_configure(
        flow_id, _heating_form(**changes)
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}


# Writing to the pump


async def _switch(hass: HomeAssistant, key: str, on: bool) -> None:
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
        {ATTR_ENTITY_ID: entity_id(hass, "switch", key)},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _switch_writing(hass: HomeAssistant, on: bool) -> None:
    await _switch(hass, "heating_write", on)


async def test_writing_sets_the_pump_one_degree_at_a_time(
    hass: HomeAssistant, pump: FakePump, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup(hass)
    assert state(hass, "switch", "heating_write").state == STATE_OFF

    await _switch_writing(hass, True)
    assert pump.writes == [{"HeWatOutTemSet": 26}]
    assert state(hass, "number", "HeWatOutTemSet").state == "26.0"

    # Colder room: the target rises past 27, but changes wait 30 minutes.
    hass.states.async_set(INDOOR, "19.5", {"unit_of_measurement": "°C"})
    freezer.tick(timedelta(minutes=10))
    await poll(hass, entry)
    assert state(hass, "sensor", "heating_status").state == "rate_limited"
    assert len(pump.writes) == 1

    freezer.tick(timedelta(minutes=21))
    await poll(hass, entry)
    assert pump.writes[-1] == {"HeWatOutTemSet": 27}
    assert state(hass, "sensor", "heating_status").state == "active"


@pytest.mark.parametrize(
    ("change", "status"),
    [({"Pow": 0}, "pump_off"), ({"SvSt": 1}, "w_depend_on"), ({"AllErr": 3}, "pump_error")],
)
async def test_holds_block_writing(
    hass: HomeAssistant, pump: FakePump, change: dict[str, int], status: str
) -> None:
    pump.values.update(change)
    await setup(hass)

    await _switch_writing(hass, True)

    assert pump.writes == []
    assert state(hass, "sensor", "heating_status").state == status


async def test_never_writes_above_the_maximum(hass: HomeAssistant, pump: FakePump) -> None:
    pump.values["HeWatOutTemSet"] = 33  # set by hand above the floor's limit
    await setup(hass, indoor="18.0", outdoor="-25.0")

    await _switch_writing(hass, True)

    assert pump.writes == [{"HeWatOutTemSet": 30}]


async def test_manual_change_pauses_writing(
    hass: HomeAssistant, pump: FakePump, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup(hass)
    await _switch_writing(hass, True)

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id(hass, "number", "HeWatOutTemSet"), ATTR_VALUE: 23},
        blocking=True,
    )
    freezer.tick(timedelta(minutes=5))
    await poll(hass, entry)

    assert state(hass, "sensor", "heating_status").state == "manual_override"
    assert state(hass, "climate", "floor_heating").attributes["manual_change_until"]
    assert pump.values["HeWatOutTemSet"] == 23

    freezer.tick(timedelta(hours=6))
    await poll(hass, entry)
    assert pump.writes[-1] == {"HeWatOutTemSet": 24}


async def test_failed_write_is_reported(hass: HomeAssistant, pump: FakePump) -> None:
    async def refuse(params: dict[str, Any]) -> None:
        raise GreeVersatiClientError("no answer")

    await setup(hass)
    with patch.object(pump, "set", refuse):
        await _switch_writing(hass, True)

    assert state(hass, "sensor", "heating_status").state == "write_failed"
    assert pump.values["HeWatOutTemSet"] == 25


async def test_writing_survives_a_reload_and_can_be_turned_off(
    hass: HomeAssistant, pump: FakePump
) -> None:
    entry = await setup(hass)
    await _switch_writing(hass, True)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert state(hass, "switch", "heating_write").state == STATE_ON

    await _switch_writing(hass, False)
    await poll(hass, entry)
    assert state(hass, "sensor", "heating_status").state == "advisory"


# Tuning from a dashboard


async def _set_number(hass: HomeAssistant, key: str, value: float) -> None:
    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_ENTITY_ID: entity_id(hass, "number", key), ATTR_VALUE: value},
        blocking=True,
    )
    await hass.async_block_till_done()


async def _press(hass: HomeAssistant, key: str) -> None:
    await hass.services.async_call(
        BUTTON_DOMAIN,
        SERVICE_PRESS,
        {ATTR_ENTITY_ID: entity_id(hass, "button", key)},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_tuning_numbers_retune_without_a_reload(
    hass: HomeAssistant, pump: FakePump
) -> None:
    entry = await setup(hass)
    before = controller(hass, entry)

    await _set_number(hass, "heating_curve_cold_flow", 29)
    await _set_number(hass, "heating_kp", 1.5)

    assert entry.options[CONF_CURVE_COLD_FLOW] == 29
    assert entry.options[CONF_KP] == 1.5
    assert controller(hass, entry) is before
    assert before.core.settings.curve.cold_flow == 29
    assert state(hass, "number", "heating_curve_cold_flow").state == "29.0"
    assert state(hass, "number", "heating_kp").state == "1.5"


async def test_tuning_that_breaks_the_curve_is_refused(
    hass: HomeAssistant, pump: FakePump
) -> None:
    entry = await setup(hass)

    with pytest.raises(ServiceValidationError) as raised:
        await _set_number(hass, "heating_curve_mild_flow", 31)

    assert raised.value.translation_key == "curve_flow_order"
    assert CONF_CURVE_MILD_FLOW not in entry.options


async def test_learning_can_be_paused_and_reset(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass)
    heating = controller(hass, entry)
    heating.core.state.learned = 1.5

    await _switch(hass, "heating_learning", False)
    assert heating.core.state.learning_enabled is False
    assert state(hass, "switch", "heating_learning").state == STATE_OFF

    await _press(hass, "heating_reset_learning")
    await poll(hass, entry)
    assert float(state(hass, "sensor", "heating_learned_offset").state) == 0


async def test_resume_ends_a_manual_change_pause(
    hass: HomeAssistant, pump: FakePump, freezer: FrozenDateTimeFactory
) -> None:
    entry = await setup(hass)
    await _switch_writing(hass, True)
    pump.values["HeWatOutTemSet"] = 23
    freezer.tick(timedelta(minutes=5))
    await poll(hass, entry)
    assert state(hass, "sensor", "heating_status").state == "manual_override"

    await _press(hass, "heating_resume")
    await poll(hass, entry)

    # Control is back, still keeping 30 minutes between changes.
    assert state(hass, "sensor", "heating_status").state == "rate_limited"
    freezer.tick(timedelta(minutes=25))
    await poll(hass, entry)
    assert pump.writes[-1] == {"HeWatOutTemSet": 24}


# Dashboard card


async def test_dashboard_card_is_served_and_loaded(
    hass: HomeAssistant, pump: FakePump, hass_client: ClientSessionGenerator
) -> None:
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")

    with patch("custom_components.gree_versati.heating.card.add_extra_js_url") as add_js:
        await setup(hass)

    add_js.assert_called_once()
    assert add_js.call_args.args[1].startswith("/gree_versati/floor-heating-card.js?v=")
    response = await (await hass_client()).get("/gree_versati/floor-heating-card.js")
    assert response.status == 200
    assert "customElements.define" in await response.text()


async def test_dashboard_card_needs_control_enabled(hass: HomeAssistant, pump: FakePump) -> None:
    assert await async_setup_component(hass, "http", {})
    hass.config.components.add("frontend")

    with patch("custom_components.gree_versati.heating.card.add_extra_js_url") as add_js:
        await setup(hass, options={})

    add_js.assert_not_called()


async def test_smoothed_outdoor_temperature_sensor(hass: HomeAssistant, pump: FakePump) -> None:
    await setup(hass, outdoor="-3.5")

    assert float(state(hass, "sensor", "heating_outdoor_temperature").state) == -3.5


# Weather forecast

WEATHER = "weather.forecast_home"
# Clear-sky midday falls 2-6 h after this in the test location (San Diego).
MORNING = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


def provide_forecast(
    hass: HomeAssistant, *, temperature: float, cloud: float, unit: str = "°C"
) -> None:
    """A weather entity whose hourly forecast stays the same for 12 hours."""
    hass.states.async_set(WEATHER, "sunny", {"temperature_unit": unit})
    items = [
        {
            "datetime": (MORNING + timedelta(hours=hour)).isoformat(),
            "temperature": temperature,
            "cloud_coverage": cloud,
        }
        for hour in range(13)
    ]

    async def get_forecasts(call: ServiceCall) -> ServiceResponse:
        return {WEATHER: {"forecast": items}}

    hass.services.async_register(
        "weather", "get_forecasts", get_forecasts, supports_response=SupportsResponse.ONLY
    )


FORECAST_OPTIONS = {**HEATING_OPTIONS, CONF_WEATHER_ENTITY: WEATHER}


@pytest.mark.parametrize(("unit", "temperature"), [("°C", 4.0), ("°F", 39.2)])
async def test_forecast_looks_ahead_and_cuts_for_sun(
    hass: HomeAssistant,
    pump: FakePump,
    freezer: FrozenDateTimeFactory,
    unit: str,
    temperature: float,
) -> None:
    freezer.move_to(MORNING)
    provide_forecast(hass, temperature=temperature, cloud=0, unit=unit)

    entry = await setup(hass, FORECAST_OPTIONS)

    # Now 0 °C, then 4 °C for every half hour of the next 4 hours.
    ahead = float(state(hass, "sensor", "heating_forecast_outdoor_temperature").state)
    assert ahead == pytest.approx(32 / 9, abs=0.01)
    decision = controller(hass, entry).data
    assert decision.outdoor == pytest.approx(32 / 9, abs=0.01)
    assert 1.8 <= decision.sun_cut <= 2.0
    assert float(state(hass, "sensor", "heating_sun_cut").state) == pytest.approx(
        decision.sun_cut
    )
    assert decision.target_flow == pytest.approx(
        decision.curve_flow - decision.sun_cut + decision.correction
    )


async def test_cloudy_forecast_cuts_nothing(
    hass: HomeAssistant, pump: FakePump, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    provide_forecast(hass, temperature=4.0, cloud=100)

    entry = await setup(hass, FORECAST_OPTIONS)

    assert controller(hass, entry).data.sun_cut == 0
    assert controller(hass, entry).data.outdoor_ahead == pytest.approx(32 / 9, abs=0.01)


async def test_without_a_forecast_control_carries_on(
    hass: HomeAssistant, pump: FakePump, caplog: pytest.LogCaptureFixture
) -> None:
    entry = await setup(hass, FORECAST_OPTIONS)
    await poll(hass, entry)

    decision = controller(hass, entry).data
    assert decision.outdoor_ahead is None
    assert decision.sun_cut == 0
    assert decision.target_flow == pytest.approx(26.68, abs=0.01)
    assert state(hass, "sensor", "heating_status").state == "advisory"
    assert state(hass, "sensor", "heating_forecast_outdoor_temperature").state == "unknown"
    assert caplog.text.count("No hourly forecast from weather.forecast_home") == 1


async def test_settings_offer_the_default_forecast(hass: HomeAssistant, pump: FakePump) -> None:
    hass.states.async_set(WEATHER, "sunny", {"temperature_unit": "°C"})
    entry = await setup(hass, options={})

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "heating"}
    )

    field = next(key for key in result["data_schema"].schema if key == CONF_WEATHER_ENTITY)
    assert field.description == {"suggested_value": WEATHER}


async def test_clearing_the_forecast_turns_it_off(hass: HomeAssistant, pump: FakePump) -> None:
    entry = await setup(hass, FORECAST_OPTIONS)
    flow_id = await _open_options(hass, entry, "heating")

    await hass.config_entries.options.async_configure(flow_id, _heating_form())
    await hass.async_block_till_done()

    assert entry.options[CONF_WEATHER_ENTITY] == ""
    assert controller(hass, entry).weather_entity_id is None
