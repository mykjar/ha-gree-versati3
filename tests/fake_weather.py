"""A weather entity's hourly forecast service for floor heating tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse

WEATHER = "weather.forecast_home"
# Clear-sky midday falls 2-6 h after this in the test location (San Diego).
MORNING = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)


class FakeWeather:
    """A weather entity's hourly forecast service, which can stop answering."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.requests = 0
        self.hung = False
        self.failing = False
        self.waiting = 0  # Requests it hasn't answered yet.
        self._request_hung = asyncio.Event()

    async def get_forecasts(self, call: ServiceCall) -> ServiceResponse:
        self.requests += 1
        if self.failing:
            raise RuntimeError("weather service is down")
        if self.hung:
            self.waiting += 1
            self._request_hung.set()
            try:
                await asyncio.get_running_loop().create_future()  # Never answers.
            finally:
                self.waiting -= 1
        return {WEATHER: {"forecast": self.items}}

    async def start_until_hung[T](self, step: Awaitable[T]) -> asyncio.Future[T]:
        """Start a step and return once it waits for a hung request (or ends)."""
        self._request_hung.clear()
        # Home Assistant doesn't track this task, so it can wait for everything else.
        task = asyncio.ensure_future(step)
        hung = asyncio.ensure_future(self._request_hung.wait())
        await asyncio.wait({task, hung}, return_when=asyncio.FIRST_COMPLETED)
        hung.cancel()
        return task


def provide_forecast(
    hass: HomeAssistant, *, temperature: float, cloud: float, unit: str = "°C"
) -> FakeWeather:
    """A weather entity whose hourly forecast stays the same for 12 hours."""
    hass.states.async_set(WEATHER, "sunny", {"temperature_unit": unit})
    weather = FakeWeather(
        [
            {
                "datetime": (MORNING + timedelta(hours=hour)).isoformat(),
                "temperature": temperature,
                "cloud_coverage": cloud,
            }
            for hour in range(13)
        ]
    )
    hass.services.async_register(
        "weather",
        "get_forecasts",
        weather.get_forecasts,
        supports_response=SupportsResponse.ONLY,
    )
    return weather


async def give_up_on_forecast[T](
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, weather: FakeWeather, step: Awaitable[T]
) -> T:
    """Run a step while the forecast hangs, until it carries on without it."""
    weather.hung = True
    task = await weather.start_until_hung(step)
    await let_time_pass(hass, freezer, 31)
    weather.hung = False
    # Fail rather than wait forever if the step never gives up.
    assert weather.waiting == 0
    return await task


async def let_time_pass(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float
) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
