"""Tests for Hi/Lo measured temperature decoding."""

from __future__ import annotations

import pytest

from custom_components.gree_versati.temperature import (
    decode_measured_temperature,
    decode_temperature,
)


@pytest.mark.parametrize(
    ("hi", "lo", "expected"),
    [
        (132, 4, 32.4),
        (132, 2, 32.2),
        (100, 0, 0.0),
        (98, 5, -1.5),
        (155, 9, 55.9),
        ("135", "7", 35.7),
    ],
)
def test_decode_temperature(hi: object, lo: object, expected: float) -> None:
    assert decode_temperature(hi, lo) == expected


@pytest.mark.parametrize(
    ("hi", "lo"),
    [
        (70, 0),  # sensor not fitted (-30.0 °C)
        (None, 2),
        (132, None),
        (132, 10),
        (132, -1),
        ("abc", 2),
        (True, 2),
        (132, False),
    ],
)
def test_decode_temperature_invalid(hi: object, lo: object) -> None:
    assert decode_temperature(hi, lo) is None


def test_decode_measured_temperature_from_data() -> None:
    data = {"AllOutWatTemHi": 132, "AllOutWatTemLo": 2, "AllInWatTemHi": 132}
    assert decode_measured_temperature(data, "AllOutWatTem") == 32.2
    assert decode_measured_temperature(data, "AllInWatTem") is None
    assert decode_measured_temperature(data, "WatBoxTem") is None
