"""Runs the floor heating control maths against live readings."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
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
    CONF_WEATHER_ENTITY,
    FORECAST_MAX_AGE,
    FORECAST_REFRESH,
    LIVE_OPTIONS,
    STALE_AFTER,
    STORAGE_VERSION,
    UPDATE_INTERVAL,
    options_with_defaults,
    settings_from_options,
    storage_key,
)
from .curve import ControlState, Decision, HeatingCore, Inputs, Status, calibrated_curve
from .forecast import (
    HourlyForecast,
    clear_sky,
    cloud_at,
    lookahead_outdoor,
    parse_hour,
    sun_score,
    window_times,
)
from .options import validate_heating

_LOGGER = logging.getLogger(__name__)

HEATING_MODES = frozenset({MODE_OPTIONS["Heat"], MODE_OPTIONS["Heat + hot water"]})


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _same_option(old: Any, new: Any) -> bool:
    # An empty entity field ("") and a missing option both mean none.
    return old == new or (old in (None, "") and new in (None, ""))


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
        self.weather_entity_id: str | None = entry.options.get(CONF_WEATHER_ENTITY) or None
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
        self._forecast: list[HourlyForecast] = []
        self._forecast_fetched: datetime | None = None
        self._forecast_tried: datetime | None = None
        self._forecast_failing = False

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
        outdoor = self.temperature(self.outdoor_entity_id)
        outdoor_ahead, sun_cut = await self._async_forecast_inputs(outdoor, now)
        decision = self.core.step(
            Inputs(
                now=now,
                outdoor=outdoor,
                indoor=self.temperature(self.indoor_entity_id),
                setpoint=setpoint,
                hold=pump_hold(pump),
                space_heating=space_heating(pump),
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

    async def _async_forecast_inputs(
        self, outdoor: float | None, now: float
    ) -> tuple[float | None, float]:
        """Look-ahead outdoor temperature and sun cut; (None, 0) without a forecast."""
        if self.weather_entity_id is None or outdoor is None:
            return None, 0.0
        forecast = await self._async_forecast()
        if not forecast:
            return None, 0.0
        settings = self.core.settings
        ahead = lookahead_outdoor(outdoor, forecast, now, settings.lookahead_hours)
        sun_cut = 0.0
        if settings.sun_cut_max > 0:
            sun_cut = settings.sun_cut_max * self._sun_score(forecast, now)
        return ahead, sun_cut

    def _sun_score(self, forecast: list[HourlyForecast], now: float) -> float:
        """Forecast sun a few hours ahead, 0..1, using the real sun at this location."""
        import astral  # noqa: PLC0415 - Home Assistant loads astral lazily too
        import astral.sun  # noqa: PLC0415

        config = self.hass.config
        observer = astral.Observer(config.latitude, config.longitude, config.elevation)

        def clear(at: float) -> float:
            return clear_sky(astral.sun.elevation(observer, dt_util.utc_from_timestamp(at)))

        start, end = self.core.settings.sun_window_hours
        window = []
        for at in window_times(now, start, end):
            cloud = cloud_at(forecast, at)
            if cloud is None:
                return 0.0
            window.append((clear(at), cloud))

        # Compare with a clear sky over the sunniest stretch of that day.
        middle = dt_util.as_local(dt_util.utc_from_timestamp(now + (start + end) / 2 * 3600))
        noon = astral.sun.noon(observer, middle.date()).timestamp()
        best = window_times(noon, -(end - start) / 2, (end - start) / 2)
        return sun_score(window, sum(clear(at) for at in best) / len(best))

    async def _async_forecast(self) -> list[HourlyForecast]:
        """The hourly forecast, fetched at most every half hour; [] if too old."""
        now = dt_util.utcnow()
        if self._forecast_tried is None or now - self._forecast_tried >= FORECAST_REFRESH:
            self._forecast_tried = now
            await self._async_fetch_forecast(now)
        if self._forecast_fetched is None or now - self._forecast_fetched > FORECAST_MAX_AGE:
            return []
        return self._forecast

    async def _async_fetch_forecast(self, now: datetime) -> None:
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": self.weather_entity_id},
                blocking=True,
                return_response=True,
            )
            items = response[self.weather_entity_id]["forecast"]
            forecast = self._parse_forecast(items)
        except Exception as err:  # A forecast must never stop floor heating control.
            if not self._forecast_failing:
                _LOGGER.warning(
                    "No hourly forecast from %s; floor heating carries on without it: %s",
                    self.weather_entity_id,
                    err,
                )
            self._forecast_failing = True
            return
        if self._forecast_failing:
            _LOGGER.info("Hourly forecast from %s is back", self.weather_entity_id)
        self._forecast_failing = False
        self._forecast = forecast
        self._forecast_fetched = now

    def _parse_forecast(self, items: list[Mapping[str, Any]]) -> list[HourlyForecast]:
        state = self.hass.states.get(self.weather_entity_id or "")
        unit = (state.attributes.get("temperature_unit") if state else None) or (
            self.hass.config.units.temperature_unit
        )

        def to_celsius(value: float) -> float:
            if unit == UnitOfTemperature.CELSIUS:
                return value
            return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

        def parse_time(value: Any) -> float | None:
            if isinstance(value, datetime):
                return value.timestamp()
            parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
            return parsed.timestamp() if parsed else None

        hours = [parse_hour(item, parse_time, to_celsius) for item in items]
        return sorted((hour for hour in hours if hour is not None), key=lambda hour: hour.start)

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
        pump = self._pump_data
        setpoint = _as_int(pump.get(PARAM_HE_WAT_OUT_TEM_SET)) if pump else None
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
        self.core.settings = settings_from_options(options)
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
