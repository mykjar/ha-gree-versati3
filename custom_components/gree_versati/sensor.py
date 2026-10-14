"""Sensor platform for Gree Versati."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .constants import (
    CONF_DEVICE_ID,
    DATA_COORDINATOR,
    DATA_ENTRIES,
    PARAM_ALL_ERR,
    PARAM_ALL_IN_WAT_TEM,
    PARAM_ALL_OUT_WAT_TEM,
    PARAM_CO_WAT_OUT_TEM_SET,
    PARAM_HE_WAT_OUT_TEM_SET,
    PARAM_HEP_OUT_WAT_TEM,
    PARAM_MOD,
    PARAM_POW,
    PARAM_RMO_HOM_TEM,
    PARAM_TEM_UN,
    PARAM_WAT_BOX_TEM,
    PARAM_WAT_BOX_TEM_SET,
    TEMP_HI_SUFFIX,
)
from .coordinator import GreeVersatiCoordinator
from .entity import GreeVersatiEntity
from .temperature import decode_measured_temperature


@dataclass(frozen=True, kw_only=True)
class GreeVersatiSensorDescription(SensorEntityDescription):
    """Description of a Gree Versati sensor."""

    param_key: str


SENSOR_DESCRIPTIONS: tuple[GreeVersatiSensorDescription, ...] = (
    GreeVersatiSensorDescription(
        key="he_wat_out_tem_set",
        translation_key="he_wat_out_tem_set",
        param_key=PARAM_HE_WAT_OUT_TEM_SET,
    ),
    GreeVersatiSensorDescription(
        key="co_wat_out_tem_set",
        translation_key="co_wat_out_tem_set",
        param_key=PARAM_CO_WAT_OUT_TEM_SET,
    ),
    GreeVersatiSensorDescription(
        key="wat_box_tem_set",
        translation_key="wat_box_tem_set",
        param_key=PARAM_WAT_BOX_TEM_SET,
    ),
    GreeVersatiSensorDescription(
        key="tem_un",
        translation_key="tem_un",
        param_key=PARAM_TEM_UN,
    ),
    GreeVersatiSensorDescription(
        key="all_err",
        translation_key="all_err",
        param_key=PARAM_ALL_ERR,
    ),
    GreeVersatiSensorDescription(
        key="mod",
        translation_key="mod",
        param_key=PARAM_MOD,
    ),
    GreeVersatiSensorDescription(
        key="pow",
        translation_key="pow",
        param_key=PARAM_POW,
    ),
)


@dataclass(frozen=True, kw_only=True)
class GreeVersatiTemperatureSensorDescription(SensorEntityDescription):
    """Description of a measured temperature reported as a Hi/Lo pair."""

    param: str


def _temperature_description(
    key: str, param: str, *, enabled_by_default: bool = True
) -> GreeVersatiTemperatureSensorDescription:
    return GreeVersatiTemperatureSensorDescription(
        key=key,
        translation_key=key,
        param=param,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        entity_registry_enabled_default=enabled_by_default,
    )


TEMPERATURE_SENSOR_DESCRIPTIONS: tuple[GreeVersatiTemperatureSensorDescription, ...] = (
    _temperature_description("all_out_wat_tem", PARAM_ALL_OUT_WAT_TEM),
    _temperature_description("all_in_wat_tem", PARAM_ALL_IN_WAT_TEM),
    _temperature_description("wat_box_tem", PARAM_WAT_BOX_TEM),
    # Often not fitted (the device then reports -30.0 °C), so disabled by default.
    _temperature_description(
        "hep_out_wat_tem", PARAM_HEP_OUT_WAT_TEM, enabled_by_default=False
    ),
    _temperature_description(
        "rmo_hom_tem", PARAM_RMO_HOM_TEM, enabled_by_default=False
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors from a config entry."""
    runtime_data = hass.data[entry.domain][DATA_ENTRIES][entry.entry_id]
    coordinator = runtime_data[DATA_COORDINATOR]
    device_id = entry.data[CONF_DEVICE_ID]

    async_add_entities(
        GreeVersatiSensor(coordinator, device_id, description)
        for description in SENSOR_DESCRIPTIONS
    )
    async_add_entities(
        GreeVersatiTemperatureSensor(coordinator, device_id, description)
        for description in TEMPERATURE_SENSOR_DESCRIPTIONS
    )


class GreeVersatiSensor(GreeVersatiEntity, SensorEntity):
    """Represents a generic value sensor from device parameters."""

    entity_description: GreeVersatiSensorDescription

    def __init__(
        self,
        coordinator: GreeVersatiCoordinator,
        device_id: str,
        description: GreeVersatiSensorDescription,
    ) -> None:
        super().__init__(coordinator, device_id, description.param_key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | float | bool | None:
        """Return raw protocol value."""
        return (self.coordinator.data or {}).get(self.entity_description.param_key)


class GreeVersatiTemperatureSensor(GreeVersatiEntity, SensorEntity):
    """Measured temperature decoded from a Hi/Lo parameter pair."""

    entity_description: GreeVersatiTemperatureSensorDescription

    def __init__(
        self,
        coordinator: GreeVersatiCoordinator,
        device_id: str,
        description: GreeVersatiTemperatureSensorDescription,
    ) -> None:
        super().__init__(
            coordinator,
            device_id,
            f"{description.param}{TEMP_HI_SUFFIX}",
            unique_id_key=description.param,
        )
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable when not reported, malformed, or the sensor is not fitted."""
        return super().available and self.native_value is not None

    @property
    def native_value(self) -> float | None:
        """Return the decoded temperature in °C."""
        return decode_measured_temperature(
            self.coordinator.data or {}, self.entity_description.param
        )
