"""Shared settings; related: config_flow.py, controller.py, runtime.py and translations."""

DOMAIN = "adaptive_floor_heating"
NAME = "Adaptive Floor Heating Climate"

CONF_MODE = "mode"
CONF_HEATER = "heater"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_ROOM_COUNT = "room_count"
CONF_ZONES = "zones"
CONF_ZONE_ID = "zone_id"

MODE_STANDALONE = "standalone"
MODE_MULTI_ZONE = "multi_zone"
MAX_ROOMS = 32
CONTEXT_HEATERS = "selected_heaters"

DEFAULT_TARGET = 20.0
DEFAULT_HOME_TEMPERATURE = 23.0
DEFAULT_AWAY_TEMPERATURE = 18.0
MIN_TARGET = 18.0
MAX_TARGET = 30.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_MIN_ON = 900
DEFAULT_MIN_OFF = 600
DEFAULT_SENSOR_TIMEOUT = 900
OVERHEAT_TEMPERATURE = 35.0
OVERHEAT_RESET = 34.0
RECOVERY_INTERVAL = 30.0
COMMAND_TIMEOUT = 10.0
OFF_RETRY_DELAYS = (5.0, 10.0)
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_MIN_ON = "minimum_on_time"
CONF_MIN_OFF = "minimum_off_time"
CONF_SENSOR_TIMEOUT = "sensor_timeout"
CONF_HOME_TEMPERATURE = "home_temperature"
CONF_AWAY_TEMPERATURE = "away_temperature"
LATCHED_FAULTS = frozenset(
    {"actuation_fault", "heater_unavailable", "overheat", "external_override"}
)
