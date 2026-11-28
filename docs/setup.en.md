# Setup

[Guide index](README.en.md) · [한국어](setup.md) · Next: [Operation and learning](operation.en.md)

## 1. Prepare the entities

Check that the following entities work in Home Assistant before adding this integration. Developer tools → States shows entity IDs and attributes.

| Entity | Requirements |
| --- | --- |
| Indoor temperature sensor | A numeric measurement for the controlled room, with `device_class: temperature` and unit `°C` or `°F`. Unknown or unavailable entities cannot be selected. |
| Heater switch | For Switch control: reports `on`/`off` and operates the actual heating request. |
| Existing thermostat | For Climate control: supports heat mode and setting and reporting a single target temperature. |
| Outdoor temperature sensor | Optional; uses the same temperature format and supports outdoor-conditioned learning and prediction correction. |

A heating device cannot be registered in multiple rooms or integration entries. Check other automations and thermostat schedules that operate the same device; conflicting external changes can cause fault locks or exclude learning cycles.

Use a sensor location representative of the room and keep it consistent during learning. Moving the sensor or changing the heating arrangement can change the response learned by the integration.

## 2. Choose a control type

Add **Adaptive Floor Heating Climate**, then choose:

| Type | Configuration |
| --- | --- |
| Switch → Standalone | One heater switch and one indoor sensor. Add a separate entry for each independently controlled room. |
| Switch → Integrated multi-room | Select 2–32 rooms, one shared indoor sensor and a switch for each room. One Climate and learning model operate the group together. |
| Climate | Select one existing thermostat and an indoor sensor, then configure the OFF lower setpoint and ON upper setpoint. Add an entry per independently controlled room. |

Integrated groups require all switches to confirm the same state. Mixed or unavailable states lead to group shutdown and fault handling.

### Climate lower and upper setpoints

Defaults are **18°C lower** and **30°C upper**. These are requests sent to the existing thermostat, separate from this integration's room target. The values must be within the thermostat's supported range.

For a room target of 23°C, the existing thermostat may display 30°C when heat is requested and 18°C when a stop is requested. The integration records and learns from the interval between reported upper and lower setpoints. If the thermostat's own measured temperature is below the lower setpoint, it may continue heating after applying that setpoint.

The Climate **command confirmation delay** defaults to 1 second and accepts 1–300 seconds. After either an ON or OFF request, the integration confirms the command against the latest reported target when the delay ends. A temporary return to the previous target during the wait does not trigger another request. Actual heating activity follows the thermostat's `hvac_action`.

## 3. Enter heating settings

Enter **maximum continuous runtime** in minutes and the other operating and protection times in seconds.

| Setting | Default | Meaning |
| --- | --- | --- |
| Start differential | 0.5°C | Basic heating starts when the indoor temperature reaches the target minus this value. |
| AUTO stop differential | 0.5°C | Upper tolerance for the AUTO basic stop threshold and predictive stop target. |
| HEAT stop differential | 0.0°C | HEAT stops at the target plus this value. |
| Minimum ON time | 15 min | Minimum normal heating duration. The allowed range is 0–60 minutes. |
| Maximum continuous runtime | 60 min | Limits each run from the time the heater confirms ON. The allowed range is 60–180 minutes. |
| Heater outage grace period for learning | 60 s | Delays the decision to exclude an active learning cycle for a heater outage. The allowed range is 0–300 seconds. Set it to 0 to exclude the cycle immediately. |
| Climate command confirmation delay | 1 s | For Climate control, wait this long after an ON or OFF request before checking the latest target temperature. The allowed range is 1–300 seconds. |
| Base minimum OFF time | 60 min | Default for new installations. The setup form allows 30–60 minutes, and existing saved values are preserved. |
| Temperature report interval limit | 900 s / 15 min | An observation-related timing setting, also used for outdoor-value validity. An unchanged indoor number does not by itself define a heating shutdown timer. |
| HOME temperature | 23°C | Target applied by the HOME preset. |
| AWAY temperature | 18°C | Target applied by the AWAY preset. |

The minimum OFF interval uses the **larger of the configured minimum and twice the preceding continuous runtime**. After a 40-minute run, the wait is at least 80 minutes. A learned response delay can extend the wait after a Sunrise Saving predictive stop. Safety stops and explicit OFF commands take priority over a normal minimum ON wait.

With a 23°C target and default differentials, the basic start threshold is 22.5°C, the HEAT stop threshold is 23°C, and the AUTO basic stop threshold is 23.5°C. Qualified AUTO predictions can stop earlier to account for residual heat.

Set heating temperature offsets and operating times on separate screens, then enter HOME/AWAY temperatures. The optional sensors screen accepts an outdoor temperature sensor for outdoor correction and a `weather.*` entity for sunrise learning. Review the heating settings and optional sensors on the final **Confirm settings** screen before completing setup.

Indoor and outdoor temperature sensors and the selected weather entity are checked at startup and every five minutes. One unavailable or unknown check reuses the last valid value once. After two consecutive misses, an unavailable indoor sensor stops heating and excludes the learning cycle. Optional sensors are omitted from the features that depend on them. New overheating readings take effect at the next check.

## 4. Check the first run

1. Verify that the new Climate starts in OFF and shows the connected indoor temperature.
2. Select HEAT to check basic control and activity feedback. Consider the target, current temperature and minimum OFF wait together.
3. Check the physical switch state or the existing thermostat's reported upper and lower setpoints as heat requests change.
4. Open the **바닥난방** sidebar panel, select the room and check control status, faults and heating information.
5. Select AUTO after confirming basic operation. It may initially use basic control while evidence is insufficient.

Normal operation collects learning data. Repeatedly changing the target or forcing the heater on and off is unnecessary. Allow time after shutdown for the residual Peak and cooling observations; see [Operation and learning](operation.en.md).
