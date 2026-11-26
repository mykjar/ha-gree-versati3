"""What the pump's raw values mean for floor heating control."""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.gree_versati.heating.curve import Status
from custom_components.gree_versati.heating.pump import read_pump

# Heating the floor at 25 °C: water out 25.0 °C, back 22.0 °C.
HEATING: dict[str, Any] = {
    "HeWatOutTemSet": 25,
    "Pow": 1,
    "Mod": 1,
    "AllErr": 0,
    "SvSt": 0,
    "SyAnFroRunSta": 0,
    "AllOutWatTemHi": 125,
    "AllOutWatTemLo": 0,
    "AllInWatTemHi": 122,
    "AllInWatTemLo": 0,
}


def test_a_pump_heating_the_floor() -> None:
    reading = read_pump(HEATING)

    assert reading.setpoint == 25
    assert reading.hold is None
    assert reading.space_heating
    assert reading.delivering_heat


def test_no_data_means_unavailable() -> None:
    for data in (None, {}):
        reading = read_pump(data)
        assert reading.setpoint is None
        assert reading.hold is Status.PUMP_UNAVAILABLE
        assert not reading.space_heating
        assert not reading.delivering_heat


@pytest.mark.parametrize(
    ("changes", "hold"),
    [
        ({"Pow": 0}, Status.PUMP_OFF),
        ({"Pow": None}, Status.PUMP_OFF),
        ({"Mod": 2}, Status.NOT_HEATING),  # Cooling.
        ({"Mod": 3}, Status.NOT_HEATING),  # Hot water only.
        ({"Mod": 4}, None),  # Heat + hot water.
        ({"AllErr": 5}, Status.PUMP_ERROR),
        ({"AllErr": ""}, None),
        ({"AllErr": None}, None),
        ({"SvSt": 1}, Status.W_DEPEND_ON),
    ],
)
def test_holds(changes: dict[str, Any], hold: Status | None) -> None:
    assert read_pump({**HEATING, **changes}).hold is hold


@pytest.mark.parametrize(
    ("changes", "space_heating"),
    [
        ({"SyAnFroRunSta": 1}, False),  # Defrosting.
        ({"AllOutWatTemHi": 133}, True),  # 33 °C: 8 °C over is still the floor.
        ({"AllOutWatTemHi": 134}, False),  # 34 °C: 9 °C over is busy.
        ({"AllOutWatTemHi": 117}, True),  # 17 °C: 8 °C under.
        ({"AllOutWatTemHi": 145}, False),  # 45 °C: busy with hot water.
        ({"AllOutWatTemHi": None}, True),  # No flow reading: assume the floor.
        ({"HeWatOutTemSet": None}, True),
    ],
)
def test_space_heating(changes: dict[str, Any], space_heating: bool) -> None:
    assert read_pump({**HEATING, **changes}).space_heating is space_heating


@pytest.mark.parametrize(
    ("changes", "delivering"),
    [
        ({"AllInWatTemHi": 124, "AllInWatTemLo": 6}, False),  # Only 0.4 °C apart.
        ({"AllInWatTemHi": 124, "AllInWatTemLo": 4}, True),  # 0.6 °C apart.
        ({"AllInWatTemHi": None}, False),
        ({"Pow": 0}, False),
        ({"Mod": 2}, False),
        ({"AllErr": 5}, True),  # Still warm water while in error.
        ({"SvSt": 1}, True),  # Heating on the pump's own curve.
    ],
)
def test_delivering_heat(changes: dict[str, Any], delivering: bool) -> None:
    assert read_pump({**HEATING, **changes}).delivering_heat is delivering
