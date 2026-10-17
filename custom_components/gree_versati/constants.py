"""Constants for the Gree Versati integration."""

from __future__ import annotations

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "gree_versati"

CONF_DEVICE_ID: Final = "device_id"
CONF_KEY: Final = "key"
CONF_TIMEOUT: Final = "timeout"
CONF_SCAN_INTERVAL: Final = "scan_interval"

DEFAULT_PORT: Final = 7000
DEFAULT_TIMEOUT: Final = 5
DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_RETRIES: Final = 2

MIN_HE_WAT_OUT_TEMP_SET: Final = 20
MAX_HE_WAT_OUT_TEMP_SET: Final = 60
HE_WAT_OUT_TEMP_SET_STEP: Final = 1

MIN_CO_WAT_OUT_TEMP_SET: Final = 5
MAX_CO_WAT_OUT_TEMP_SET: Final = 25
CO_WAT_OUT_TEMP_SET_STEP: Final = 1

MIN_WAT_BOX_TEMP_SET: Final = 20
MAX_WAT_BOX_TEMP_SET: Final = 60
WAT_BOX_TEMP_SET_STEP: Final = 1

PARAM_HE_WAT_OUT_TEM_SET: Final = "HeWatOutTemSet"
PARAM_CO_WAT_OUT_TEM_SET: Final = "CoWatOutTemSet"
PARAM_WAT_BOX_TEM_SET: Final = "WatBoxTemSet"
PARAM_TEM_UN: Final = "TemUn"
PARAM_ALL_ERR: Final = "AllErr"
PARAM_MOD: Final = "Mod"
PARAM_POW: Final = "Pow"

POLL_KEYS: Final[tuple[str, ...]] = (
    PARAM_HE_WAT_OUT_TEM_SET,
    PARAM_CO_WAT_OUT_TEM_SET,
    PARAM_WAT_BOX_TEM_SET,
    PARAM_TEM_UN,
    PARAM_ALL_ERR,
    PARAM_MOD,
    PARAM_POW,
)

# Measured temperatures are reported as a pair of integer parameters:
#   temperature_c = (<name>Hi - 100) + <name>Lo * 0.1
# e.g. AllOutWatTemHi=132, AllOutWatTemLo=2 -> 32.2 °C.
# A decoded value of -30.0 (Hi=70, Lo=0) means the sensor is not fitted.
PARAM_ALL_IN_WAT_TEM: Final = "AllInWatTem"
PARAM_ALL_OUT_WAT_TEM: Final = "AllOutWatTem"
PARAM_HEP_OUT_WAT_TEM: Final = "HepOutWatTem"
PARAM_WAT_BOX_TEM: Final = "WatBoxTem"
PARAM_RMO_HOM_TEM: Final = "RmoHomTem"

TEMP_HI_SUFFIX: Final = "Hi"
TEMP_LO_SUFFIX: Final = "Lo"
TEMP_HI_OFFSET: Final = 100
TEMP_LO_SCALE: Final = 0.1
TEMP_NOT_FITTED: Final = -30.0

MEASURED_TEMPERATURE_PARAMS: Final[tuple[str, ...]] = (
    PARAM_ALL_IN_WAT_TEM,
    PARAM_ALL_OUT_WAT_TEM,
    PARAM_HEP_OUT_WAT_TEM,
    PARAM_WAT_BOX_TEM,
    PARAM_RMO_HOM_TEM,
)

# Polled in a separate request so that firmware which does not report these
# keys can never break polling of the setpoints, mode and power state.
MEASUREMENT_POLL_KEYS: Final[tuple[str, ...]] = tuple(
    f"{param}{suffix}"
    for param in MEASURED_TEMPERATURE_PARAMS
    for suffix in (TEMP_HI_SUFFIX, TEMP_LO_SUFFIX)
)

# Which goes first in "Heat + hot water" mode: the floor or the hot water tank.
# The Gree app calls this "Priority selection".
PARAM_HET_HT_WTER: Final = "HetHtWter"
HEAT_HOT_WATER_PRIORITY_OPTIONS: Final[dict[str, int]] = {"heating": 0, "hot_water": 1}

# Settings some firmware may not report, polled in their own request.
SETTING_POLL_KEYS: Final[tuple[str, ...]] = (PARAM_HET_HT_WTER,)

# Status flags read by floor heating control, polled only when it is enabled.
# SvSt is the controller's "W-depend" (weather-dependent curve) switch; while
# it is on the pump works out its own water setpoint.
PARAM_SV_ST: Final = "SvSt"
PARAM_SY_AN_FRO_RUN_STA: Final = "SyAnFroRunSta"
STATUS_POLL_KEYS: Final[tuple[str, ...]] = (PARAM_SV_ST, PARAM_SY_AN_FRO_RUN_STA)

# After a failed measurement or status request, skip it for this long before
# retrying.
MEASUREMENT_RETRY_BACKOFF_SECONDS: Final = 300

SERVICE_SET_PARAM: Final = "set_param"
SERVICE_GET_PARAMS: Final = "get_params"

ATTR_KEY: Final = "key"
ATTR_KEYS: Final = "keys"
ATTR_VALUE: Final = "value"

DEFAULT_MAX_DATAGRAM_BYTES: Final = 8192

DATA_ENTRIES: Final = "entries"
DATA_CLIENT: Final = "client"
DATA_COORDINATOR: Final = "coordinator"
DATA_HEATING: Final = "heating"

MODE_OPTIONS: Final[dict[str, int]] = {
    "Heat": 1,
    "Cool": 5,
    "Hot water": 2,
    "Cool + hot water": 3,
    "Heat + hot water": 4,
}

PLATFORMS: Final[list[Platform]] = [
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.CLIMATE,
    Platform.BUTTON,
]
