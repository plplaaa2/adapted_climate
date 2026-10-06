# Five-minute curve learning

This document describes the implemented development model. The existing thermal
EWMA model remains the default AUTO estimator. The `learning_model` Select can
choose the curve estimator, and insufficient curve data falls back to the
existing estimator. Live Home Assistant and physical-heating validation remain
pending.

## Observation contract

- One config entry owns four independent standards: `COLD_HEATING`,
  `WARM_HEATING`, `PREDICTIVE_WARM_HEATING`, and `COOLING`.
- Confirmed aggregate switch transitions define ON and OFF. A continuous AWAY
  preset lasting at least three hours arms Cold on return to HOME. Cold takes
  precedence over predictive starts and remains armed until a Cold heating
  segment is accepted. Long HOME OFF periods alone do not classify Cold.
  AWAY segments never update curve standards. External switch changes invalidate
  the affected episode.
- The tracker samples the last valid registered room-sensor state at each
  five-minute boundary relative to confirmed ON. A held unchanged state
  produces `delta_c=0`; an unavailable or invalid state invalidates the
  segment. These ticks do not create reports in the existing thermal model.
- The three heating curves contain ON through the confirmed post-OFF peak.
  The cooling curve begins at that peak and ends at the next ON, sustained
  warming, or three hours after Peak, whichever occurs first. Peak waiting has
  its own four-hour observation limit measured from OFF.
- Both observers confirm Peak after a heating response, a real report at least
  0.3 C below the observed maximum, and the next distinct report falling at least
  another 0.1 C. Repeated equal temperatures, timers and duplicate/out-of-order
  reports cannot confirm Peak. A report above the 0.3 C threshold resets the
  candidate; a new maximum also updates Peak. Below-threshold reports without
  the additional 0.1 C fall become the next candidate, so two adjacent reports
  must satisfy the additional-fall condition. There is no ten-minute decline timer.
  The last report at the maximum defines Peak, not the confirmation timestamp.
  Initial cooling before response and a single downward report do not end learning.
  After OFF, a measured rebound of at least 0.1 C from the observed trough, with
  at least ten elapsed minutes, confirms delayed response. Unresponsive episodes
  expire as incomplete. A timer alone cannot confirm Peak.
  A restart or reload
  ends an active episode as incomplete rather than joining data across runs.
  Shared OFF profile validation, memory prediction and restore use the same
  240-minute maximum. ON-to-Peak raw sampling continues through the OFF horizon
  instead of truncating at six hours from ON. Cooling observation remains three
  hours after the last measured Peak, independently of confirmation time.

## Learning and storage

### Cycle investigation (SQLite schema 5)

The sidebar Learning Analysis tab is organized around cycle records, with four
curve summaries, type/acceptance filters, recent 30/100 results and a selectable
detail view. Heating and Cooling records share a cycle ID but remain separate
segments. Records appear only when observation ends; rejection counts do not
count physical switch transitions. The completed-Peak comparison on the dashboard
is a separate runtime diagnostic and is not the rejected-cycle ledger.

`adaptive_floor_heating/curve_cycles` accepts `entity_id`, `limit` (1–100, default
30), optional `curve_type`, and optional boolean `accepted`. It enforces the same
Climate registry ownership and entity-read permission as `curve_memory`. Reads
use a worker thread, a serialized store lock and a read-only SQLite snapshot;
they do not open/migrate/create the database or run predictors, learning or control.
Results are ordered by observation end time descending; the list displays start
time and both ID and curve type identify selection.

Schema 5 retains schemas 1–4 and adds nullable measured start/OFF/Peak temperatures,
OFF slope, residual rise, Peak delay, ON duration, original bucket count and
versioned JSON evidence. Evidence captures quality measurements and the exact
limits used at save time, reference buckets used for deviation checks, Current
means/evidence before and after, curve/OFF confidence evaluated at the same saved
timestamp, and actual Long-term promotion outcomes across buckets and response
groups. All of these commit atomically with cycle and memory updates. Failed
transactions or duplicate IDs cannot change stored evidence or learning.

The final quality reason follows the existing first-failure ordering; the detail
table records all independent conditions for investigation, not additional
rejection events. Peak observation and Peak-delay quality bounds are four hours;
other acceptance checks, confidence gates and controller safety remain unchanged.
New evidence records the 0.3 C first fall, additional 0.1 C fall, actual confirming
drop/report count and four-hour observation limit; old evidence is not rewritten.
Confidence remains zero for fewer than three statistical observations;
this is separate from whether a cycle passes quality checks.

Legacy rows expose their stored reasons and times. Durations can be calculated
from exact stored timestamps, but missing temperatures, historical thresholds,
learning transitions and promotion outcomes are not fabricated. Original bucket
counts migrate only when raw rows still exist. Raw buckets expire after seven
days; schema-5 cleanup marks actual pruning and preserves metadata/evidence/counts.
Older absent raw rows report unavailable, rather than claiming a known count or
confirmed expiry. UI timestamps follow the HA timezone; absolute temperatures
and delta/slope conversions follow the HA display unit correctly. Charts preserve
zero and gaps; Heating buckets extend from ON through Peak, Cooling from Peak.

- The first accepted delta initializes each curve bucket. Later accepted
  deltas update it with `0.8 * old + 0.2 * new` independently per bucket.
  A measured zero is accepted. Empty or invalid buckets do not update it.
- A complete segment needs at least two buckets. An invalid sensor, external
  override, manual target/mode change, impossible five-minute delta, or
  substantial deviation from an established curve excludes it. The current
  deviation thresholds (0.35°C for at least three comparable buckets, or
  cumulative drift above the configured code threshold) are development
  defaults requiring field calibration.
- A separate SQLite file under HA `.storage` holds each entry's cycle metadata,
  seven-day raw five-minute buckets, and persistent standard buckets. Cycle
  identity plus curve type prevents duplicate aggregate updates. SQLite writes
  run outside the HA event loop. No seasonal reset or season summary exists.
- SQLite schema 4 additively migrates schemas 1–3, preserving standards and metadata.
  Up to 24 independent completed OFF profiles per heating type survive raw TTL
  cleanup. Profiles contain ON duration, measured slope at OFF and five-minute
  held OFF-to-Peak temperature trajectories, including initial dips and the exact
  Peak endpoint. Optional context contains start temperature, ON displacement,
  pre-ON slope and elapsed time since the preceding confirmed OFF. Unknown
  pre-ON slope/OFF duration remains unknown, including across restart.
- The default model and curve model both learn independently of the selector.
  The default model learns equivalent OFF-response hours from measured rise
  divided by slope at OFF; its prediction multiplies this learned time by the
  current slope when both slopes exceed 0.1 C/h. At lower slopes it directly
  averages measured rise at comparable thermal states, without division by slope.
  Thermal model schema 5 retains schemas 1–4 and permits contextual compact OFF
  profiles, including valid zero-rise responses and zero-time peaks.
- Curve OFF prediction combines comparable measured OFF trajectories, requires
  a compatible active ON shape and honors minimum ON. Both predictors require
  three comparable profiles; duration tolerance is max(10 minutes, 35% of current
  duration). Rising-state slope ratio is 0.7–1.3; delayed-state absolute slope
  difference is at most 0.2 C/h and requires context. Start-temperature, ON-delta
  and pre-slope differences must be within 1 C, 0.3 C and 0.3 C/h, respectively;
  prior-OFF duration tolerance is max(30 minutes, 50% of the shorter duration).
  One-sided unknown conditions refuse matching; two unknowns reduce weight by
  0.75 per condition. State similarity also gates delayed confidence.
  Weighted response dispersion
  and sample count determine confidence, which must be at least 0.25. Confidence
  gates prediction rather than shrinking the physical peak estimate.
- Curve predictive ON integrates continuously covered cooling buckets over the
  learned heating response delay (half for balanced, full for comfort). Missing
  coverage falls back to the default slope-based ON estimator; eco skips it.
- Climate attributes expose `off_prediction` and `last_peak_comparison` for the
  selected estimate and independent model errors at an actual OFF event. Errors
  are predicted minus observed Peak in degrees Celsius. The last completed
  comparison survives restart; an active comparison does not.
- Learned OFF-baseline peak/time bounds restart waiting before predictive ON.
  A measured drop greater than 0.5 C below OFF releases this forecast for
  reevaluation. Legacy aggregate waiting is confined to models without OFF
  profiles/memory; a failed modern condition match cannot reuse a global mean.
  legacy scalar residual alone no longer enables predictive OFF.
- Four independent Current/Long-term memories store ON/cooling EWMA buckets and
  conditional OFF response curves. Current alpha is 0.2; only stable Current
  initializes Long-term, then updates it with alpha 0.02. At least five independent
  observations and confidence >=0.7 are required (six observations when variance
  is zero). ON/cooling promotion needs a contiguous stable prefix of two buckets.
- Every bucket has mean, EWMA variance, evidence count and update time. Old means
  migrate with unknown variance and zero validated statistical evidence. Promotion
  counts are separate from independent observations. Duplicate IDs and failed
  transactions cannot change either memory layer.
- Confidence combines sample count and variance; Current freshness decays linearly
  to zero after 30 days while Long-term remains usable across a non-heating season.
  Both qualified layers blend using Current confidence as weight; missing or weak
  Current uses qualified Long-term. Missing coverage still falls back safely.
- OFF memories use duration bins of 30 minutes and slope bins of 0.5 C/h, each
  with observed time-to-peak and 21 normalized-time response points. Newly
  observed thermal context is stored independently by regime and context
  bins (1 C start, 0.3 C ON delta, 0.3 C/h pre-slope, 30 minutes prior OFF).
  All points and condition statistics must be stable before promotion. A single blend weight over
  the entire response preserves a coherent curve. Up to 48 groups per type persist;
  full capacity preserves old Long-term groups rather than deleting seasonal memory.
- Current/Long-term payloads commit atomically with cycles in `curve_memory` and
  survive seven-day raw cleanup. A corrupt Long-term restores valid Current when
  possible. Old schema-2 profiles retain their real cycle update time and cannot
  masquerade as fresh Current indefinitely.
- `curve_memory` Climate attributes report layer and OFF confidence, coverage,
  promotions, and last Long-term update. `off_prediction.memory_source` and
  `current_weight` identify the actual selected/blended OFF memory.
- Regime-change versus anomaly adaptation remains a separate research item;
  existing cycle quality checks still apply.
