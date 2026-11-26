"""Fetches the hourly forecast for floor heating control and reads it.

The maths lives in forecast.py. This module asks the weather entity for its
forecast at most every half hour, gives up on one that doesn't answer, keeps
the last good one for a few hours, and turns it into the look-ahead outdoor
temperature and sun cut for a control step.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import FORECAST_MAX_AGE, FORECAST_REFRESH, FORECAST_TIMEOUT
from .curve import ControlSettings
from .forecast import (
    HourlyForecast,
    clear_sky,
    cloud_at,
    lookahead_outdoor,
    parse_hour,
    sun_score,
    window_times,
)

_LOGGER = logging.getLogger(__name__)


class ForecastSource:
    """One weather entity's hourly forecast, as floor heating control uses it."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._forecast: list[HourlyForecast] = []
        self._fetched: float | None = None
        self._tried: float | None = None
        self._failing = False

    async def async_inputs(
        self, outdoor: float | None, settings: ControlSettings, now: float
    ) -> tuple[float | None, float]:
        """Look-ahead outdoor temperature and sun cut; (None, 0) without a forecast.

        Asks the weather entity only when look-ahead or sun cut uses it, and
        never waits more than about 30 s for an answer.
        """
        if outdoor is None or not settings.uses_forecast:
            return None, 0.0
        forecast = await self._async_forecast(now)
        if not forecast:
            return None, 0.0
        ahead = lookahead_outdoor(outdoor, forecast, now, settings.lookahead_hours)
        sun_cut = 0.0
        if settings.sun_cut_max > 0:
            sun_cut = settings.sun_cut_max * self._sun_score(forecast, now, settings)
        return ahead, sun_cut

    def ask_now(self) -> None:
        """Ask at the next step rather than at the next half-hourly refresh."""
        self._tried = None

    async def _async_forecast(self, now: float) -> list[HourlyForecast]:
        """The hourly forecast, fetched at most every half hour; [] if too old."""
        if self._tried is None or now - self._tried >= FORECAST_REFRESH.total_seconds():
            self._tried = now
            await self._async_fetch(now)
        if self._fetched is None or now - self._fetched > FORECAST_MAX_AGE.total_seconds():
            return []
        return self._forecast

    async def _async_fetch(self, now: float) -> None:
        timeout = asyncio.timeout(FORECAST_TIMEOUT.total_seconds())
        try:
            async with timeout:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"type": "hourly"},
                    target={"entity_id": self.entity_id},
                    blocking=True,
                    return_response=True,
                )
            items = response[self.entity_id]["forecast"]
            forecast = self._parse(items)
        except Exception as err:  # A forecast must never stop floor heating control.
            if not self._failing:
                _LOGGER.warning(
                    "No hourly forecast from %s; floor heating carries on without it: %s",
                    self.entity_id,
                    f"no answer within {FORECAST_TIMEOUT.total_seconds():.0f} s"
                    if timeout.expired()
                    else err,
                )
            self._failing = True
            return
        if self._failing:
            _LOGGER.info("Hourly forecast from %s is back", self.entity_id)
        self._failing = False
        self._forecast = forecast
        self._fetched = now

    def _parse(self, items: list[Mapping[str, Any]]) -> list[HourlyForecast]:
        state = self.hass.states.get(self.entity_id)
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

    def _sun_score(
        self, forecast: list[HourlyForecast], now: float, settings: ControlSettings
    ) -> float:
        """Forecast sun a few hours ahead, 0..1, using the real sun at this location."""
        import astral  # noqa: PLC0415 - Home Assistant loads astral lazily too
        import astral.sun  # noqa: PLC0415

        config = self.hass.config
        observer = astral.Observer(config.latitude, config.longitude, config.elevation)

        def clear(at: float) -> float:
            return clear_sky(astral.sun.elevation(observer, dt_util.utc_from_timestamp(at)))

        start, end = settings.sun_window_hours
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
