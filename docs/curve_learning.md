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
  its own three-hour limit measured from OFF.
- A peak is confirmed by the first actual temperature decline. The temperature
  and timestamp of the report immediately before that decline define Peak,
  including the last report on a flat plateau. A timer cannot confirm Peak.
  A restart or reload
  ends an active episode as incomplete rather than joining data across runs.

## Learning and storage

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
- SQLite schema 2 additively migrates schema 1, preserving standards and metadata.
  Up to 24 independent completed OFF profiles per heating type survive raw TTL
  cleanup. Profiles contain ON duration, measured slope at OFF and five-minute
  held OFF-to-Peak temperature trajectories, including the exact Peak endpoint.
- The default model and curve model both learn independently of the selector.
  The default model learns equivalent OFF-response hours from measured rise
  divided by slope at OFF; its prediction multiplies this learned time by the
  current slope. Thermal model schema 4 retains schemas 1–3 and adds compact OFF
  profiles, including valid zero-rise responses.
- Curve OFF prediction combines comparable measured OFF trajectories, requires
  a compatible active ON shape and honors minimum ON. Both predictors require
  three comparable profiles; duration tolerance is max(10 minutes, 35% of current
  duration), current/observed slope ratio is 0.7–1.3. Weighted response dispersion
  and sample count determine confidence, which must be at least 0.25. Confidence
  gates prediction rather than shrinking the physical peak estimate.
- Curve predictive ON integrates continuously covered cooling buckets over the
  learned heating response delay (half for balanced, full for comfort). Missing
  coverage falls back to the default slope-based ON estimator; eco skips it.
- Climate attributes expose `off_prediction` and `last_peak_comparison` for the
  selected estimate and independent model errors at an actual OFF event. Errors
  are predicted minus observed Peak in degrees Celsius. The last completed
  comparison survives restart; an active comparison does not.
- Learned OFF-baseline peak/time bounds restart waiting. Legacy aggregate
  residual/peak-delay waiting remains available when no new OFF prediction exists;
  legacy scalar residual alone no longer enables predictive OFF.
- Current/Long-term blending and regime-change adaptation remain separate future
  work; this version preserves the existing EWMA standards and bounded OFF profiles.
