"""Run the floor heating controller against a simulated house.

The house is two heat stores: the concrete floor, warmed by the water, and the
room, warmed by the floor and the sun and cooled by the outdoors. The numbers
are for a well-insulated house of about 150 m² with a screed floor. They don't
need to match any real house, only to behave like one, so the defaults can be
tuned and checked for stability over a simulated fortnight.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field

import pytest

from custom_components.gree_versati.heating.curve import (
    ControlSettings,
    ControlState,
    HeatingCore,
    Inputs,
)
from custom_components.gree_versati.heating.forecast import (
    HourlyForecast,
    cloud_at,
    lookahead_outdoor,
    sun_score,
    window_times,
)

STEP_SECONDS = 300
STEPS_PER_DAY = 86400 // STEP_SECONDS
SUBSTEP_SECONDS = 60
WARM_UP_DAYS = 2
ROOM_TARGET = 21.0


@dataclass
class House:
    """Two-store model; heat flows in W, capacities in J/K."""

    loss: float = 150.0  # W/K, room to outdoors
    floor_to_room: float = 1500.0  # W/K
    water_to_floor: float = 1500.0  # W/K
    room_capacity: float = 1.0e7  # air, furniture and inner walls
    floor_capacity: float = 2.1e7  # about 7 cm of screed
    internal_gains: float = 300.0  # people and appliances
    room: float = ROOM_TARGET
    floor: float = 23.0

    def advance(self, seconds: float, outdoor: float, setpoint: int, solar: float) -> None:
        # The pump holds the outlet at the setpoint; the mean water temperature
        # in the loops is a little lower. It never cools the floor.
        water = setpoint - 1.0
        heat = max(self.water_to_floor * (water - self.floor), 0.0)
        to_room = self.floor_to_room * (self.floor - self.room)
        loss = self.loss * (self.room - outdoor)
        self.floor += seconds * (heat - to_room) / self.floor_capacity
        self.room += (
            seconds * (to_room + self.internal_gains + solar - loss) / self.room_capacity
        )


def _interpolate(points: list[tuple[float, float]], x: float) -> float:
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


def winter_fortnight(hours: float) -> float:
    """Mild days, a four-day cold snap from day 5, then mild again.

    Each day swings ±4 °C around its base, coldest before dawn.
    """
    base = _interpolate(
        [(0, 2.0), (5, 2.0), (5.5, -12.0), (9, -12.0), (9.5, 0.0), (14, 0.0)],
        hours / 24,
    )
    return base + 4.0 * math.sin(2 * math.pi * ((hours % 24) - 9) / 24)


def sunny_afternoons(hours: float) -> float:
    """Up to 1.5 kW of sun through the windows on days 2, 3, 10 and 11."""
    day, hour = divmod(hours, 24)
    if int(day) not in (2, 3, 10, 11) or not 11 <= hour <= 16:
        return 0.0
    return 1500.0 * math.sin(math.pi * (hour - 11) / 5)


def no_sun(hours: float) -> float:
    return 0.0


def sun_shape(hour_of_day: float) -> float:
    """Clear-sky sun through south windows on an autumn day, 0..1."""
    if not 9 <= hour_of_day <= 16:
        return 0.0
    return math.sin(math.pi * (hour_of_day - 9) / 7)


# Average clear-sky value over the sunniest stretch the sun window can cover.
_WINDOW = ControlSettings().sun_window_hours
_SAMPLES = int(round((_WINDOW[1] - _WINDOW[0]) * 2)) + 1
BEST_CLEAR = sum(sun_shape(12.5 + (i - (_SAMPLES - 1) / 2) / 2) for i in range(_SAMPLES)) / _SAMPLES


def forecast_inputs(
    forecast: list[HourlyForecast], now: float, outdoor: float, settings: ControlSettings
) -> tuple[float | None, float]:
    """Look-ahead temperature and sun cut, worked out as the integration does."""
    ahead = lookahead_outdoor(outdoor, forecast, now, settings.lookahead_hours)
    window = []
    for at in window_times(now, *settings.sun_window_hours):
        cloud = cloud_at(forecast, at)
        if cloud is None:
            return ahead, 0.0
        window.append((sun_shape((at / 3600) % 24), cloud))
    return ahead, settings.sun_cut_max * sun_score(window, BEST_CLEAR)


@dataclass
class ForecastWeather:
    """What the weather does, and what its forecast says it will do."""

    outdoor: Callable[[float], float]
    sun: list[float]  # per day, 0 overcast .. 1 clear
    forecast_sun: list[float]
    temperature_bias: list[float]  # per day, °C the forecast is off by
    peak_w: float
    seed: int

    def solar(self, hours: float) -> float:
        day, hour = divmod(hours, 24)
        # Passing clouds: up to 30 % either side of the day's sunshine.
        passing = 1 + 0.3 * math.sin(hours * 2.3 + self.seed)
        sunshine = min(max(self.sun[int(day) % len(self.sun)] * passing, 0.0), 1.0)
        return self.peak_w * sunshine * sun_shape(hour)

    def forecast(self, now: float) -> list[HourlyForecast]:
        """Hourly forecast issued at `now`: errors grow with lead time."""
        first = now // 3600 * 3600
        hours = []
        for step in range(13):
            start = first + step * 3600
            day = int(start // 86400) % len(self.sun)
            lead = min((start - now) / 3600 / 12, 1.0)
            hours.append(
                HourlyForecast(
                    start=start,
                    temperature=self.outdoor(start / 3600) + self.temperature_bias[day] * lead,
                    cloud=1 - self.forecast_sun[day],
                )
            )
        return hours


def forecast_weather(
    outdoor: Callable[[float], float],
    sun: list[float],
    peak_w: float,
    seed: int,
    wrong_share: float = 0.05,
) -> ForecastWeather:
    """Forecasts off by ~0.2 in sunshine and ~1.5 °C at 12 h; some days plain wrong."""
    rng = random.Random(seed)
    forecast_sun = [
        1 - day if rng.random() < wrong_share else min(max(day + rng.gauss(0, 0.2), 0), 1)
        for day in sun
    ]
    bias = [rng.gauss(0, 1.5) for _ in sun]
    return ForecastWeather(outdoor, sun, forecast_sun, bias, peak_w, seed)


def winter_with_forecast(seed: int) -> ForecastWeather:
    """The winter fortnight, sunny on days 2, 3, 10 and 11."""
    sun = [1.0 if day in (2, 3, 10, 11) else 0.0 for day in range(14)]
    return forecast_weather(winter_fortnight, sun, 1500.0, seed)


def sunny_autumn(seed: int, wrong_share: float = 0.05) -> ForecastWeather:
    """Mild days, sun most of them, a house with big south windows."""
    rng = random.Random(seed)
    sun = [rng.choice([0.0, 0.2, 0.5, 0.8, 1.0, 1.0]) for _ in range(14)]

    def outdoor(hours: float) -> float:
        base = 6 + 3 * math.sin(hours / 24 / 2.3)
        return base + 4 * math.sin(2 * math.pi * ((hours % 24) - 9) / 24)

    return forecast_weather(outdoor, sun, 2500.0, seed, wrong_share)


def cold_and_clear(seed: int) -> ForecastWeather:
    """High-pressure winter: clear, cold, and low sun that gives little heat."""
    rng = random.Random(seed)
    sun = [rng.choice([0.8, 0.9, 1.0]) for _ in range(14)]

    def outdoor(hours: float) -> float:
        return -12 + 5 * math.sin(2 * math.pi * ((hours % 24) - 9) / 24)

    return forecast_weather(outdoor, sun, 1000.0, seed)


@dataclass
class Result:
    rooms: list[float] = field(default_factory=list)
    setpoints: list[int] = field(default_factory=list)
    writes_per_day: list[int] = field(default_factory=list)
    learned: float = 0.0

    @property
    def comfortable_share(self) -> float:
        """Share of the time the room is within ±0.5 °C of target."""
        return sum(abs(room - ROOM_TARGET) <= 0.5 for room in self.rooms) / len(self.rooms)

    @property
    def too_warm(self) -> float:
        """Degree-hours more than 0.5 °C above target."""
        return sum(max(room - ROOM_TARGET - 0.5, 0) for room in self.rooms) * STEP_SECONDS / 3600

    @property
    def too_cold(self) -> float:
        """Degree-hours more than 0.5 °C below target."""
        return sum(max(ROOM_TARGET - 0.5 - room, 0) for room in self.rooms) * STEP_SECONDS / 3600


def simulate(
    house: House,
    outdoor_at: Callable[[float], float],
    *,
    sun_at: Callable[[float], float] = sunny_afternoons,
    days: int = 14,
    settings: ControlSettings | None = None,
    start_setpoint: int = 26,
    forecast: Callable[[float], list[HourlyForecast]] | None = None,
) -> Result:
    """Run the controller in write mode, as the integration does, every 5 minutes.

    With a `forecast` (the hourly forecast issued at a given time) it looks
    ahead and lowers the curve for sun, through the same code as the
    integration.
    """
    settings = settings or ControlSettings()
    core = HeatingCore(settings, ControlState(room_target=ROOM_TARGET, write_enabled=True))
    setpoint = start_setpoint
    result = Result(writes_per_day=[0] * days)

    for step in range(days * 86400 // STEP_SECONDS):
        now = step * STEP_SECONDS
        outdoor = round(outdoor_at(now / 3600), 1)
        outdoor_ahead, sun_cut = None, 0.0
        if forecast is not None:
            outdoor_ahead, sun_cut = forecast_inputs(forecast(now), now, outdoor, settings)
        decision = core.step(
            Inputs(
                now=now,
                outdoor=outdoor,
                indoor=round(house.room, 1),
                setpoint=setpoint,
                outdoor_ahead=outdoor_ahead,
                sun_cut=sun_cut,
            )
        )
        if decision.write is not None:
            core.record_write(decision.write, setpoint, now)
            setpoint = decision.write
            result.writes_per_day[now // 86400] += 1

        for sub in range(STEP_SECONDS // SUBSTEP_SECONDS):
            hours = (now + sub * SUBSTEP_SECONDS) / 3600
            house.advance(SUBSTEP_SECONDS, outdoor_at(hours), setpoint, sun_at(hours))

        if now >= WARM_UP_DAYS * 86400:
            result.rooms.append(house.room)
            result.setpoints.append(setpoint)

    result.learned = core.state.learned
    return result


def no_weather(hours: float) -> float:
    return 0.0


def with_noise(outdoor_at: Callable[[float], float], seed: int) -> Callable[[float], float]:
    """Add ±1 °C of sensor noise, a new value every 5 minutes."""
    rng = random.Random(seed)
    samples: dict[int, float] = {}

    def noisy(hours: float) -> float:
        sample = round(hours * 3600 / STEP_SECONDS)
        if sample not in samples:
            samples[sample] = rng.uniform(-1.0, 1.0)
        return outdoor_at(hours) + samples[sample]

    return noisy


# Loss 150 W/K roughly matches the default curve; 125 and 180 make the curve
# about 15 % too warm and 20 % too cold, which the learned offset must absorb.
@pytest.mark.parametrize("loss", [125.0, 150.0, 180.0])
def test_winter_fortnight_stays_comfortable(loss: float) -> None:
    result = simulate(House(loss=loss), winter_fortnight)

    # The only misses are sunny afternoons: a warm floor keeps heating for
    # hours whatever the setpoint does.
    assert result.comfortable_share >= 0.95
    assert max(result.writes_per_day) <= 12


@pytest.mark.parametrize(("loss", "direction"), [(125.0, -1), (180.0, 1)])
def test_learning_corrects_a_curve_that_is_off(loss: float, direction: int) -> None:
    result = simulate(House(loss=loss), winter_fortnight)

    assert direction * result.learned > 0.2


@pytest.mark.parametrize("loss", [130.0, 170.0])
def test_steady_weather_settles_without_oscillating(loss: float) -> None:
    result = simulate(House(loss=loss), no_weather, sun_at=no_sun, days=10)
    settled = slice((4 - WARM_UP_DAYS) * STEPS_PER_DAY, None)

    # Whole-degree setpoints can't hold an in-between water temperature, so the
    # controller may alternate between two neighbours, but never wider.
    setpoints = result.setpoints[settled]
    assert max(setpoints) - min(setpoints) <= 1
    assert all(abs(room - ROOM_TARGET) <= 0.3 for room in result.rooms[settled])
    assert max(result.writes_per_day[4:]) <= 6


@pytest.mark.parametrize("loss", [125.0, 150.0, 180.0])
def test_noisy_outdoor_sensor(loss: float) -> None:
    result = simulate(House(loss=loss), with_noise(winter_fortnight, seed=int(loss)))

    assert result.comfortable_share >= 0.95
    assert max(result.writes_per_day) <= 12


# Weather forecasts: looking ahead and lowering the curve for sun.

FORECAST_SCENARIOS: dict[str, Callable[[int], ForecastWeather]] = {
    "winter fortnight": winter_with_forecast,
    "sunny autumn": sunny_autumn,
    "sunny autumn, 30 % of days forecast wrong": lambda seed: sunny_autumn(seed, wrong_share=0.3),
    "cold and clear": cold_and_clear,
}


@pytest.mark.parametrize("scenario", list(FORECAST_SCENARIOS))
@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize("loss", [125.0, 150.0, 180.0])
def test_forecast_keeps_the_room_steadier(scenario: str, seed: int, loss: float) -> None:
    weather = FORECAST_SCENARIOS[scenario](seed)

    without = simulate(House(loss=loss), weather.outdoor, sun_at=weather.solar)
    ahead = simulate(
        House(loss=loss), weather.outdoor, sun_at=weather.solar, forecast=weather.forecast
    )

    assert ahead.comfortable_share >= without.comfortable_share + 0.02
    # Never colder, even when a forecast promises sun that doesn't come.
    assert ahead.too_cold <= without.too_cold + 0.05
    assert ahead.too_warm <= without.too_warm + 0.1
    assert max(ahead.writes_per_day) <= 12


def test_forecast_cuts_overheating_on_sunny_days() -> None:
    too_warm_without = too_warm_ahead = 0.0
    for seed in (0, 1):
        weather = sunny_autumn(seed)
        too_warm_without += simulate(House(), weather.outdoor, sun_at=weather.solar).too_warm
        too_warm_ahead += simulate(
            House(), weather.outdoor, sun_at=weather.solar, forecast=weather.forecast
        ).too_warm

    assert too_warm_ahead <= 0.4 * too_warm_without


def test_without_a_usable_forecast_nothing_changes() -> None:
    weather = sunny_autumn(0)

    without = simulate(House(), weather.outdoor, sun_at=weather.solar)
    empty = simulate(House(), weather.outdoor, sun_at=weather.solar, forecast=lambda now: [])

    assert empty.rooms == without.rooms
