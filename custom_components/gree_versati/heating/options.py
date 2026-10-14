"""Settings form for floor heating control."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.const import UnitOfTemperature, UnitOfTime
from homeassistant.helpers import selector

from ..constants import MAX_HE_WAT_OUT_TEMP_SET, MIN_HE_WAT_OUT_TEMP_SET
from .const import (
    CONF_CURVE_COLD_FLOW,
    CONF_CURVE_COLD_OUTDOOR,
    CONF_CURVE_MILD_FLOW,
    CONF_CURVE_MILD_OUTDOOR,
    CONF_HEATING_ENABLED,
    CONF_INDOOR_SENSOR,
    CONF_KI,
    CONF_KP,
    CONF_MANUAL_PAUSE_HOURS,
    CONF_MAX_FLOW,
    CONF_MIN_FLOW,
    CONF_OUTDOOR_SENSOR,
    CONF_OUTDOOR_SMOOTHING_HOURS,
    settings_from_options,
)


def _number(
    minimum: float, maximum: float, step: float, unit: str | None = None
) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=minimum, max=maximum, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit is not None:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


def heating_schema(options: Mapping[str, Any]) -> vol.Schema:
    """Form fields, pre-filled from the current options or the defaults."""
    settings = settings_from_options(options)
    curve = settings.curve
    sensor = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
    celsius = UnitOfTemperature.CELSIUS
    pump_range = (MIN_HE_WAT_OUT_TEMP_SET, MAX_HE_WAT_OUT_TEMP_SET)

    return vol.Schema(
        {
            vol.Required(
                CONF_HEATING_ENABLED, default=bool(options.get(CONF_HEATING_ENABLED, False))
            ): selector.BooleanSelector(),
            vol.Optional(
                CONF_INDOOR_SENSOR,
                description={"suggested_value": options.get(CONF_INDOOR_SENSOR)},
            ): sensor,
            vol.Optional(
                CONF_OUTDOOR_SENSOR,
                description={"suggested_value": options.get(CONF_OUTDOOR_SENSOR)},
            ): sensor,
            vol.Required(CONF_CURVE_COLD_OUTDOOR, default=curve.cold_outdoor): _number(
                -40, 10, 0.5, celsius
            ),
            vol.Required(CONF_CURVE_COLD_FLOW, default=curve.cold_flow): _number(
                10, 60, 0.1, celsius
            ),
            vol.Required(CONF_CURVE_MILD_OUTDOOR, default=curve.mild_outdoor): _number(
                -10, 25, 0.5, celsius
            ),
            vol.Required(CONF_CURVE_MILD_FLOW, default=curve.mild_flow): _number(
                10, 60, 0.1, celsius
            ),
            vol.Required(CONF_MIN_FLOW, default=settings.min_flow): _number(
                *pump_range, 1, celsius
            ),
            vol.Required(CONF_MAX_FLOW, default=settings.max_flow): _number(
                *pump_range, 1, celsius
            ),
            vol.Required(CONF_KP, default=settings.kp): _number(0, 10, 0.1),
            vol.Required(CONF_KI, default=settings.ki): _number(0, 1, 0.01),
            vol.Required(
                CONF_OUTDOOR_SMOOTHING_HOURS, default=settings.outdoor_smoothing_hours
            ): _number(0, 24, 0.5, UnitOfTime.HOURS),
            vol.Required(
                CONF_MANUAL_PAUSE_HOURS, default=settings.manual_pause_hours
            ): _number(0, 48, 0.5, UnitOfTime.HOURS),
        }
    )


def validate_heating(data: Mapping[str, Any]) -> dict[str, str]:
    """Field errors for a submitted form, keyed by field."""
    errors: dict[str, str] = {}
    indoor = data.get(CONF_INDOOR_SENSOR)
    outdoor = data.get(CONF_OUTDOOR_SENSOR)
    if data.get(CONF_HEATING_ENABLED):
        if not indoor:
            errors[CONF_INDOOR_SENSOR] = "sensor_required"
        if not outdoor:
            errors[CONF_OUTDOOR_SENSOR] = "sensor_required"
    if indoor and indoor == outdoor:
        errors[CONF_OUTDOOR_SENSOR] = "same_sensor"
    if data[CONF_CURVE_MILD_OUTDOOR] <= data[CONF_CURVE_COLD_OUTDOOR]:
        errors[CONF_CURVE_MILD_OUTDOOR] = "curve_outdoor_order"
    if data[CONF_CURVE_MILD_FLOW] > data[CONF_CURVE_COLD_FLOW]:
        errors[CONF_CURVE_MILD_FLOW] = "curve_flow_order"
    if data[CONF_MAX_FLOW] - data[CONF_MIN_FLOW] < 1:
        errors[CONF_MAX_FLOW] = "flow_range"
    return errors
