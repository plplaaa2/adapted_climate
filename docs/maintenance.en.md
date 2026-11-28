# Maintenance

[Guide index](README.en.md) · [한국어](maintenance.md)

## Routine checks

- Check indoor/outdoor measurements and actual heating activity feedback.
- Compare predicted and measured Peaks over several cycles. Record conditions before changing multiple settings at once.
- Review exclusion reasons in Learning Analysis. Interruptions, AWAY operation and sunrise effects mean total run counts can differ from accepted learning counts.

## Configuration changes

Integration options let you change temperature differentials, operating and protection times, HOME/AWAY temperatures, the outdoor temperature sensor, and the weather entity used for sunrise learning. Climate options also let you change the external thermostat's lower and upper setpoints. Review the summary on the final **Confirm settings** screen before saving. Saving options stops heating and reloads the integration, so wait for active learning to finish.

Use the Climate or panel for routine target, mode and preset changes. Predictive operation settings contain Predictive ON, the Peak observation limit and the sunrise-saving schedule.

**Wait for heating to stop before reloading or reinstalling the integration.** Reloading or reinstalling with the heater already confirmed OFF clears the previous minimum OFF wait. If unloading stops a running heater, the restart delay begins at that confirmed stop. For Climate control, the integration confirms OFF when the connected thermostat reports the lower setpoint or its HVAC mode is `off`.

## Updating

1. Check that both heating and the post-OFF residual Peak/cooling observations have finished.
2. Download diagnostic JSON if needed and make a Home Assistant backup.
3. Select OFF and confirm the device actually stops. For Climate control, inspect activity rather than just the requested lower setpoint.
4. Update through HACS or replace the integration files manually, then restart Home Assistant.
5. Check the indoor sensor, actual OFF state, faults and learned records before selecting HEAT/AUTO.

Completed learning is restored from storage. Interrupted observations are not resumed. A routine update does not require a learning reset.

## Backups and retention

Runtime intent and basic learning use Home Assistant `.storage`; curve/cycle data uses `.storage/adaptive_floor_heating_<entry_id>_curves.sqlite3`. Check that recovery backups include configuration, `.storage` and the Recorder database when needed. Use a Home Assistant backup or a consistent backup while stopped rather than copying a single live SQLite file.

Confirmed heater states and saved stop and cooling forecasts are retained in the same SQLite database for up to 30 days. The upgrade preserves existing learning and curves and imports the last saved forecasts and available heater history from Recorder once per entry. A successful import is marked complete even when it finds no records. A failed read or write is retried on the next startup. Home Assistant Recorder continues to supply indoor and target temperature history.

JSON downloads are diagnostic exports. The panel currently has no import feature for restoring learning from them; they do not replace a recovery backup.

| Data | Retention |
| --- | --- |
| Per-cycle five-minute raw buckets | Seven days |
| Detailed cycle records | Thirty days |
| Learning summaries, accumulated forecast comparisons and eligible runtime average | Stored separately from details and restored after restart |
| HA Recorder history | Controlled by HA recording filters and retention policy |

Expired details do not mean all learning has been reset. Export data before expiration when longer analysis is needed.

## Learning history migration

An administrator can select a room under **바닥난방 → Learning Analysis** and press **Migration**. Recovery rechecks retained timeouts using the connected indoor sensor's Recorder history **within the original observation interval**. A newly confirmed Peak contributes to runtime and residual heat prediction models when it meets the quality criteria.

When sunrise saving is enabled, recovery also reviews retained sunrise exclusions for heating and cooling using the configured window. The panel shows recovery counts and reasons for skipped steps. The original verdict and recovery evidence are preserved, and repeat runs cannot count a recovered cycle twice.

For rooms with one heater, migration also checks heating runs excluded with `HEATER_UNAVAILABLE`. Recorder must retain the actual heating start and stop events and indoor temperature history. If the heater went offline after stopping and returned in the same OFF state within the current learning grace period, migration rechecks the Peak and learning quality against the recorded temperatures. A zero grace period or insufficient history leaves the original verdict in place.

The same button recovers outdoor temperatures from retained heating cycles and the outdoor sensor's Recorder history. It recovers the time-weighted ON-to-OFF outdoor mean for the runtime model and the temperature at confirmed OFF for the residual model. Run it when you want to recover missing outdoor values. Cycles with missing weather still contribute to the shared runtime model.

## Learning reset

Consider a reset after a substantial sensor-location or heating-system change that makes the existing model unsuitable. Routine updates or temporary lack of prediction support do not by themselves require one.

1. Export learning and diagnostic JSON.
2. Select OFF and confirm actual heater OFF with no pending commands.
3. Run Learning reset for the intended room/device.
4. Return to the desired mode and collect qualifying cycles again; runtime-based predictive OFF starts with the three-cycle minimum.

Reset clears that device's learning and its prediction comparisons, while retaining configuration and HA Recorder history. Make a recovery backup first if you may need to restore the previous learning.
