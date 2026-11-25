"""Shared settings; related: config_flow.py, controller.py, runtime.py and translations."""

DOMAIN = "adaptive_floor_heating"
NAME = "Adaptive Floor Heating Climate"

CONF_MODE = "mode"
CONF_HEATER = "heater"
CONF_CLIMATE_LOW = "climate_low"
CONF_CLIMATE_HIGH = "climate_high"
DEFAULT_CLIMATE_LOW = 18.0
DEFAULT_CLIMATE_HIGH = 30.0
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_OUTDOOR_TEMPERATURE_SENSOR = "outdoor_temperature_sensor"
CONF_SUNRISE_WEATHER_ENTITY = "sunrise_weather_entity"
CONF_ROOM_COUNT = "room_count"
CONF_ZONES = "zones"
CONF_ZONE_ID = "zone_id"

MODE_STANDALONE = "standalone"
MODE_MULTI_ZONE = "multi_zone"
MODE_MULTI_ZONE_INTEGRATED = "multi_zone_integrated"
MAX_ROOMS = 32
MIN_INTEGRATED_ROOMS = 2
CONTEXT_HEATERS = "selected_heaters"

DEFAULT_TARGET = 20.0
DEFAULT_HOME_TEMPERATURE = 23.0
DEFAULT_AWAY_TEMPERATURE = 18.0
MIN_TARGET = 18.0
MAX_TARGET = 30.0
DEFAULT_TOLERANCE = 0.5
DEFAULT_HEAT_HOT_TOLERANCE = 0.0
DEFAULT_MIN_ON = 900
# Floor heating keeps releasing heat well after the actuator turns off.
DEFAULT_MIN_OFF = 1800
MIN_CONFIG_MIN_OFF = 1800
DEFAULT_SENSOR_TIMEOUT = 900
# Configure learning-only outage tolerance; related: controller.py, runtime.py.
DEFAULT_HEATER_UNAVAILABLE_GRACE = 60
MAX_HEATER_UNAVAILABLE_GRACE = 300
# Verify a learned runtime cutoff before committing its final OFF command.
OVERHEAT_TEMPERATURE = 35.0
OVERHEAT_RESET = 34.0
RECOVERY_INTERVAL = 30.0
HEATER_UNAVAILABLE_DELAY = 10.0
# Allow device reports to stabilize after reconnect; related: runtime.py.
HEATER_RECOVERY_OVERRIDE_GRACE = 10.0
COMMAND_TIMEOUT = 10.0
# Climate confirmation delay in seconds. Related: config_flow.py, controller.py, actuator.py.
CONF_CLIMATE_COMMAND_WAIT = "climate_command_wait"
DEFAULT_CLIMATE_COMMAND_WAIT = 1
MAX_CLIMATE_COMMAND_WAIT = 300
OFF_RETRY_DELAYS = (5.0, 10.0)
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_HEAT_HOT_TOLERANCE = "heat_hot_tolerance"
CONF_MIN_ON = "minimum_on_time"
# Keep the persisted option key while applying the cap to every run; related: controller.py, runtime.py.
CONF_INITIAL_MAX_ON_MINUTES = "initial_maximum_on_minutes"
MIN_INITIAL_MAX_ON_MINUTES = 60
MAX_INITIAL_MAX_ON_MINUTES = 180
CONF_MIN_OFF = "minimum_off_time"
CONF_SENSOR_TIMEOUT = "sensor_timeout"
CONF_HEATER_UNAVAILABLE_GRACE = "heater_unavailable_grace"
CONF_PEAK_OBSERVATION_HOURS = "peak_observation_hours"
DEFAULT_PEAK_OBSERVATION_HOURS = 6
MIN_PEAK_OBSERVATION_HOURS = 1
MAX_PEAK_OBSERVATION_HOURS = 12
CONF_HOME_TEMPERATURE = "home_temperature"
CONF_AWAY_TEMPERATURE = "away_temperature"
# Selectable predictive start strength; related: runtime.py, select.py, storage.py.
PREDICTION_MODES = ("eco", "balanced", "comfort")
# Model labels retained in historical forecasts and accuracy records; related: storage.py.
OFF_MODELS = ("runtime", "additive", "hybrid")
DEFAULT_PREDICTION_MODE = "balanced"
PERSISTED_LEARNING_MODELS = ("existing", "curve")
LATCHED_FAULTS = frozenset(
    {"actuation_fault", "heater_unavailable", "overheat", "external_override"}
)
