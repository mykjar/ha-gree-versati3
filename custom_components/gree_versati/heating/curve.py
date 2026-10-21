"""Floor heating control maths.

Pure Python with no Home Assistant imports, so it can be unit-tested and run
against a simulated house. The integration feeds it readings every few
minutes and carries out the writes it asks for.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """What the controller is doing, or why it is leaving the pump alone."""

    ACTIVE = "active"
    ADVISORY = "advisory"
    PAUSED = "paused"
    MANUAL_OVERRIDE = "manual_override"
    RATE_LIMITED = "rate_limited"
    SENSOR_UNAVAILABLE = "sensor_unavailable"
    PUMP_UNAVAILABLE = "pump_unavailable"
    PUMP_OFF = "pump_off"
    NOT_HEATING = "not_heating"
    PUMP_ERROR = "pump_error"
    W_DEPEND_ON = "w_depend_on"
    WRITE_FAILED = "write_failed"
    WRITE_IGNORED = "write_ignored"


@dataclass(frozen=True, kw_only=True)
class CurveSettings:
    """Two-point heating curve, defined at a reference room temperature.

    Beyond the two points the curve stays flat, as the pump's own
    weather-dependent curve does.
    """

    cold_outdoor: float = -20.0
    cold_flow: float = 30.0
    mild_outdoor: float = 15.0
    mild_flow: float = 22.0
    reference_room: float = 21.0

    @property
    def slope(self) -> float:
        """Degrees of water per degree of outdoor temperature."""
        return (self.cold_flow - self.mild_flow) / (self.mild_outdoor - self.cold_outdoor)

    def flow(self, outdoor: float, room_target: float) -> float:
        """Water temperature the curve asks for."""
        outdoor = min(max(outdoor, self.cold_outdoor), self.mild_outdoor)
        base = self.cold_flow - self.slope * (outdoor - self.cold_outdoor)
        # Water above room temperature is proportional to room above outdoor
        # temperature, so each degree of room target needs 1 + slope degrees.
        return base + (1 + self.slope) * (room_target - self.reference_room)


@dataclass(frozen=True, kw_only=True)
class ControlSettings:
    """Tuning and safety limits. Temperatures in °C, water per °C of room."""

    # Defaults tuned in tests/test_heating_simulation.py.
    curve: CurveSettings = field(default_factory=CurveSettings)
    kp: float = 2.5
    ki: float = 0.1  # per hour
    learned_limit: float = 3.0
    correction_limit: float = 4.0
    min_flow: float = 20.0
    max_flow: float = 30.0
    outdoor_smoothing_hours: float = 1.0
    hysteresis: float = 0.25
    max_step: int = 1
    write_interval_minutes: float = 30.0
    manual_pause_hours: float = 6.0
    settle_hours: float = 6.0
    write_confirm_seconds: float = 180.0
    max_learning_seconds: float = 900.0
    # Forecast use (forecast.py); the integration applies them through Inputs.
    lookahead_hours: float = 4.0
    sun_cut_max: float = 2.0
    sun_window_hours: tuple[float, float] = (2.0, 6.0)


@dataclass(kw_only=True)
class ControlState:
    """Everything the controller remembers between steps and restarts."""

    room_target: float = 21.0
    active: bool = True
    write_enabled: bool = False
    learning_enabled: bool = True
    learned: float = 0.0
    smoothed_outdoor: float | None = None
    last_step_at: float | None = None
    room_target_changed_at: float | None = None
    expected_setpoint: int | None = None
    write_pending_since: float | None = None
    setpoint_before_write: int | None = None
    last_attempt_at: float | None = None
    last_write_failed: bool = False
    last_write_ignored: bool = False
    override_until: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the state in a JSON-friendly form."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ControlState:
        """Restore saved state, ignoring keys this version doesn't know."""
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True, kw_only=True)
class Inputs:
    """One reading of everything the controller looks at.

    ``hold`` is a pump-side reason not to write, decided by the integration.
    ``space_heating`` is False while the pump is busy with something else
    (defrost, a hot-water cycle), when room readings say little about the curve.
    ``outdoor_ahead`` is the outdoor temperature looking ahead with the forecast,
    used by the curve instead of the reading. ``sun_cut`` lowers the curve
    ahead of forecast sun.
    """

    now: float
    outdoor: float | None
    indoor: float | None
    setpoint: int | None
    hold: Status | None = None
    space_heating: bool = True
    outdoor_ahead: float | None = None
    sun_cut: float = 0.0


@dataclass(frozen=True, kw_only=True)
class Decision:
    """Result of one step. ``write`` is None when the pump should be left alone."""

    status: Status
    outdoor: float | None
    curve_flow: float | None
    correction: float | None
    learned: float
    target_flow: float | None
    suggested_setpoint: int | None
    write: int | None
    sun_cut: float = 0.0
    outdoor_ahead: float | None = None


def round_half_up(value: float) -> int:
    """Round to the nearest whole degree, halves upwards."""
    return math.floor(value + 0.5)


def smooth(previous: float | None, value: float, elapsed: float, hours: float) -> float:
    """Exponential moving average with a time constant in hours (0 = off)."""
    if previous is None or hours <= 0:
        return value
    alpha = 1 - math.exp(-max(elapsed, 0.0) / (hours * 3600))
    return previous + alpha * (value - previous)


def next_setpoint(target: float, current: int, settings: ControlSettings) -> int:
    """Whole-degree setpoint to move to, or ``current`` when no change is due.

    A setpoint outside the limits goes straight back inside them. Otherwise
    the target must clear rounding plus hysteresis before anything changes,
    and each change is at most ``max_step`` degrees.
    """
    low = math.ceil(settings.min_flow)
    high = math.floor(settings.max_flow)
    if current > high:
        return high
    if current < low:
        return low
    if abs(target - current) < 0.5 + settings.hysteresis:
        return current
    desired = min(max(round_half_up(target), low), high)
    step = max(-settings.max_step, min(settings.max_step, desired - current))
    return current + step


def calibrated_curve(
    curve: CurveSettings,
    *,
    outdoor: float,
    indoor: float,
    room_target: float,
    setpoint: float,
) -> CurveSettings:
    """Shift the curve so it matches the setpoint the house runs on today.

    The current setpoint holds the room at ``indoor``. Holding ``room_target``
    instead needs 1 + slope degrees of water per degree of difference.
    """
    needed = setpoint + (1 + curve.slope) * (room_target - indoor)
    offset = needed - curve.flow(outdoor, room_target)
    return replace(
        curve,
        cold_flow=round(curve.cold_flow + offset, 1),
        mild_flow=round(curve.mild_flow + offset, 1),
    )


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


class HeatingCore:
    """Turns readings into a water setpoint, one step every few minutes."""

    def __init__(self, settings: ControlSettings, state: ControlState | None = None) -> None:
        self.settings = settings
        self.state = state if state is not None else ControlState()

    def set_room_target(self, value: float, now: float) -> None:
        """Change the room target; learning waits while the floor catches up."""
        if value != self.state.room_target:
            self.state.room_target = value
            self.state.room_target_changed_at = now

    def set_active(self, active: bool) -> None:
        """Pause or resume control. Resuming also ends a manual-change pause."""
        if active and not self.state.active:
            self.state.override_until = None
        self.state.active = active

    def set_write_enabled(self, enabled: bool) -> None:
        """Switch between advisory mode and writing to the pump."""
        self.state.write_enabled = enabled

    def set_learning_enabled(self, enabled: bool) -> None:
        """Turn the slow learned offset on or off; it is kept either way."""
        self.state.learning_enabled = enabled

    def resume(self) -> None:
        """End a manual-change pause now."""
        self.state.override_until = None

    def reset_learning(self) -> None:
        """Forget the learned offset."""
        self.state.learned = 0.0

    def record_write(self, value: int, previous: int | None, now: float) -> None:
        """The pump accepted a write; a later step checks that it took effect."""
        state = self.state
        state.last_attempt_at = now
        state.last_write_failed = False
        state.expected_setpoint = value
        state.write_pending_since = now
        state.setpoint_before_write = previous

    def record_write_failure(self, now: float) -> None:
        """A write raised an error; retry after the usual interval."""
        self.state.last_attempt_at = now
        self.state.last_write_failed = True

    def step(self, inputs: Inputs) -> Decision:
        """Work out the target and whether to write it now."""
        state, settings = self.state, self.settings
        now = inputs.now
        elapsed = 0.0 if state.last_step_at is None else max(now - state.last_step_at, 0.0)
        state.last_step_at = now

        self._track_setpoint(inputs.setpoint, now)
        if state.override_until is not None and now >= state.override_until:
            state.override_until = None

        outdoor: float | None = None
        if inputs.outdoor is not None:
            reading = inputs.outdoor if inputs.outdoor_ahead is None else inputs.outdoor_ahead
            state.smoothed_outdoor = smooth(
                state.smoothed_outdoor,
                reading,
                elapsed,
                settings.outdoor_smoothing_hours,
            )
            outdoor = state.smoothed_outdoor

        curve_flow: float | None = None
        correction: float | None = None
        target: float | None = None
        if outdoor is not None and inputs.indoor is not None:
            curve_flow = settings.curve.flow(outdoor, state.room_target)
            lowered = curve_flow - inputs.sun_cut
            error = state.room_target - inputs.indoor
            if self._learning_allowed(inputs, now, lowered, error):
                hours = min(elapsed, settings.max_learning_seconds) / 3600
                state.learned = _clamp(
                    state.learned + settings.ki * error * hours, settings.learned_limit
                )
            correction = _clamp(settings.kp * error + state.learned, settings.correction_limit)
            target = min(max(lowered + correction, settings.min_flow), settings.max_flow)

        suggested: int | None = None
        if target is not None and inputs.setpoint is not None:
            suggested = next_setpoint(target, inputs.setpoint, settings)

        status = self._status(inputs, now, target, suggested)
        write = suggested if status is Status.ACTIVE and suggested != inputs.setpoint else None
        return Decision(
            status=status,
            outdoor=outdoor,
            curve_flow=curve_flow,
            correction=correction,
            learned=state.learned,
            target_flow=target,
            suggested_setpoint=suggested,
            write=write,
            sun_cut=inputs.sun_cut,
            outdoor_ahead=inputs.outdoor_ahead,
        )

    def _track_setpoint(self, observed: int | None, now: float) -> None:
        """Tell our own writes apart from changes made on the pump or in apps."""
        state, settings = self.state, self.settings
        if observed is None:
            return

        if state.write_pending_since is not None:
            if observed == state.expected_setpoint:
                state.write_pending_since = None
                state.setpoint_before_write = None
                state.last_write_ignored = False
                return
            if now - state.write_pending_since < settings.write_confirm_seconds:
                return
            before = state.setpoint_before_write
            state.write_pending_since = None
            state.setpoint_before_write = None
            if observed == before:
                state.last_write_ignored = True
                state.expected_setpoint = observed
                return

        if (
            state.expected_setpoint is not None
            and observed != state.expected_setpoint
            and state.active
            and state.write_enabled
        ):
            state.override_until = now + settings.manual_pause_hours * 3600
        state.expected_setpoint = observed

    def _learning_allowed(
        self, inputs: Inputs, now: float, curve_flow: float, error: float
    ) -> bool:
        state, settings = self.state, self.settings
        if not (state.learning_enabled and state.active and state.write_enabled):
            return False
        if state.override_until is not None or inputs.hold is not None:
            return False
        if not inputs.space_heating:
            return False
        if (
            state.room_target_changed_at is not None
            and now - state.room_target_changed_at < settings.settle_hours * 3600
        ):
            return False

        # Anti-windup: stop pushing once the output can't follow any further.
        raw = settings.kp * error + state.learned
        if error > 0 and (
            raw >= settings.correction_limit
            or curve_flow + _clamp(raw, settings.correction_limit) >= settings.max_flow
        ):
            return False
        if error < 0 and (
            raw <= -settings.correction_limit
            or curve_flow + _clamp(raw, settings.correction_limit) <= settings.min_flow
        ):
            return False
        return True

    def _status(
        self,
        inputs: Inputs,
        now: float,
        target: float | None,
        suggested: int | None,
    ) -> Status:
        state = self.state
        if not state.active:
            return Status.PAUSED
        if state.override_until is not None:
            return Status.MANUAL_OVERRIDE
        if inputs.hold is not None:
            return inputs.hold
        if target is None:
            return Status.SENSOR_UNAVAILABLE
        if inputs.setpoint is None or suggested is None:
            return Status.PUMP_UNAVAILABLE
        if not state.write_enabled:
            return Status.ADVISORY
        if suggested == inputs.setpoint:
            return Status.ACTIVE
        if (
            state.last_attempt_at is not None
            and now - state.last_attempt_at < self.settings.write_interval_minutes * 60
        ):
            if state.last_write_failed:
                return Status.WRITE_FAILED
            if state.last_write_ignored:
                return Status.WRITE_IGNORED
            return Status.RATE_LIMITED
        return Status.ACTIVE
