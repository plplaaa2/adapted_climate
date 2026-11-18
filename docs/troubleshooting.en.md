# Troubleshooting

[Guide index](README.en.md) · [한국어](troubleshooting.md)

## Setup and first run

| Symptom | Checks |
| --- | --- |
| Sensor cannot be selected | Entity exists, state is numeric, device class is temperature and unit is °C/°F. |
| Thermostat cannot be selected | Supports heat, one setpoint and actual `hvac_action`; requested lower/upper values are within its range. |
| Duplicate device error | The heater is not already owned by another room/entry. |
| New configuration is missing from the panel | Confirm setup/entity creation, refresh the page, then check HA logs for setup errors. |

## Unexpected heating behavior

| Symptom | Checks |
| --- | --- |
| Room is below target but heat has not started | Mode, start differential, minimum OFF wait, faults and residual waiting. The OFF wait is the larger of the configured base and twice the previous continuous runtime. |
| Heat does not stop immediately at target | HEAT/AUTO stop differential, minimum ON time and actual device feedback. AUTO uses basic thresholds when predictive evidence is insufficient. |
| Climate continues heating after a stop request | The thermostat's own temperature may be below the requested lower setpoint. Sending a lower setpoint does not prove actual heating stopped. |
| Integrated multi-room group stops or faults | All switches must report the same valid state. |
| Scheduled stop time changes | Check the active model and evidence in Prediction Analysis; a qualified hybrid decision can revise the initial runtime plan. |

Resolve the fault cause first. Confirm a valid sensor, actual heater OFF and the minimum OFF wait before selecting HEAT/AUTO again to release a fault lock. Check other automations and schedules controlling the same device.

## Learning and prediction

| Symptom | Checks |
| --- | --- |
| Initial learning is incomplete after several starts | Check qualifying cycles. Missing Peaks, manual changes, AWAY, sunrise effects and invalid observations can exclude a cycle from the minimum three. |
| Peak observation takes a long time | Real falling reports are required. Repeating a low value or waiting alone cannot confirm a Peak. MAE and sunrise evidence also determine whether the time limit applies. |
| Basic control remains after three accepted cycles | Current outdoor/runtime conditions may lack applicable evidence. Three cycles do not guarantee every prediction. |
| Old timeouts were not recovered | The original indoor sensor's Recorder history must remain available. Derived Climate history and held five-minute bins cannot replace it. Manual/sunrise exclusions are not recovery candidates. |
| Older raw charts are absent | Check the seven-day raw and thirty-day cycle retention windows. Learned summaries are stored separately. |

## Collecting diagnostics

1. Select the affected room in the **바닥난방** sidebar panel.
2. Use **Learning Analysis → Learning data JSON download** for cycles and learning evidence.
3. Use **Prediction Analysis → Prediction diagnostic JSON download** for current state and the latest 24 hours of related Recorder history. Filters, retention and permissions can limit the contents.
4. Record the event time/timezone, target/current temperature, mode, preset, expected behavior and actual behavior.
5. Check HA logs for warnings/errors from `custom_components.adaptive_floor_heating`. Collect data soon after the event, before detailed history expires.

See [Maintenance](maintenance.en.md) for backups and historical recovery.
