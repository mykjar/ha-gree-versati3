"""Weather forecast maths for floor heating control.

Pure Python with no Home Assistant imports, like curve.py. A concrete floor
answers hours late, so the controller looks ahead in two ways:

- look-ahead: the curve uses the average of the outdoor temperature now and
  the forecast for every half hour of the next few hours;
- sun cut: the curve is lowered by up to a set amount when the forecast shows
  sun a few hours ahead, so the floor isn't full of heat when the sun arrives.

Both were tuned in tests/test_heating_simulation.py.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

HALF_HOUR = 1800.0

# Cloud cover to assume from the weather condition when a forecast gives none.
CONDITION_CLOUD: dict[str, float] = {
    "sunny": 0.0,
    "clear-night": 0.0,
    "partlycloudy": 0.5,
    "windy": 0.5,
    "windy-variant": 0.7,
    "cloudy": 0.9,
}


@dataclass(frozen=True)
class HourlyForecast:
    """One hour of forecast. Times are timestamps in seconds; cloud is 0..1."""

    start: float
    temperature: float | None
    cloud: float | None


def parse_hour(
    item: Mapping[str, Any],
    parse_time: Callable[[str], float | None],
    to_celsius: Callable[[float], float],
) -> HourlyForecast | None:
    """One entry of a Home Assistant hourly forecast, or None if unusable."""
    start = parse_time(item.get("datetime", ""))
    if start is None:
        return None
    temperature = _number(item.get("temperature"))
    cloud = _number(item.get("cloud_coverage"))
    if cloud is not None:
        cloud = min(max(cloud / 100, 0.0), 1.0)
    elif (condition := item.get("condition")) is not None:
        # Rain, snow, fog and the like count as fully overcast.
        cloud = CONDITION_CLOUD.get(condition, 1.0)
    return HourlyForecast(
        start=start,
        temperature=to_celsius(temperature) if temperature is not None else None,
        cloud=cloud,
    )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def temperature_at(forecast: Sequence[HourlyForecast], at: float) -> float | None:
    """Forecast temperature at a moment, between hourly points; None if not covered."""
    points = [hour for hour in forecast if hour.temperature is not None]
    if not points:
        return None
    # Forecasts often start at the next full hour; let it stand for the hour before.
    if points[0].start - 3600 <= at <= points[0].start:
        return points[0].temperature
    for before, after in zip(points, points[1:], strict=False):
        if before.start <= at <= after.start:
            span = after.start - before.start
            share = (at - before.start) / span if span else 0.0
            return before.temperature + share * (after.temperature - before.temperature)
    return None


def cloud_at(forecast: Sequence[HourlyForecast], at: float) -> float | None:
    """Forecast cloud cover for the hour containing a moment; None if not covered."""
    hours = [hour for hour in forecast if hour.cloud is not None]
    if hours and hours[0].start - 3600 <= at < hours[0].start:
        return hours[0].cloud
    for hour in hours:
        if hour.start <= at < hour.start + 3600:
            return hour.cloud
    return None


def lookahead_outdoor(
    current: float, forecast: Sequence[HourlyForecast], now: float, hours: float
) -> float | None:
    """Average of now and the forecast for each half hour of the next `hours`.

    None when look-ahead is off or the forecast doesn't cover the period.
    """
    if hours <= 0:
        return None
    temperatures = [current]
    for step in range(1, int(hours * 2) + 1):
        ahead = temperature_at(forecast, now + step * HALF_HOUR)
        if ahead is None:
            return None
        temperatures.append(ahead)
    return sum(temperatures) / len(temperatures)


def window_times(now: float, start_hours: float, end_hours: float) -> list[float]:
    """Every half hour from `start_hours` to `end_hours` ahead, inclusive."""
    count = int(round((end_hours - start_hours) * 2)) + 1
    return [now + start_hours * 3600 + step * HALF_HOUR for step in range(count)]


def clear_sky(elevation_degrees: float) -> float:
    """How strongly a clear sky shines at a sun elevation, 0..1."""
    return max(math.sin(math.radians(elevation_degrees)), 0.0)


def sun_score(window: Sequence[tuple[float, float]], best_clear: float) -> float:
    """Forecast sun over a window, from (clear sky, cloud) pairs, 0..1.

    1 means a clear sky over the sunniest stretch of the same length that day,
    `best_clear` being the average clear-sky value over that stretch.
    """
    if best_clear <= 0 or not window:
        return 0.0
    sunshine = sum(clear * (1 - cloud) for clear, cloud in window) / len(window)
    return min(max(sunshine / best_clear, 0.0), 1.0)
