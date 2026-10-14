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


def simulate(
    house: House,
    outdoor_at: Callable[[float], float],
    *,
    sun_at: Callable[[float], float] = sunny_afternoons,
    days: int = 14,
    settings: ControlSettings | None = None,
    start_setpoint: int = 26,
) -> Result:
    """Run the controller in write mode, as the integration does, every 5 minutes."""
    core = HeatingCore(
        settings or ControlSettings(),
        ControlState(room_target=ROOM_TARGET, write_enabled=True),
    )
    setpoint = start_setpoint
    result = Result(writes_per_day=[0] * days)

    for step in range(days * 86400 // STEP_SECONDS):
        now = step * STEP_SECONDS
        decision = core.step(
            Inputs(
                now=now,
                outdoor=round(outdoor_at(now / 3600), 1),
                indoor=round(house.room, 1),
                setpoint=setpoint,
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
