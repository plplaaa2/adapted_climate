# Operation and learning

[Guide index](README.en.md) · [한국어](operation.md)

## Modes and presets

| Mode | Behavior |
| --- | --- |
| OFF | Disables automatic control and requests a stop; verify the device actually stops. |
| HEAT | Uses the target, start/stop differentials and minimum ON/OFF times. |
| AUTO | Adds learned predictive starts and stops to basic control. |

HOME/AWAY changes the room target. Stable targets and occupancy conditions help collect normal learning cycles. AWAY cycles are excluded from common learning. Target, mode or preset changes and external operation can interrupt a cycle and exclude it.

## Runtime limit and heater outages

**Maximum continuous runtime** applies to every HEAT and AUTO run regardless of learning count. The heater stops earlier if it reaches the target or predictive OFF calls for a stop. A run that reaches the limit still enters residual heat observation and can contribute to learning if it passes the existing quality checks. The normal minimum OFF wait applies after a limit-triggered stop.

For Climate control, runtime starts when the connected thermostat reports the configured upper setpoint (for example, 30°C) and ends when it reports the lower setpoint (for example, 18°C). The integration uses this interval for the runtime limit and learning. While the upper setpoint remains in effect, the thermostat can alternate between heating and idle without interrupting AUTO or the runtime record. Existing learning data remains available after updating.

Climate ON/OFF target commands have a configurable confirmation delay, defaulting to one second. If the connected ESPHome Climate briefly reports the previous target after a request, the integration does not resend the command during the wait. At the end, it confirms against the latest reported target. Actual heating starts and stops continue to follow `hvac_action`.

Indoor/outdoor temperatures and the selected weather input are checked at startup and every five minutes. One missed check reuses the last valid value. After two consecutive misses, a missing indoor temperature stops heating and excludes the active cycle from learning. Optional sensors are omitted from learning and correction that depend on them. New overheating readings take effect at the next five-minute check.

When the heater's state is temporarily unknown, the integration holds temperature reports during the configured learning grace period. If the heater returns in the same state within that period, the reports are processed in order. If the grace period expires or the heater returns in a different state, the active cycle is excluded with `HEATER_UNAVAILABLE`. Heating control continues to use its existing 10-second outage detection threshold.

## Initial learning and predictive control

After installation or reset, normal operation collects data without a forced training procedure. Runtime-based predictive OFF needs at least **three qualifying heating cycles** with measured runtime and a confirmed residual Peak. Switch operation counts alone do not meet this requirement.

Three cycles do not guarantee support for every outdoor temperature or runtime. Basic control remains available when the current conditions lack evidence. Predictive ON is evaluated separately using cooling and measured heating response delay.

- **Predictive ON** can start early based on expected cooling and the delay before heat affects the room. Minimum OFF and safety conditions still apply.
- At the start decision, **indoor temperature at or below target minus the start differential** takes normal-heating priority. Only an early start above that threshold is classified as Predictive ON. With a 23°C target and a 0.5°C differential, 22.5°C and below use normal heating; 22.6°C and above can use Predictive ON when its evidence qualifies. The same rule applies to AWAY-to-HOME and target changes.
- **Predictive OFF** first schedules a stop from measured runtime samples, giving higher weight to similar outdoor temperatures. If enough cycles have comparable runtime and weather, a residual forecast at or above the target can advance the stop. When residual evidence is insufficient, AUTO follows the scheduled stop. If no valid schedule is available, basic temperature control continues collecting observations.
- **Residual observation** continues after shutdown while the room may still warm. It is needed to confirm the Peak and compare the original forecast with the result.

### Early-stop limits

The measured runtime floor may be relaxed by up to 20% before ten accepted heating cycles, then by a base allowance of up to 10%. If outdoor weighting reduces predicted runtime, it can earn up to 5% of additional allowance on the measured floor.

For regular heating, the final runtime respects the configured minimum ON time, 60% of the latest ten eligible runtimes, and the existing shortfall floor. Only accepted cycles without a control-learning hold enter the average. A runtime below 60% of the average available when heating starts is excluded from the average. Eligibility for prediction learning is assessed independently. The average is shared across outdoor bands and survives restarts and detail cleanup.

## Peak confirmation and observation termination

A real sensor report must fall **at least 0.1°C below the maximum**, followed by another real report falling **at least 0.1°C from the previous report**. For `23.9 → 23.8 → 23.7°C`, 23.9°C becomes the confirmed Peak. Repeated low values or elapsed time alone do not confirm it. An actual heating response must also have been observed.

Confirmation time differs from Peak time: Peak time is the last real report of that maximum temperature.

| Evidence at actual OFF | Observation policy |
| --- | --- |
| MAE over up to ten recent comparisons is greater than 0.5°C, or required evidence is missing | Apply the observation limit: six hours by default, adjustable from 1–12 hours in predictive operation settings. |
| MAE is at most 0.5°C and the original forecast and sunrise information exist | Do not apply that time limit. After sunrise, more than 0.5°C above the frozen predicted Peak ends observation and excludes learning. |

The policy is selected from evidence at OFF. Manual interruptions, invalid sensors, pre-Peak rebound and other termination rules also remain active.

## Sustained cooling observations

When a new cooling cycle qualifies for learning, the integration checks its five-minute readings from the confirmed peak. It records the start of the first continuous 15-minute window in which the temperature never rises and drops by at least **0.1°C** in total.

Each subsequent complete 15-minute window records the **cooling rate (°C/h)** and **cooling acceleration (°C/h²)**. Positive acceleration indicates faster cooling than in the previous window. The first window has no acceleration value because there is no preceding window to compare. A gap in the readings ends the calculation.

Find the summary under **Learning Analysis → Cooling cycle → Sustained cooling observations** or in the cycle's `analysis.cooling_decline` field in the learning JSON export. The summary remains available for the lifetime of the cycle record after the raw five-minute data expires. These diagnostics are collected for analysis. Cooling forecasts and heating control continue to use their existing calculations.

## Sunrise saving

Sunrise Saving becomes active after at least ten valid regular heating runtime samples and a supported predictive stop model are available. When a Sunrise Saving cycle stops predictively, ten usable regular heating response delays allow the integration to wait from the last confirmed start until their 75th percentile (the eighth value in ascending order). The configured minimum OFF time and twice the last confirmed runtime still apply.

Enable Sunrise saving in predictive operation settings and save both start and end times. During the scheduled window and from sunrise to sunset, AUTO lowers its predictive stop target from **target plus upper tolerance** to **target**. During daylight, the lower stop target also applies to basic temperature control.

The schedule follows Home Assistant local time and can cross midnight. Equal start and end times disable the scheduled window. Sunrise-saving cycles contribute their heating intervals to the dedicated **Sunrise saving heating** curve in Learning Analysis. Confirmed peaks near the setpoint contribute separate residual samples, with an initial goal of 10.

Sunrise saving calculates its predictive stop runtime floor from dedicated samples. While those samples are limited, it scales the regular heating average to the temperature rise needed for Sunrise saving. The configured minimum ON time applies to both modes.

The **Solar gain** curve starts observing 60 minutes after Home Assistant's sunrise time. It records indoor temperature, solar elevation, and sunny conditions every five minutes while the heater is off. The peak is confirmed once at least 60 minutes have passed since the last peak reading and the temperature meets the decline criteria. A repeat reading at that maximum or a higher reading restarts the 60-minute wait. If the heater turns on, that observation ends.

## Learning readiness

The integration's device page in Home Assistant includes three diagnostic binary sensors named **Predictive OFF Ready**, **Predictive ON Ready**, and **Sunrise Saving Ready**. A sensor turns on when the corresponding feature has enough learned evidence to make a prediction. Feature enablement and current operation are reported separately. Sensor attributes show available models and why readiness conditions have yet to be met.

## Reading the panel

| View | What to check |
| --- | --- |
| Dashboard | Selected room, temperature, control state, heating information and recent results |
| Prediction Analysis | Initial schedule, active model, predicted/measured Peak and forecast errors |
| Learning Analysis | Acceptance/exclusion reasons, Current/Long-term evidence and observations |

Heating and Cooling are separate records even within one run. Active cycles appear after completion. Sample count and internal model confidence do not equal measured accuracy; check stored forecast-to-outcome comparisons as well.
