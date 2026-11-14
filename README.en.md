# Adaptive Floor Heating Climate

[한국어](README.md)

A custom Home Assistant climate integration for adaptive underfloor heating. Connect an indoor temperature sensor and a heater switch for basic temperature control. In AUTO mode, the integration learns from past heating cycles to help decide when to start and stop heating.

> [!IMPORTANT]
> **Experimental.** This integration has been tested only with individual boilers. Reliable operation with other systems is not guaranteed. You are responsible for installation and use.

## Installation and setup

1. Add the [GitHub repository](https://github.com/plplaaa2/adapted_climate) to HACS as a custom repository of type **Integration**. For manual installation, copy `custom_components/adaptive_floor_heating/` into the `custom_components/` directory in your Home Assistant configuration.
2. Restart Home Assistant and select **Adaptive Floor Heating Climate** under **Settings → Devices & services → Add integration**.
3. Choose standalone or integrated multi-room control and connect the heater switch and indoor temperature sensor. Set the start and stop temperature differentials, minimum ON and OFF times, and HOME and AWAY temperatures. An outdoor temperature sensor is optional and enables weather-conditioned predictions.
4. The initial mode is `OFF`. Select `HEAT` or `AUTO` on the climate entity to enable heating.

## Operating modes

| Mode | Behavior |
| --- | --- |
| `OFF` | Disables automatic heating control. |
| `HEAT` | Controls heating using the target temperature and start and stop differentials. |
| `AUTO` | Adds learned predictions to basic temperature control. |

- **Predictive ON** can start heating early based on expected cooling and the delay before heating affects the indoor temperature.
- **Predictive OFF** uses the runtime model to schedule an initial stop. When the hybrid model has sufficient evidence, it uses the predicted peak temperature to decide when to stop. Basic temperature control applies when neither model has enough evidence.
- **HOME/AWAY** changes the target temperature. Sensor or switch errors, overheating, and external switch operation can stop or delay heating. After a fault lockout, resolve the cause, confirm that the heater is OFF, and select `HEAT` or `AUTO` again.

### Sunrise saving

Enable **Sunrise saving** in the predictive operation settings and save both a start and an end time. During this window, AUTO lowers its predictive stop target from the **target temperature plus the upper tolerance** to the **target temperature**. The schedule uses Home Assistant's local time and can cross midnight. Equal start and end times disable the window.

## Dashboard and learning

### Initial learning and the three-cycle minimum

After installation or a learning reset, operate the heating as usual. The integration learns automatically. Runtime-based predictive OFF requires at least **three qualifying heating cycles** with measured heater runtime and an observed indoor temperature peak after shutdown. Excluded cycles do not count toward this minimum.

When the evidence for predictive OFF is insufficient, the integration uses the basic stop threshold based on the target temperature and tolerance. Once three qualifying cycles are available, it checks whether a runtime prediction can be applied under the current operating conditions. Predictive ON is evaluated independently using learned cooling behavior and heating response delay.

- Open **Prediction Analysis** to view the initial stop schedule, the active model, and the predicted and measured peak temperatures.
- Open **Learning Analysis** to view recent and long-term learning and reasons individual cycles were held back. Adjust the **Peak observation time limit** in the predictive operation settings.
- **Learning reset** clears the device's learned data and prediction comparisons once the heater is confirmed OFF and there are no pending switch commands. Home Assistant Recorder history and device settings are retained.

## Development tests

Hassfest also runs on private repositories. HACS validation runs on the public repository and skips the `license` check because the repository has no license file.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests/components/adaptive_floor_heating -p "test_*.py" -v
```
