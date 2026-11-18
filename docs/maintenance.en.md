# Maintenance

[Guide index](README.en.md) · [한국어](maintenance.md)

## Routine checks

- Check indoor/outdoor measurements and actual heating activity feedback.
- Compare predicted and measured Peaks over several cycles. Record conditions before changing multiple settings at once.
- Review exclusion reasons in Learning Analysis. Interruptions, AWAY operation and sunrise effects mean total run counts can differ from accepted learning counts.

## Configuration changes

Integration options edit differentials, minimum ON/OFF times, HOME/AWAY temperatures and the outdoor sensor. Climate options also edit the external thermostat's lower/upper request setpoints. Saving options stops heating and reloads the integration, so wait for active learning to finish.

Use the Climate or panel for routine target, mode and preset changes. Predictive operation settings contain Predictive ON, the Peak observation limit and the sunrise-saving schedule.

## Updating

1. Check that both heating and the post-OFF residual Peak/cooling observations have finished.
2. Download diagnostic JSON if needed and make a Home Assistant backup.
3. Select OFF and confirm the device actually stops. For Climate control, inspect activity rather than just the requested lower setpoint.
4. Update through HACS or replace the integration files manually, then restart Home Assistant.
5. Check the indoor sensor, actual OFF state, faults and learned records before selecting HEAT/AUTO.

Completed learning is restored from storage. Interrupted observations are not resumed. A routine update does not require a learning reset.

## Backups and retention

Runtime intent and basic learning use Home Assistant `.storage`; curve/cycle data uses `.storage/adaptive_floor_heating_<entry_id>_curves.sqlite3`. Check that recovery backups include configuration, `.storage` and the Recorder database when needed. Use a Home Assistant backup or a consistent backup while stopped rather than copying a single live SQLite file.

JSON downloads are diagnostic exports. The panel currently has no import feature for restoring learning from them; they do not replace a recovery backup.

| Data | Retention |
| --- | --- |
| Per-cycle five-minute raw buckets | Seven days |
| Detailed cycle records | Thirty days |
| Learning summaries, accumulated forecast comparisons and eligible runtime average | Stored separately from details and restored after restart |
| HA Recorder history | Controlled by HA recording filters and retention policy |

Expired details do not mean all learning has been reset. Export data before expiration when longer analysis is needed.

## Historical timeout recovery

Startup rechecks retained timeout exclusions using actual indoor sensor history from Recorder **within the original observation interval**. A newly confirmed Peak updates the verdict and contributes to runtime/residual prediction models subject to quality gates.

Original verdicts, buckets and recovery evidence are preserved; recovery is idempotent. Error comparisons require the stored original forecast. Historical curves do not rewind newer curve memory. Missing source history, manual interruptions and sunrise exclusions remain unchanged. Recovery needs the **connected indoor sensor's own Recorder history**, rather than temperatures displayed by the derived Climate.

## Learning reset

Consider a reset after a substantial sensor-location or heating-system change that makes the existing model unsuitable. Routine updates or temporary lack of prediction support do not by themselves require one.

1. Export learning and diagnostic JSON.
2. Select OFF and confirm actual heater OFF with no pending commands.
3. Run Learning reset for the intended room/device.
4. Return to the desired mode and collect qualifying cycles again; runtime-based predictive OFF starts with the three-cycle minimum.

Reset clears that device's learning and its prediction comparisons, while retaining configuration and HA Recorder history. Make a recovery backup first if you may need to restore the previous learning.
