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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from ..client import GreeVersatiClientError
from ..constants import (
    DOMAIN,
    MAX_HE_WAT_OUT_TEMP_SET,
    MIN_HE_WAT_OUT_TEMP_SET,
    PARAM_HE_WAT_OUT_TEM_SET,
)
from ..coordinator import GreeVersatiCoordinator
from .const import (
    CONF_CURVE_COLD_FLOW,
    CONF_CURVE_MILD_FLOW,
    CONF_INDOOR_SENSOR,
    CONF_OUTDOOR_SENSOR,
    CONF_WEATHER_ENTITY,
    LIVE_OPTIONS,
    STALE_AFTER,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    options_with_defaults,
    settings_from_options,
    storage_key,
)
from .curve import ControlState, Decision, HeatingCore, Inputs, Status, calibrated_curve
from .forecast_source import ForecastSource
from .options import validate_heating
from .pump import PumpReading, read_pump

_LOGGER = logging.getLogger(__name__)


def _same_option(old: Any, new: Any) -> bool:
    # An empty entity field ("") and a missing option both mean none.
    return old == new or (old in (None, "") and new in (None, ""))


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
        weather_entity_id = entry.options.get(CONF_WEATHER_ENTITY)
        self.forecast = ForecastSource(hass, weather_entity_id) if weather_entity_id else None

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
        reading = self.pump_reading
        now = dt_util.utcnow().timestamp()
        setpoint = reading.setpoint
        # An old outdoor reading counts as missing, as the curve can't do
        # without it. An old indoor one only stops the room correction.
        outdoor = self.temperature(self.outdoor_entity_id)
        indoor, indoor_old = self._reading(self.indoor_entity_id)
        outdoor_ahead, sun_cut = (
            await self.forecast.async_inputs(outdoor, self.core.settings, now)
            if self.forecast
            else (None, 0.0)
        )
        decision = self.core.step(
            Inputs(
                now=now,
                outdoor=outdoor,
                indoor=indoor,
                indoor_old=indoor_old,
                setpoint=setpoint,
                hold=reading.hold,
                space_heating=reading.space_heating,
                outdoor_ahead=outdoor_ahead,
                sun_cut=sun_cut,
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
    def pump_reading(self) -> PumpReading:
        """The pump as it is now, from its latest data."""
        return read_pump(self.pump.data if self.pump.last_update_success else None)

    def temperature(self, entity_id: str) -> float | None:
        """A sensor's temperature in °C, or None if it can't be trusted."""
        value, old = self._reading(entity_id)
        return None if old else value

    def _reading(self, entity_id: str) -> tuple[float | None, bool]:
        """A sensor's temperature in °C, if it has one, and whether it is old."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None, False
        try:
            value = float(state.state)
        except ValueError:
            return None, False
        if not math.isfinite(value):
            return None, False
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit in (UnitOfTemperature.FAHRENHEIT, UnitOfTemperature.KELVIN):
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        return value, dt_util.utcnow() - state.last_reported > STALE_AFTER

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

    async def async_set_learning_enabled(self, enabled: bool) -> None:
        """Turn the slowly learned curve offset on or off."""
        self.core.set_learning_enabled(enabled)
        await self._async_changed()

    async def async_reset_learning(self) -> None:
        """Forget the learned curve offset."""
        self.core.reset_learning()
        await self._async_changed()

    async def async_resume(self) -> None:
        """End a pause after a manual change now."""
        self.core.resume()
        await self._async_changed()

    async def async_set_option(self, key: str, value: float) -> None:
        """Change one tuning value, checked as if saved in the options form."""
        options = {**self._entry.options, key: value}
        if errors := validate_heating(options_with_defaults(options)):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key=next(iter(errors.values()))
            )
        self.hass.config_entries.async_update_entry(self._entry, options=options)

    async def async_calibrate(self) -> None:
        """Shift the curve so it matches the setpoint the pump runs on now."""
        setpoint = self.pump_reading.setpoint
        indoor = self.temperature(self.indoor_entity_id)
        outdoor = self.core.state.smoothed_outdoor
        if outdoor is None:
            outdoor = self.temperature(self.outdoor_entity_id)
        if setpoint is None or indoor is None or outdoor is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="calibration_needs_readings"
            )

        curve = calibrated_curve(
            self.core.settings.curve,
            outdoor=outdoor,
            indoor=indoor,
            room_target=self.core.state.room_target,
            # The setpoint already has any sun cut taken off; the curve shouldn't.
            setpoint=setpoint + (self.data.sun_cut if self.data else 0.0),
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
            if not _same_option(self._options.get(key), options.get(key))
        }
        if not changed <= LIVE_OPTIONS:
            return False
        self._options = dict(options)
        used_forecast = self.core.settings.uses_forecast
        self.core.settings = settings_from_options(options)
        if self.forecast and self.core.settings.uses_forecast and not used_forecast:
            self.forecast.ask_now()
        self.async_update_listeners()
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
