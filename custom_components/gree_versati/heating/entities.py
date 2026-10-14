"""Entities for floor heating control, attached to the pump's device."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import ATTR_TEMPERATURE, PRECISION_TENTHS, EntityCategory, UnitOfTemperature
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from ..constants import DOMAIN
from .const import MAX_ROOM_TARGET, MIN_ROOM_TARGET
from .controller import HeatingController
from .curve import Decision, Status


class HeatingEntity(CoordinatorEntity[HeatingController]):
    """Base for floor heating entities."""

    _attr_has_entity_name = True

    def __init__(self, controller: HeatingController, device_id: str, key: str) -> None:
        super().__init__(controller)
        self._attr_unique_id = f"{device_id}_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device_id)})

    @property
    def decision(self) -> Decision | None:
        """The latest result of the control loop."""
        return self.coordinator.data


class FloorHeatingClimate(HeatingEntity, ClimateEntity):
    """Thermostat for the room: target temperature in, water setpoint out."""

    _attr_translation_key = "floor_heating"
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_precision = PRECISION_TENTHS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = MIN_ROOM_TARGET
    _attr_max_temp = MAX_ROOM_TARGET

    def __init__(self, controller: HeatingController, device_id: str) -> None:
        super().__init__(controller, device_id, "floor_heating")

    async def async_added_to_hass(self) -> None:
        """Follow the indoor sensor and the pump between control steps."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.coordinator.indoor_entity_id], self._async_indoor_changed
            )
        )
        self.async_on_remove(
            self.coordinator.pump.async_add_listener(self.async_write_ha_state)
        )

    @callback
    def _async_indoor_changed(self, event: Event[EventStateChangedData]) -> None:
        self.async_write_ha_state()

    @property
    def current_temperature(self) -> float | None:
        """Indoor temperature."""
        return self.coordinator.temperature(self.coordinator.indoor_entity_id)

    @property
    def target_temperature(self) -> float:
        """Room temperature to aim for."""
        return self.coordinator.core.state.room_target

    @property
    def hvac_mode(self) -> HVACMode:
        """Heat while control is on; Off pauses it and leaves the pump as it is."""
        return HVACMode.HEAT if self.coordinator.core.state.active else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """Whether the pump is delivering heat."""
        if not self.coordinator.core.state.active:
            return HVACAction.OFF
        return HVACAction.HEATING if self.coordinator.pump_heating else HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The full calculation, for anyone tuning the curve."""
        decision = self.decision
        override_until = self.coordinator.core.state.override_until
        return {
            "status": decision.status if decision else None,
            "outdoor_temperature": _rounded(decision.outdoor if decision else None),
            "curve_water_temperature": _rounded(decision.curve_flow if decision else None),
            "room_correction": _rounded(decision.correction if decision else None),
            "target_water_temperature": _rounded(decision.target_flow if decision else None),
            "suggested_setpoint": decision.suggested_setpoint if decision else None,
            "manual_change_until": (
                dt_util.utc_from_timestamp(override_until).isoformat()
                if override_until is not None
                else None
            ),
        }

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the room temperature to aim for."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_room_target(float(temperature))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Turn control on or off."""
        await self.coordinator.async_set_active(hvac_mode == HVACMode.HEAT)

    async def async_turn_on(self) -> None:
        """Resume control."""
        await self.coordinator.async_set_active(True)

    async def async_turn_off(self) -> None:
        """Pause control."""
        await self.coordinator.async_set_active(False)


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


@dataclass(frozen=True, kw_only=True)
class HeatingSensorDescription(SensorEntityDescription):
    """Sensor showing one part of the control calculation."""

    value_fn: Callable[[Decision], StateType | datetime]


def _water_temperature(
    key: str, value_fn: Callable[[Decision], float | None], precision: int = 1
) -> HeatingSensorDescription:
    return HeatingSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=precision,
        value_fn=value_fn,
    )


HEATING_SENSORS: tuple[HeatingSensorDescription, ...] = (
    _water_temperature("heating_curve_water_temperature", lambda d: d.curve_flow),
    _water_temperature("heating_target_water_temperature", lambda d: d.target_flow),
    _water_temperature(
        "heating_suggested_setpoint", lambda d: d.suggested_setpoint, precision=0
    ),
    # A temperature difference: no device class, so it is never unit-converted.
    HeatingSensorDescription(
        key="heating_learned_offset",
        translation_key="heating_learned_offset",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=2,
        value_fn=lambda d: d.learned,
    ),
    HeatingSensorDescription(
        key="heating_status",
        translation_key="heating_status",
        device_class=SensorDeviceClass.ENUM,
        options=[status.value for status in Status],
        value_fn=lambda d: d.status.value,
    ),
)


class HeatingSensor(HeatingEntity, SensorEntity):
    """One part of the control calculation, for dashboards and history."""

    entity_description: HeatingSensorDescription

    def __init__(
        self,
        controller: HeatingController,
        device_id: str,
        description: HeatingSensorDescription,
    ) -> None:
        super().__init__(controller, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> StateType | datetime:
        """Value from the latest control step."""
        decision = self.decision
        return self.entity_description.value_fn(decision) if decision else None


class WriteToPumpSwitch(HeatingEntity, SwitchEntity):
    """Off: advisory only. On: the controller sets the pump's water setpoint."""

    _attr_translation_key = "heating_write"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: HeatingController, device_id: str) -> None:
        super().__init__(controller, device_id, "heating_write")

    @property
    def is_on(self) -> bool:
        """Whether the controller writes to the pump."""
        return self.coordinator.core.state.write_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let the controller set the pump's water setpoint."""
        await self.coordinator.async_set_write_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Go back to advisory mode; the pump keeps its current setpoint."""
        await self.coordinator.async_set_write_enabled(False)


class CalibrateCurveButton(HeatingEntity, ButtonEntity):
    """Fit the curve to the setpoint the pump runs on today."""

    _attr_translation_key = "heating_calibrate"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, controller: HeatingController, device_id: str) -> None:
        super().__init__(controller, device_id, "heating_calibrate")

    async def async_press(self) -> None:
        """Shift the curve to match the current setpoint."""
        await self.coordinator.async_calibrate()


def heating_sensors(controller: HeatingController, device_id: str) -> list[HeatingSensor]:
    """All floor heating sensors."""
    return [
        HeatingSensor(controller, device_id, description) for description in HEATING_SENSORS
    ]
