"""Option names and defaults for floor heating control."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from .curve import ControlSettings, CurveSettings

CONF_HEATING_ENABLED: Final = "heating_enabled"
CONF_INDOOR_SENSOR: Final = "heating_indoor_sensor"
CONF_OUTDOOR_SENSOR: Final = "heating_outdoor_sensor"
CONF_CURVE_COLD_OUTDOOR: Final = "heating_curve_cold_outdoor"
CONF_CURVE_COLD_FLOW: Final = "heating_curve_cold_flow"
CONF_CURVE_MILD_OUTDOOR: Final = "heating_curve_mild_outdoor"
CONF_CURVE_MILD_FLOW: Final = "heating_curve_mild_flow"
CONF_MIN_FLOW: Final = "heating_min_flow"
CONF_MAX_FLOW: Final = "heating_max_flow"
CONF_KP: Final = "heating_kp"
CONF_KI: Final = "heating_ki"
CONF_OUTDOOR_SMOOTHING_HOURS: Final = "heating_outdoor_smoothing_hours"
CONF_MANUAL_PAUSE_HOURS: Final = "heating_manual_pause_hours"

# Changing these only retunes the controller; anything else reloads the entry.
LIVE_OPTIONS: Final = frozenset(
    {
        CONF_CURVE_COLD_OUTDOOR,
        CONF_CURVE_COLD_FLOW,
        CONF_CURVE_MILD_OUTDOOR,
        CONF_CURVE_MILD_FLOW,
        CONF_MIN_FLOW,
        CONF_MAX_FLOW,
        CONF_KP,
        CONF_KI,
        CONF_OUTDOOR_SMOOTHING_HOURS,
        CONF_MANUAL_PAUSE_HOURS,
    }
)

UPDATE_INTERVAL: Final = timedelta(minutes=5)
# A reading this old counts as missing, even if the sensor still reports it.
STALE_AFTER: Final = timedelta(hours=12)
# Measured water this far from the setpoint means the pump is busy with
# something else (hot water, defrost), not heating the floor.
BUSY_FLOW_DIFFERENCE: Final = 8.0

MIN_ROOM_TARGET: Final = 16.0
MAX_ROOM_TARGET: Final = 26.0

STORAGE_VERSION: Final = 1

_DEFAULTS = ControlSettings()


def settings_from_options(options: Mapping[str, Any]) -> ControlSettings:
    """Build controller settings from the entry options, filling in defaults."""

    def value(key: str, default: float) -> float:
        return float(options.get(key, default))

    curve = _DEFAULTS.curve
    return ControlSettings(
        curve=CurveSettings(
            cold_outdoor=value(CONF_CURVE_COLD_OUTDOOR, curve.cold_outdoor),
            cold_flow=value(CONF_CURVE_COLD_FLOW, curve.cold_flow),
            mild_outdoor=value(CONF_CURVE_MILD_OUTDOOR, curve.mild_outdoor),
            mild_flow=value(CONF_CURVE_MILD_FLOW, curve.mild_flow),
        ),
        min_flow=value(CONF_MIN_FLOW, _DEFAULTS.min_flow),
        max_flow=value(CONF_MAX_FLOW, _DEFAULTS.max_flow),
        kp=value(CONF_KP, _DEFAULTS.kp),
        ki=value(CONF_KI, _DEFAULTS.ki),
        outdoor_smoothing_hours=value(
            CONF_OUTDOOR_SMOOTHING_HOURS, _DEFAULTS.outdoor_smoothing_hours
        ),
        manual_pause_hours=value(CONF_MANUAL_PAUSE_HOURS, _DEFAULTS.manual_pause_hours),
    )


def storage_key(entry_id: str) -> str:
    """Where the controller keeps its state between restarts."""
    return f"gree_versati.heating.{entry_id}"
