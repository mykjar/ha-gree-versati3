"""Runs the floor heating control maths against live readings."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from ..client import GreeVersatiClientError
from ..constants import (
    DOMAIN,
    MAX_HE_WAT_OUT_TEMP_SET,
    MIN_HE_WAT_OUT_TEMP_SET,
    MODE_OPTIONS,
    PARAM_ALL_ERR,
    PARAM_ALL_IN_WAT_TEM,
    PARAM_ALL_OUT_WAT_TEM,
    PARAM_HE_WAT_OUT_TEM_SET,
    PARAM_MOD,
    PARAM_POW,
    PARAM_SV_ST,
    PARAM_SY_AN_FRO_RUN_STA,
)
from ..coordinator import GreeVersatiCoordinator
from ..temperature import decode_measured_temperature
from .const import (
    BUSY_FLOW_DIFFERENCE,
    CONF_CURVE_COLD_FLOW,
    CONF_CURVE_MILD_FLOW,
    CONF_INDOOR_SENSOR,
    CONF_OUTDOOR_SENSOR,
    LIVE_OPTIONS,
    STALE_AFTER,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    settings_from_options,
    storage_key,
)
from .curve import ControlState, Decision, HeatingCore, Inputs, Status, calibrated_curve

_LOGGER = logging.getLogger(__name__)

HEATING_MODES = frozenset({MODE_OPTIONS["Heat"], MODE_OPTIONS["Heat + hot water"]})


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pump_hold(data: Mapping[str, Any] | None) -> Status | None:
    """Pump-side reason not to change the setpoint, if there is one."""
    if not data:
        return Status.PUMP_UNAVAILABLE
    if _as_int(data.get(PARAM_POW)) != 1:
        return Status.PUMP_OFF
    if _as_int(data.get(PARAM_MOD)) not in HEATING_MODES:
        return Status.NOT_HEATING
    error = data.get(PARAM_ALL_ERR)
    if error not in (None, "") and _as_int(error) != 0:
        return Status.PUMP_ERROR
    if _as_int(data.get(PARAM_SV_ST)) == 1:
        return Status.W_DEPEND_ON
    return None


def space_heating(data: Mapping[str, Any] | None) -> bool:
    """False while the pump is defrosting or busy with hot water."""
    if not data:
        return False
    if _as_int(data.get(PARAM_SY_AN_FRO_RUN_STA)) == 1:
        return False
    flow = decode_measured_temperature(data, PARAM_ALL_OUT_WAT_TEM)
    setpoint = _as_int(data.get(PARAM_HE_WAT_OUT_TEM_SET))
    if flow is not None and setpoint is not None:
        return abs(flow - setpoint) <= BUSY_FLOW_DIFFERENCE
    return True


class HeatingController(DataUpdateCoordinator[Decision]):
    """Every few minutes, works out the floor water setpoint for the pump."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        pump: GreeVersatiCoordinator,
    ) -> None:
        self.indoor_entity_id: str = entry.options[CONF_INDOOR_SENSOR]
        self.outdoor_entity_id: str = entry.options[CONF_OUTDOOR_SENSOR]
        # Also arranges for async_shutdown to run when the entry unloads.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} floor heating",
            update_interval=UPDATE_INTERVAL,
        )
        self.pump = pump
        self._entry = entry
        self.core = HeatingCore(settings_from_options(entry.options))
        self._options = dict(entry.options)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, storage_key(entry.entry_id)
        )
        self._loaded = False

    async def async_load(self) -> None:
        """Pick up where the controller left off before a restart."""
        if (saved := await self._store.async_load()) is not None:
            self.core.state = ControlState.from_dict(saved)
        self._loaded = True

    async def async_shutdown(self) -> None:
        """Stop the loop and save the state right away."""
        await super().async_shutdown()
        # A controller that never loaded must not overwrite the saved state.
        if self._loaded:
            await self._store.async_save(self.core.state.as_dict())

    async def _async_update_data(self) -> Decision:
        pump = self._pump_data
        now = dt_util.utcnow().timestamp()
        setpoint = _as_int(pump.get(PARAM_HE_WAT_OUT_TEM_SET)) if pump else None
        decision = self.core.step(
            Inputs(
                now=now,
                outdoor=self.temperature(self.outdoor_entity_id),
                indoor=self.temperature(self.indoor_entity_id),
                setpoint=setpoint,
                hold=pump_hold(pump),
                space_heating=space_heating(pump),
            )
        )
        if decision.write is not None:
            decision = await self._async_write(decision, decision.write, setpoint, now)
        self._save()
        return decision

    async def _async_write(
        self, decision: Decision, value: int, previous: int | None, now: float
    ) -> Decision:
        # Settings are checked when saved, but never send the pump anything
        # outside its own range.
        if not MIN_HE_WAT_OUT_TEMP_SET <= value <= MAX_HE_WAT_OUT_TEMP_SET:
            _LOGGER.error("Refusing to set the floor water setpoint to %s °C", value)
            self.core.record_write_failure(now)
            return replace(decision, status=Status.WRITE_FAILED, write=None)
        try:
            await self.pump.client.async_set({PARAM_HE_WAT_OUT_TEM_SET: value})
        except GreeVersatiClientError as err:
            _LOGGER.warning("Could not set the floor water setpoint to %s °C: %s", value, err)
            self.core.record_write_failure(now)
            return replace(decision, status=Status.WRITE_FAILED, write=None)

        self.core.record_write(value, previous, now)
        _LOGGER.info(
            "Floor water setpoint %s -> %s °C (outdoor %.1f °C, curve %.1f °C, "
            "room correction %+.1f °C, target %.1f °C)",
            previous,
            value,
            decision.outdoor,
            decision.curve_flow,
            decision.correction,
            decision.target_flow,
        )
        await self.pump.async_request_refresh()
        return decision

    @property
    def _pump_data(self) -> Mapping[str, Any] | None:
        return self.pump.data if self.pump.last_update_success else None

    @property
    def pump_heating(self) -> bool:
        """Whether the pump is delivering heat right now."""
        data = self._pump_data
        if data is None or pump_hold(data) in (
            Status.PUMP_UNAVAILABLE,
            Status.PUMP_OFF,
            Status.NOT_HEATING,
        ):
            return False
        flow = decode_measured_temperature(data, PARAM_ALL_OUT_WAT_TEM)
        inlet = decode_measured_temperature(data, PARAM_ALL_IN_WAT_TEM)
        return flow is not None and inlet is not None and flow > inlet + 0.5

    def temperature(self, entity_id: str) -> float | None:
        """A sensor's temperature in °C, or None if it can't be trusted."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not math.isfinite(value) or dt_util.utcnow() - state.last_reported > STALE_AFTER:
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        return value

    async def async_set_room_target(self, value: float) -> None:
        """Change the room temperature to aim for."""
        self.core.set_room_target(value, dt_util.utcnow().timestamp())
        await self._async_changed()

    async def async_set_active(self, active: bool) -> None:
        """Pause control (Off) or resume it (Heat)."""
        self.core.set_active(active)
        await self._async_changed()

    async def async_set_write_enabled(self, enabled: bool) -> None:
        """Write to the pump, or only advise."""
        self.core.set_write_enabled(enabled)
        await self._async_changed()

    async def async_calibrate(self) -> None:
        """Shift the curve so it matches the setpoint the pump runs on now."""
        pump = self._pump_data
        setpoint = _as_int(pump.get(PARAM_HE_WAT_OUT_TEM_SET)) if pump else None
        indoor = self.temperature(self.indoor_entity_id)
        outdoor = self.core.state.smoothed_outdoor
        if outdoor is None:
            outdoor = self.temperature(self.outdoor_entity_id)
        if setpoint is None or indoor is None or outdoor is None:
            raise HomeAssistantError(
                "Calibration needs the indoor and outdoor temperatures and the "
                "pump's current setpoint"
            )

        curve = calibrated_curve(
            self.core.settings.curve,
            outdoor=outdoor,
            indoor=indoor,
            room_target=self.core.state.room_target,
            setpoint=setpoint,
        )
        _LOGGER.info(
            "Calibrated floor heating curve to setpoint %s °C at %.1f °C outdoors: "
            "water %.1f-%.1f °C",
            setpoint,
            outdoor,
            curve.mild_flow,
            curve.cold_flow,
        )
        # The curve now includes what the learned offset was making up for.
        self.core.reset_learning()
        self.hass.config_entries.async_update_entry(
            self._entry,
            options={
                **self._entry.options,
                CONF_CURVE_COLD_FLOW: curve.cold_flow,
                CONF_CURVE_MILD_FLOW: curve.mild_flow,
            },
        )

    @callback
    def apply_options(self, options: Mapping[str, Any]) -> bool:
        """Retune from new options without a reload, if that's all that changed."""
        changed = {
            key
            for key in self._options.keys() | options.keys()
            if self._options.get(key) != options.get(key)
        }
        if not changed <= LIVE_OPTIONS:
            return False
        self._options = dict(options)
        self.core.settings = settings_from_options(options)
        self.hass.async_create_task(self.async_request_refresh())
        return True

    async def _async_changed(self) -> None:
        self._save()
        # Show the change right away; the recalculation may be debounced.
        self.async_update_listeners()
        await self.async_request_refresh()

    @callback
    def _save(self) -> None:
        if self._loaded:
            self._store.async_delay_save(self.core.state.as_dict, 10)
