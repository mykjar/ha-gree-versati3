"""What the pump's raw values mean for floor heating control.

Pure Python with no Home Assistant imports, like curve.py: the controller
hands over the pump coordinator's data and gets a reading back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ..constants import (
    MODE_OPTIONS,
    PARAM_ALL_ERR,
    PARAM_ALL_IN_WAT_TEM,
    PARAM_ALL_OUT_WAT_TEM,
    PARAM_HE_WAT_OUT_TEM_SET,
    PARAM_MOD,
    PARAM_POW,
    PARAM_SV_ST,
    PARAM_SY_AN_FRO_RUN_STA,
)
from ..temperature import decode_measured_temperature
from .curve import Status

# Values a reading needs beyond the pump's usual poll.
STATUS_POLL_KEYS: Final[tuple[str, ...]] = (PARAM_SV_ST, PARAM_SY_AN_FRO_RUN_STA)

HEATING_MODES: Final = frozenset({MODE_OPTIONS["Heat"], MODE_OPTIONS["Heat + hot water"]})
# Measured water this far from the setpoint means the pump is busy with
# something else (hot water, defrost), not heating the floor.
BUSY_FLOW_DIFFERENCE: Final = 8.0
# Water out this much warmer than water back means heat is going in.
DELIVERING_DIFFERENCE: Final = 0.5

# Holds after which the pump isn't heating at all, whatever the water does.
_NOT_RUNNING = frozenset({Status.PUMP_UNAVAILABLE, Status.PUMP_OFF, Status.NOT_HEATING})


@dataclass(frozen=True)
class PumpReading:
    """The pump as floor heating control sees it. Temperatures in °C."""

    setpoint: int | None
    # Pump-side reason not to change the setpoint, if there is one.
    hold: Status | None
    # False while the pump is defrosting or busy with hot water.
    space_heating: bool
    flow: float | None
    inlet: float | None

    @property
    def delivering_heat(self) -> bool:
        """Whether the pump is putting heat into the floor right now."""
        if self.hold in _NOT_RUNNING or self.flow is None or self.inlet is None:
            return False
        return self.flow > self.inlet + DELIVERING_DIFFERENCE


def read_pump(data: Mapping[str, Any] | None) -> PumpReading:
    """Read the pump coordinator's data; None or empty means unavailable."""
    if not data:
        return PumpReading(
            setpoint=None,
            hold=Status.PUMP_UNAVAILABLE,
            space_heating=False,
            flow=None,
            inlet=None,
        )
    setpoint = _as_int(data.get(PARAM_HE_WAT_OUT_TEM_SET))
    flow = decode_measured_temperature(data, PARAM_ALL_OUT_WAT_TEM)
    return PumpReading(
        setpoint=setpoint,
        hold=_hold(data),
        space_heating=_space_heating(data, setpoint, flow),
        flow=flow,
        inlet=decode_measured_temperature(data, PARAM_ALL_IN_WAT_TEM),
    )


def _hold(data: Mapping[str, Any]) -> Status | None:
    if _as_int(data.get(PARAM_POW)) != 1:
        return Status.PUMP_OFF
    if _as_int(data.get(PARAM_MOD)) not in HEATING_MODES:
        return Status.NOT_HEATING
    error = data.get(PARAM_ALL_ERR)
    if error not in (None, "") and _as_int(error) != 0:
        return Status.PUMP_ERROR
    if _as_int(data.get(PARAM_SV_ST)) == 1:
        return Status.W_DEPEND_ON
    return None


def _space_heating(data: Mapping[str, Any], setpoint: int | None, flow: float | None) -> bool:
    if _as_int(data.get(PARAM_SY_AN_FRO_RUN_STA)) == 1:
        return False
    if flow is not None and setpoint is not None:
        return abs(flow - setpoint) <= BUSY_FLOW_DIFFERENCE
    return True


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
