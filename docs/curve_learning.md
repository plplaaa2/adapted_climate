# Five-minute curve learning

This document describes the implemented development model. The existing thermal
EWMA model remains the default AUTO estimator. The `learning_model` Select can
choose the curve estimator, and insufficient curve data falls back to the
existing estimator. Live Home Assistant and physical-heating validation remain
pending.

## Observation contract

- One config entry owns four independent standards: `COLD_HEATING`,
  `WARM_HEATING`, `PREDICTIVE_WARM_HEATING`, and `COOLING`.
- Confirmed aggregate switch transitions define ON and OFF. An uninterrupted
  observed OFF period of at least 60 minutes classifies a threshold start as
  cold; predictive starts use their own curve. An external switch change
  invalidates the affected episode.
- The tracker samples the last valid registered room-sensor state at each
  five-minute boundary relative to confirmed ON. A held unchanged state
  produces `delta_c=0`; an unavailable or invalid state invalidates the
  segment. These ticks do not create reports in the existing thermal model.
- The three heating curves contain ON through the confirmed post-OFF peak.
  The cooling curve begins at that peak and ends at the next ON, sustained
  warming, or three hours after OFF, whichever occurs first.
- A peak is confirmed after ten minutes without a higher reported temperature.
  A higher temperature resets the candidate peak time. A restart or reload
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
- The curve estimator derives response delay from the first learned positive
  heating bucket. Early stop uses only the learned OFF-to-peak rise, requires
  an active shape similar to the matching standard and honors minimum ON time.
  Peak delay bounds post-OFF waiting. Missing estimates use the existing
  learned-control path. The Climate attributes expose the selected model,
  fallback reason, curve phase, accepted/rejected counts, and last quality
  reason.
