"""Unit tests for the weather forecast maths of floor heating control."""

from __future__ import annotations

import pytest

from custom_components.gree_versati.heating.curve import (
    ControlSettings,
    ControlState,
    HeatingCore,
    Inputs,
)
from custom_components.gree_versati.heating.forecast import (
    HourlyForecast,
    clear_sky,
    cloud_at,
    lookahead_outdoor,
    parse_hour,
    sun_score,
    temperature_at,
    window_times,
)

HOUR = 3600.0
NOW = 1_800_000_000.0  # on a whole hour

FORECAST = [
    HourlyForecast(start=NOW + HOUR, temperature=2.0, cloud=0.0),
    HourlyForecast(start=NOW + 2 * HOUR, temperature=4.0, cloud=0.5),
    HourlyForecast(start=NOW + 3 * HOUR, temperature=6.0, cloud=1.0),
]


def _time(text: str) -> float | None:
    return {"2026-10-21T10:00:00+00:00": NOW}.get(text)


def test_parse_hour() -> None:
    hour = parse_hour(
        {"datetime": "2026-10-21T10:00:00+00:00", "temperature": 41, "cloud_coverage": 25},
        _time,
        lambda fahrenheit: round((fahrenheit - 32) * 5 / 9, 1),
    )

    assert hour == HourlyForecast(start=NOW, temperature=5.0, cloud=0.25)


@pytest.mark.parametrize(
    ("condition", "cloud"),
    [("sunny", 0.0), ("partlycloudy", 0.5), ("cloudy", 0.9), ("rainy", 1.0), ("fog", 1.0)],
)
def test_cloud_from_condition_when_no_cover_is_given(condition: str, cloud: float) -> None:
    hour = parse_hour(
        {"datetime": "2026-10-21T10:00:00+00:00", "condition": condition}, _time, float
    )

    assert hour is not None
    assert hour.cloud == cloud
    assert hour.temperature is None


def test_unusable_hours_are_skipped() -> None:
    assert parse_hour({"datetime": "yesterday", "temperature": 3}, _time, float) is None


def test_temperature_between_hours() -> None:
    assert temperature_at(FORECAST, NOW + 1.5 * HOUR) == pytest.approx(3.0)
    assert temperature_at(FORECAST, NOW + 3 * HOUR) == pytest.approx(6.0)
    # A forecast starting at the next full hour also stands for the hour before.
    assert temperature_at(FORECAST, NOW + 0.5 * HOUR) == pytest.approx(2.0)
    assert temperature_at(FORECAST, NOW - 0.5 * HOUR) is None
    assert temperature_at(FORECAST, NOW + 3.5 * HOUR) is None


def test_cloud_for_the_hour() -> None:
    assert cloud_at(FORECAST, NOW + 2.9 * HOUR) == 0.5
    assert cloud_at(FORECAST, NOW + 0.5 * HOUR) == 0.0
    assert cloud_at(FORECAST, NOW + 4 * HOUR) is None


def test_lookahead_averages_now_and_the_hours_ahead() -> None:
    # Now 0 °C; half-hourly ahead: 2, 2, 3, 4 (to two hours ahead).
    assert lookahead_outdoor(0.0, FORECAST, NOW, 2) == pytest.approx((0 + 2 + 2 + 3 + 4) / 5)


def test_lookahead_off_or_not_covered() -> None:
    assert lookahead_outdoor(0.0, FORECAST, NOW, 0) is None
    assert lookahead_outdoor(0.0, FORECAST, NOW, 4) is None
    assert lookahead_outdoor(0.0, [], NOW, 2) is None


def test_window_times() -> None:
    times = window_times(NOW, 2, 6)

    assert len(times) == 9
    assert times[0] == NOW + 2 * HOUR
    assert times[-1] == NOW + 6 * HOUR


def test_clear_sky() -> None:
    assert clear_sky(90) == pytest.approx(1)
    assert clear_sky(30) == pytest.approx(0.5)
    assert clear_sky(-5) == 0


def test_sun_score() -> None:
    window = [(0.4, 0.0), (0.6, 0.0)]

    assert sun_score(window, best_clear=0.5) == pytest.approx(1)
    assert sun_score([(0.4, 0.5), (0.6, 0.5)], best_clear=0.5) == pytest.approx(0.5)
    assert sun_score([(0.4, 1.0)], best_clear=0.5) == 0
    assert sun_score(window, best_clear=0.25) == 1  # capped
    assert sun_score([], best_clear=0.5) == 0
    assert sun_score(window, best_clear=0) == 0


def _step(**inputs: object):
    core = HeatingCore(
        ControlSettings(outdoor_smoothing_hours=0), ControlState(write_enabled=True)
    )
    return core.step(Inputs(now=NOW, indoor=21.0, setpoint=25, **inputs))


def test_curve_follows_the_lookahead_temperature() -> None:
    decision = _step(outdoor=0.0, outdoor_ahead=-10.0)

    assert decision.outdoor == -10
    assert decision.outdoor_ahead == -10
    assert decision.curve_flow == pytest.approx(27.71, abs=0.01)


def test_sun_cut_lowers_the_target() -> None:
    plain = _step(outdoor=-10.0)
    sunny = _step(outdoor=-10.0, sun_cut=1.5)

    assert sunny.curve_flow == plain.curve_flow
    assert sunny.target_flow == pytest.approx(plain.target_flow - 1.5)
    assert sunny.sun_cut == 1.5


def test_the_reading_is_still_needed() -> None:
    assert _step(outdoor=None, outdoor_ahead=-10.0).target_flow is None
