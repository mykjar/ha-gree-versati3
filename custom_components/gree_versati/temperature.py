"""Decoding of measured temperatures reported by Gree Versati devices."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .constants import (
    TEMP_HI_OFFSET,
    TEMP_HI_SUFFIX,
    TEMP_LO_SCALE,
    TEMP_LO_SUFFIX,
    TEMP_NOT_FITTED,
)


def decode_temperature(hi: Any, lo: Any) -> float | None:
    """Decode a Hi/Lo parameter pair into degrees Celsius.

    The device reports whole degrees offset by 100 in ``Hi`` and tenths in
    ``Lo``: ``(Hi - 100) + Lo * 0.1``. Returns ``None`` when either part is
    missing or malformed, or when the device reports the "sensor not fitted"
    value (-30.0 °C).
    """
    if hi is None or lo is None or isinstance(hi, bool) or isinstance(lo, bool):
        return None
    try:
        hi_int = int(hi)
        lo_int = int(lo)
    except (TypeError, ValueError):
        return None
    if not 0 <= lo_int <= 9:
        return None

    value = round((hi_int - TEMP_HI_OFFSET) + lo_int * TEMP_LO_SCALE, 1)
    if value <= TEMP_NOT_FITTED:
        return None
    return value


def decode_measured_temperature(data: Mapping[str, Any], param: str) -> float | None:
    """Decode the measured temperature ``param`` from coordinator data."""
    return decode_temperature(
        data.get(f"{param}{TEMP_HI_SUFFIX}"),
        data.get(f"{param}{TEMP_LO_SUFFIX}"),
    )
