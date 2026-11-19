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

When the heater's state is temporarily unknown, the integration holds temperature reports during the configured learning grace period. If the heater returns in the same state within that period, the reports are processed in order. If the grace period expires or the heater returns in a different state, the active cycle is excluded with `HEATER_UNAVAILABLE`. Heating control continues to use its existing 10-second outage detection threshold.

## Initial learning and predictive control

After installation or reset, normal operation collects data without a forced training procedure. Runtime-based predictive OFF needs at least **three qualifying heating cycles** with measured runtime and a confirmed residual Peak. Switch operation counts alone do not meet this requirement.

Three cycles do not guarantee support for every outdoor temperature or runtime. Basic control remains available when the current conditions lack evidence. Predictive ON is evaluated separately using cooling and measured heating response delay.

- **Predictive ON** can start early based on expected cooling and the delay before heat affects the room. Minimum OFF and safety conditions still apply.
- At the start decision, **indoor temperature at or below target minus the start differential** takes normal-heating priority. Only an early start above that threshold is classified as Predictive ON. With a 23°C target and a 0.5°C differential, 22.5°C and below use normal heating; 22.6°C and above can use Predictive ON when its evidence qualifies. The same rule applies to AWAY-to-HOME and target changes.
- **Predictive OFF** first schedules a runtime-based stop. When qualified, the hybrid model reevaluates the decision using the current temperature and expected residual Peak. The initial schedule is not an unconditional promise of the final stop time.
- **Residual observation** continues after shutdown while the room may still warm. It is needed to confirm the Peak and compare the original forecast with the result.

### Early-stop limits

The measured runtime floor may be relaxed by up to 20% before ten accepted heating cycles, then by a base allowance of up to 10%. Outdoor and forecast-error corrections can subsequently earn a separate allowance of up to 5%. These allowances relax support limits; they are not unconditional deductions from every calculated runtime.

The final runtime respects minimum ON, 60% of the latest ten eligible runtimes, and the existing shortfall floor. Only accepted cycles without a control-learning hold enter the average. A runtime below 60% of the average available before its ON edge is excluded from the average without automatically changing its prediction-learning eligibility. The average is global across outdoor bands and survives restarts and detail cleanup.

## Peak confirmation and observation termination

A real sensor report must fall **at least 0.1°C below the maximum**, followed by another real report falling **at least 0.1°C from the previous report**. For `23.9 → 23.8 → 23.7°C`, 23.9°C becomes the confirmed Peak. Repeated low values or elapsed time alone do not confirm it. An actual heating response must also have been observed.

Confirmation time differs from Peak time: Peak time is the last real report of that maximum temperature.

| Evidence at actual OFF | Observation policy |
| --- | --- |
| MAE over up to ten recent comparisons is greater than 0.5°C, or required evidence is missing | Apply the observation limit: six hours by default, adjustable from 1–12 hours in predictive operation settings. |
| MAE is at most 0.5°C and the original forecast and sunrise information exist | Do not apply that time limit. After sunrise, more than 0.5°C above the frozen predicted Peak ends observation and excludes learning. |

The policy is selected from evidence at OFF. Manual interruptions, invalid sensors, pre-Peak rebound and other termination rules also remain active.

## Sunrise saving

Enable Sunrise saving in predictive operation settings and save both start and end times. During the window, AUTO lowers its predictive stop target from **target plus upper tolerance** to **target**.

The schedule follows Home Assistant local time and can cross midnight. Equal start and end times disable the window. Sunrise-saving cycles are recorded separately from common learning. This user-defined schedule differs from post-sunrise observation termination triggered by excess above the original Peak prediction.

## Reading the panel

| View | What to check |
| --- | --- |
| Dashboard | Selected room, temperature, control state, heating information and recent results |
| Prediction Analysis | Initial schedule, active model, predicted/measured Peak and forecast errors |
| Learning Analysis | Acceptance/exclusion reasons, Current/Long-term evidence and observations |

Heating and Cooling are separate records even within one run. Active cycles appear after completion. Sample count and internal model confidence do not equal measured accuracy; check stored forecast-to-outcome comparisons as well.
