"""Unit tests for the floor heating control maths."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.gree_versati.heating.curve import (
    ControlSettings,
    ControlState,
    CurveSettings,
    HeatingCore,
    Inputs,
    Status,
    calibrated_curve,
    next_setpoint,
    round_half_up,
    smooth,
)

NOW = 1_800_000_000.0
MINUTE = 60.0
HOUR = 3600.0

# Without outdoor smoothing every reading counts in full, which keeps the
# expected numbers exact.
SETTINGS = ControlSettings(outdoor_smoothing_hours=0)


def reading(
    now: float = NOW,
    *,
    outdoor: float | None = 0.0,
    indoor: float | None = 21.0,
    setpoint: int | None = 25,
    **kwargs: object,
) -> Inputs:
    return Inputs(now=now, outdoor=outdoor, indoor=indoor, setpoint=setpoint, **kwargs)


def make_core(settings: ControlSettings = SETTINGS, **state: object) -> HeatingCore:
    return HeatingCore(settings, ControlState(**state))


# Curve


def test_curve_passes_through_its_points() -> None:
    curve = CurveSettings()

    assert curve.flow(-20, 21) == pytest.approx(30)
    assert curve.flow(15, 21) == pytest.approx(22)
    assert curve.flow(0, 21) == pytest.approx(25.43, abs=0.01)
    assert curve.slope == pytest.approx(8 / 35)


def test_curve_is_flat_beyond_its_points() -> None:
    curve = CurveSettings()

    assert curve.flow(-30, 21) == pytest.approx(30)
    assert curve.flow(25, 21) == pytest.approx(22)


def test_room_target_shifts_the_curve() -> None:
    curve = CurveSettings()

    assert curve.flow(0, 22) - curve.flow(0, 21) == pytest.approx(1 + curve.slope)


def test_calibration_matches_todays_setpoint() -> None:
    curve = calibrated_curve(
        CurveSettings(), outdoor=0, indoor=21, room_target=21, setpoint=27
    )

    assert curve.flow(0, 21) == pytest.approx(27, abs=0.05)
    assert curve.slope == pytest.approx(CurveSettings().slope)


def test_calibration_allows_for_a_room_that_is_off_target() -> None:
    # 27 °C water keeps the room at 20.5; 21 needs a little more.
    curve = calibrated_curve(
        CurveSettings(), outdoor=0, indoor=20.5, room_target=21, setpoint=27
    )

    assert curve.flow(0, 21) == pytest.approx(27 + 0.5 * (1 + curve.slope), abs=0.05)


# Helpers


@pytest.mark.parametrize(
    ("value", "expected"), [(29.5, 30), (28.5, 29), (28.49, 28), (-0.5, 0)]
)
def test_round_half_up(value: float, expected: int) -> None:
    assert round_half_up(value) == expected


def test_smooth() -> None:
    assert smooth(None, 5.0, 300, 1) == 5.0
    assert smooth(0.0, 5.0, 300, 0) == 5.0
    assert smooth(0.0, 10.0, HOUR, 1) == pytest.approx(6.32, abs=0.01)


@pytest.mark.parametrize(
    ("target", "current", "expected"),
    [
        (25.6, 25, 25),  # inside rounding + hysteresis
        (25.74, 25, 25),
        (25.75, 25, 26),
        (24.25, 25, 24),
        (28.9, 25, 26),  # one degree per change
        (21.0, 25, 24),
        (40.0, 30, 30),  # never above the limit
        (40.0, 29, 30),
        (25.0, 33, 30),  # out of range goes straight back inside
        (25.0, 18, 20),
    ],
)
def test_next_setpoint(target: float, current: int, expected: int) -> None:
    assert next_setpoint(target, current, SETTINGS) == expected


# Statuses and writes


def test_advisory_mode_suggests_but_never_writes() -> None:
    core = make_core()

    decision = core.step(reading(outdoor=-10))

    assert decision.status is Status.ADVISORY
    assert decision.target_flow == pytest.approx(27.71, abs=0.01)
    assert decision.suggested_setpoint == 26
    assert decision.write is None


def test_write_mode_writes_one_degree_at_a_time() -> None:
    core = make_core(write_enabled=True)

    decision = core.step(reading(outdoor=-10))

    assert decision.status is Status.ACTIVE
    assert decision.write == 26


def test_no_write_when_already_there() -> None:
    core = make_core(write_enabled=True)

    decision = core.step(reading(outdoor=0, setpoint=25))

    assert decision.status is Status.ACTIVE
    assert decision.write is None


def test_writes_are_rate_limited() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-10))
    core.record_write(26, 25, NOW)

    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-10, setpoint=26))
    assert decision.status is Status.RATE_LIMITED
    assert decision.write is None

    decision = core.step(reading(NOW + 30 * MINUTE, outdoor=-10, setpoint=26))
    assert decision.status is Status.ACTIVE
    assert decision.write == 27


@pytest.mark.parametrize(
    "hold",
    [
        Status.PUMP_UNAVAILABLE,
        Status.PUMP_OFF,
        Status.NOT_HEATING,
        Status.PUMP_ERROR,
        Status.W_DEPEND_ON,
    ],
)
def test_pump_holds_block_writes(hold: Status) -> None:
    core = make_core(write_enabled=True)

    decision = core.step(reading(outdoor=-10, hold=hold))

    assert decision.status is hold
    assert decision.write is None


@pytest.mark.parametrize("missing", ["outdoor", "indoor"])
def test_missing_sensor_blocks_writes(missing: str) -> None:
    core = make_core(write_enabled=True)
    temperatures: dict[str, float | None] = {"outdoor": -10.0, "indoor": 21.0}
    temperatures[missing] = None

    decision = core.step(reading(**temperatures))

    assert decision.status is Status.SENSOR_UNAVAILABLE
    assert decision.write is None


def test_unknown_pump_setpoint_blocks_writes() -> None:
    core = make_core(write_enabled=True)

    decision = core.step(reading(outdoor=-10, setpoint=None))

    assert decision.status is Status.PUMP_UNAVAILABLE
    assert decision.write is None


def test_paused_controller_leaves_the_pump_alone() -> None:
    core = make_core(write_enabled=True, active=False)

    decision = core.step(reading(outdoor=-10))

    assert decision.status is Status.PAUSED
    assert decision.write is None


# Manual changes and failed writes


def test_manual_change_pauses_control() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-10, setpoint=27))

    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-10, setpoint=24))
    assert decision.status is Status.MANUAL_OVERRIDE
    assert decision.write is None

    later = NOW + 5 * MINUTE + SETTINGS.manual_pause_hours * HOUR
    decision = core.step(reading(later, outdoor=-10, setpoint=24))
    assert decision.status is Status.ACTIVE
    assert decision.write == 25


def test_switching_back_on_ends_a_manual_pause() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(setpoint=27))
    core.step(reading(NOW + 5 * MINUTE, setpoint=24))
    core.set_active(False)
    core.set_active(True)

    decision = core.step(reading(NOW + 10 * MINUTE, setpoint=24))

    assert decision.status is not Status.MANUAL_OVERRIDE


def test_manual_change_in_advisory_mode_is_just_followed() -> None:
    core = make_core()
    core.step(reading(setpoint=27))

    decision = core.step(reading(NOW + 5 * MINUTE, setpoint=24))

    assert decision.status is Status.ADVISORY


def test_own_write_is_not_a_manual_change() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-10))
    core.record_write(26, 25, NOW)

    # The pump hasn't reported the new value yet, then it has.
    decision = core.step(reading(NOW + MINUTE, outdoor=-10, setpoint=25))
    assert decision.status is not Status.MANUAL_OVERRIDE
    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-10, setpoint=26))
    assert decision.status is Status.RATE_LIMITED


def test_write_the_pump_ignores_is_reported() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-10))
    core.record_write(26, 25, NOW)

    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-10, setpoint=25))

    assert decision.status is Status.WRITE_IGNORED
    assert core.state.override_until is None


def test_failed_write_is_retried_after_the_interval() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-10))
    core.record_write_failure(NOW)

    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-10))
    assert decision.status is Status.WRITE_FAILED
    assert decision.write is None

    decision = core.step(reading(NOW + 30 * MINUTE, outdoor=-10))
    assert decision.write == 26


# Learning


def test_learning_follows_room_error() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(indoor=20.5))

    decision = core.step(reading(NOW + 5 * MINUTE, indoor=20.5))

    assert decision.learned == pytest.approx(0.1 * 0.5 * 5 / 60)


def test_no_learning_in_advisory_mode() -> None:
    core = make_core()
    core.step(reading(indoor=20.5))

    assert core.step(reading(NOW + 5 * MINUTE, indoor=20.5)).learned == 0


def test_no_learning_while_the_floor_settles_after_a_target_change() -> None:
    core = make_core(write_enabled=True)
    core.set_room_target(21.5, NOW)
    core.step(reading(indoor=21.0))

    decision = core.step(reading(NOW + 5 * HOUR, indoor=21.0))
    assert decision.learned == 0

    core.step(reading(NOW + 6 * HOUR, indoor=21.0))
    decision = core.step(reading(NOW + 6 * HOUR + 5 * MINUTE, indoor=21.0))
    assert decision.learned > 0


@pytest.mark.parametrize(
    "inputs",
    [
        {"space_heating": False},
        {"hold": Status.PUMP_OFF},
    ],
)
def test_no_learning_while_pump_is_busy_or_held(inputs: dict[str, object]) -> None:
    core = make_core(write_enabled=True)
    core.step(reading(indoor=20.5, **inputs))

    assert core.step(reading(NOW + 5 * MINUTE, indoor=20.5, **inputs)).learned == 0


def test_no_learning_when_pinned_at_the_maximum() -> None:
    core = make_core(write_enabled=True)
    core.step(reading(outdoor=-25, indoor=20.0))

    decision = core.step(reading(NOW + 5 * MINUTE, outdoor=-25, indoor=20.0))

    assert decision.target_flow == 30
    assert decision.learned == 0


def test_learned_offset_is_limited() -> None:
    core = make_core(replace(SETTINGS, kp=0, ki=100), write_enabled=True)
    core.step(reading(indoor=20.5))

    decision = core.step(reading(NOW + 5 * MINUTE, indoor=20.5))

    assert decision.learned == SETTINGS.learned_limit


def test_learning_can_be_switched_off_and_reset() -> None:
    core = make_core(write_enabled=True, learned=1.0)
    core.set_learning_enabled(False)
    core.step(reading(indoor=20.5))

    assert core.step(reading(NOW + 5 * MINUTE, indoor=20.5)).learned == 1.0

    core.reset_learning()
    assert core.state.learned == 0


# State


def test_state_round_trips_and_ignores_unknown_keys() -> None:
    state = ControlState(room_target=21.5, learned=0.7, expected_setpoint=26)

    restored = ControlState.from_dict({**state.as_dict(), "from_a_newer_version": 1})

    assert restored == state
