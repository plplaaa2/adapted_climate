# Adaptive Floor Heating Climate

[한국어](README.md)

A custom Home Assistant climate integration for adaptive underfloor heating. Connect an indoor temperature sensor and either a heater switch or an existing thermostat. In AUTO mode, the integration learns from past heating cycles to help decide when to start and stop heating.

> [!IMPORTANT]
> **Experimental.** This integration has been tested only with individual boilers. Reliable operation with other systems is not guaranteed. You are responsible for installation and use.

## Installation and setup

1. Add the [GitHub repository](https://github.com/plplaaa2/adapted_climate) to HACS as a custom repository of type **Integration**. For manual installation, copy `custom_components/adaptive_floor_heating/` into the `custom_components/` directory in your Home Assistant configuration.
2. Restart Home Assistant and select **Adaptive Floor Heating Climate** under **Settings → Devices & services → Add integration**.
3. Choose **Switch** or **Climate**. Switch setup offers standalone and integrated multi-room control. Climate setup uses one thermostat and asks for an upper and lower setpoint. Connect an indoor temperature sensor, then set the start and stop temperature differentials, minimum ON and OFF times, and HOME and AWAY temperatures. Outdoor temperature and weather entities for sunrise learning are optional.
4. The initial mode is `OFF`. Select `HEAT` or `AUTO` on the climate entity to enable heating.

## First run

1. The initial mode is OFF. Check the connected indoor temperature and actual heater OFF state.
2. Confirm basic operation with HEAT, then select AUTO. HOME/AWAY changes the target temperature.
3. Runtime-based predictive OFF needs at least **three qualifying cycles** with actual runtime and a confirmed residual Peak. Basic control applies when evidence is insufficient.
4. Select the room in the **바닥난방** sidebar panel to view control status, Prediction Analysis and Learning Analysis.

Minimum ON/OFF waits can delay requests. Climate control sends lower/upper setpoints to the existing thermostat, so check actual heating activity too. Wait until heating and residual/cooling observations have finished before updates or configuration reloads.

Climate command confirmation delay is configurable in Options and defaults to 1 second. Indoor/outdoor temperature and selected weather sensors are checked at startup and every five minutes. One missed check reuses the last valid value.

Climate command confirmation delay is configurable in Options and defaults to 1 second. Indoor/outdoor temperature and selected weather sensors are checked at startup and every five minutes. One missed check reuses the last valid value.

## User guide

See the [guide index](docs/README.en.md) for detailed instructions.

Administrators can use **바닥난방 → Learning Analysis → Migration** to recheck past Peak timeouts, heater outages and sunrise-saving exclusions, and to recalculate outdoor temperature bands. See the [recovery requirements](docs/maintenance.en.md#learning-history-migration).

| Guide | Contents |
| --- | --- |
| [Setup](docs/setup.en.md) | Control types, entity requirements, defaults and first-run checks |
| [Operation and learning](docs/operation.en.md) | Predictive control, initial learning, Peak confirmation and sunrise saving |
| [Maintenance](docs/maintenance.en.md) | Options, updates, backups, retention, recovery and reset |
| [Troubleshooting](docs/troubleshooting.en.md) | Heating waits, excluded cycles, faults and diagnostic JSON exports |

## Development tests

Hassfest also runs on private repositories. HACS validation runs on the public repository and skips the `license` check because the repository has no license file.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/components/adaptive_floor_heating -p "test_*.py" -v
```
