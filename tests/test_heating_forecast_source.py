"""The forecast source: fetching, keeping and reading the hourly forecast."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.gree_versati.heating.curve import ControlSettings
from custom_components.gree_versati.heating.forecast_source import ForecastSource

from .fake_weather import MORNING, WEATHER, give_up_on_forecast, let_time_pass, provide_forecast

SETTINGS = ControlSettings()  # 4 h look-ahead, up to 2 °C sun cut.
UNUSED = ControlSettings(lookahead_hours=0, sun_cut_max=0)


async def inputs(
    source: ForecastSource, settings: ControlSettings = SETTINGS, outdoor: float | None = 0.0
) -> tuple[float | None, float]:
    return await source.async_inputs(outdoor, settings, dt_util.utcnow().timestamp())


async def test_looks_ahead_and_cuts_for_sun(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    provide_forecast(hass, temperature=4.0, cloud=0)

    ahead, sun_cut = await inputs(ForecastSource(hass, WEATHER))

    # Now 0 °C, then 4 °C for every half hour of the next 4 hours.
    assert ahead == pytest.approx(32 / 9, abs=0.01)
    assert 1.8 <= sun_cut <= 2.0


async def test_cloudy_forecast_cuts_nothing(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    provide_forecast(hass, temperature=4.0, cloud=100)

    ahead, sun_cut = await inputs(ForecastSource(hass, WEATHER))

    assert sun_cut == 0
    assert ahead == pytest.approx(32 / 9, abs=0.01)


async def test_nothing_without_an_outdoor_reading_or_when_unused(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=4.0, cloud=0)
    source = ForecastSource(hass, WEATHER)

    assert await inputs(source, outdoor=None) == (None, 0.0)
    assert await inputs(source, UNUSED) == (None, 0.0)
    assert weather.requests == 0


async def test_a_hung_request_is_given_up_on_after_30_s(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=4.0, cloud=0)
    weather.hung = True

    asking = await weather.start_until_hung(inputs(ForecastSource(hass, WEATHER)))
    assert weather.waiting == 1
    await let_time_pass(hass, freezer, 25)
    assert weather.waiting == 1
    await let_time_pass(hass, freezer, 6)
    assert weather.waiting == 0

    assert await asking == (None, 0.0)
    assert caplog.text.count("No hourly forecast from weather.forecast_home") == 1
    assert "no answer within 30 s" in caplog.text


async def test_after_a_failure_it_asks_again_at_the_next_refresh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=4.0, cloud=0)
    source = ForecastSource(hass, WEATHER)
    await give_up_on_forecast(hass, freezer, weather, inputs(source))

    freezer.tick(timedelta(minutes=5))
    assert await inputs(source) == (None, 0.0)
    assert weather.requests == 1

    freezer.move_to(MORNING + timedelta(minutes=30))
    await give_up_on_forecast(hass, freezer, weather, inputs(source))
    assert weather.requests == 2
    assert caplog.text.count("No hourly forecast from weather.forecast_home") == 1

    freezer.move_to(MORNING + timedelta(hours=1))
    ahead, _ = await inputs(source)
    assert weather.requests == 3
    assert ahead == pytest.approx(32 / 9, abs=0.01)
    assert "Hourly forecast from weather.forecast_home is back" in caplog.text


async def test_ask_now_skips_the_wait_for_the_refresh(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=4.0, cloud=0)
    weather.failing = True
    source = ForecastSource(hass, WEATHER)
    await inputs(source)
    weather.failing = False

    freezer.tick(timedelta(minutes=5))
    source.ask_now()
    ahead, _ = await inputs(source)

    assert weather.requests == 2
    assert ahead == pytest.approx(32 / 9, abs=0.01)


async def test_a_forecast_older_than_3_hours_is_dropped(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=4.0, cloud=0)
    source = ForecastSource(hass, WEATHER)
    await inputs(source)
    weather.failing = True

    # Still covered by the forecast fetched at MORNING, but that's too old by now.
    freezer.move_to(MORNING + timedelta(hours=2, minutes=55))
    assert (await inputs(source))[0] is not None
    freezer.move_to(MORNING + timedelta(hours=3, minutes=5))
    assert await inputs(source) == (None, 0.0)


@pytest.mark.parametrize(
    "items",
    [
        # Out of order, with an unusable entry.
        [
            {"datetime": (MORNING + timedelta(hours=hour)).isoformat(), "temperature": 4.0}
            for hour in (6, 2, 0, 4, 1, 5, 3)
        ]
        + [{"datetime": "not a time", "temperature": 30.0}],
        # Times as datetimes rather than text.
        [{"datetime": MORNING + timedelta(hours=hour), "temperature": 4.0} for hour in range(7)],
    ],
)
async def test_reads_the_forecast_entries(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, items: list[dict[str, Any]]
) -> None:
    freezer.move_to(MORNING)
    weather = provide_forecast(hass, temperature=0.0, cloud=0)
    weather.items = items

    ahead, _ = await inputs(ForecastSource(hass, WEATHER))

    assert ahead == pytest.approx(32 / 9, abs=0.01)
