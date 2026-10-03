"""Keep independent Current/Long-term curve statistics; related: curve_learning.py, curve_storage.py, off_response.py."""

from dataclasses import dataclass
from math import isfinite, sqrt
import time

from .off_response import OffPrediction, interpolate, valid_profile, valid_context, match_state

CURRENT_ALPHA = 0.2
LONG_TERM_ALPHA = 0.02
MIN_PROMOTION_SAMPLES = 5
PROMOTION_CONFIDENCE = 0.7
MIN_PREDICTION_CONFIDENCE = 0.25
CURRENT_MAX_AGE = 30 * 86400.0
RESPONSE_GRID = 20
MAX_RESPONSE_GROUPS = 48
CONTEXT_FIELDS = {"ctx_start_temperature", "ctx_on_delta", "ctx_pre_slope", "ctx_off_minutes",
                  "ctx_pre_slope_known", "ctx_off_minutes_known"}


def _context_values(context: dict) -> dict:
    """Encode optional observed conditions without fabricating values; related: curve_storage.py."""
    values = {f"ctx_{key}": value if value is not None else 0.0 for key, value in context.items()}
    values.update({f"ctx_{key}_known": float(context[key] is not None)
                   for key in ("pre_slope", "off_minutes")})
    return values


def _context_from_values(values: dict) -> dict | None:
    if not CONTEXT_FIELDS.issubset(values):
        return None
    return {key: (None if key in ("pre_slope", "off_minutes") and values[f"ctx_{key}_known"] == 0
                  else values[f"ctx_{key}"])
            for key in ("start_temperature", "on_delta", "pre_slope", "off_minutes")}


def _response_scale(name: str) -> float:
    return 30 if name in ("duration", "peak_minutes", "ctx_off_minutes") else 0.5


@dataclass
class Bucket:
    """An EWMA mean with separately measured variance and independent evidence count."""

    mean: float
    variance: float | None = None
    samples: int = 0
    updated_at: float = 0.0
    promotions: int = 0

    def update(self, value: float, now: float) -> None:
        if self.samples == 0:
            # A migrated old mean has unknown variance. Start new evidence honestly.
            self.mean = (1 - CURRENT_ALPHA) * self.mean + CURRENT_ALPHA * value
            self.variance, self.samples = 0.0, 1
        else:
            delta = value - self.mean
            self.mean += CURRENT_ALPHA * delta
            self.variance = (1 - CURRENT_ALPHA) * ((self.variance or 0) + CURRENT_ALPHA * delta ** 2)
            self.samples += 1
        self.updated_at = now

    def confidence(self, now: float, *, long_term: bool = False, scale: float = 0.15) -> float:
        if self.variance is None or self.samples < 3:
            return 0.0
        freshness = 1.0 if long_term else max(0.0, 1 - max(0.0, now - self.updated_at) / CURRENT_MAX_AGE)
        return min(self.samples / 8, 1.0) / (1 + sqrt(self.variance) / scale) * freshness

    def stable(self, now: float, scale: float = 0.15) -> bool:
        return self.samples >= MIN_PROMOTION_SAMPLES and self.confidence(now, scale=scale) >= PROMOTION_CONFIDENCE

    def promote(self, current: "Bucket", now: float) -> None:
        """Absorb stable Current, never raw cycles; promotions are not independent samples."""
        if self.promotions == 0:
            self.mean, self.variance = current.mean, current.variance
        else:
            delta = current.mean - self.mean
            self.mean += LONG_TERM_ALPHA * delta
            self.variance = ((1 - LONG_TERM_ALPHA) * ((self.variance or 0) + LONG_TERM_ALPHA * delta ** 2)
                             + LONG_TERM_ALPHA * (current.variance or 0))
        self.samples = max(self.samples, current.samples)
        self.updated_at = now
        self.promotions += 1

    def dump(self) -> list:
        return [self.mean, self.variance, self.samples, self.updated_at, self.promotions]

    @classmethod
    def restore(cls, data) -> "Bucket":
        if not isinstance(data, list) or len(data) != 5:
            raise ValueError("Invalid memory bucket")
        mean, variance, samples, updated, promotions = data
        if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not isfinite(v)
                for v in (mean, updated)) or updated < 0 or abs(mean) > 2000
                or (variance is not None and (isinstance(variance, bool)
                    or not isinstance(variance, (int, float)) or not isfinite(variance) or variance < 0))
                or any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 1000000
                       for v in (samples, promotions)) or (samples > 0 and variance is None)
                or (promotions > 0 and samples < MIN_PROMOTION_SAMPLES)):
            raise ValueError("Invalid memory statistics")
        return cls(float(mean), variance, samples, float(updated), promotions)


def blended(current: Bucket | None, long_term: Bucket | None, now: float, *, scale=0.15):
    """Select qualified data and use Current confidence as its blend weight."""
    current_conf = current.confidence(now, scale=scale) if current else 0.0
    long_conf = long_term.confidence(now, long_term=True, scale=scale) if long_term else 0.0
    has_current = current_conf >= MIN_PREDICTION_CONFIDENCE
    has_long = long_conf >= MIN_PREDICTION_CONFIDENCE
    if has_current and has_long:
        weight = current_conf
        return ((1 - weight) * long_term.mean + weight * current.mean,
                weight * current_conf + (1 - weight) * long_conf, "blended", weight)
    if has_current:
        return current.mean, current_conf, "current", 1.0
    if has_long:
        return long_term.mean, long_conf, "long_term", 0.0
    return None


class CurveMemory:
    """Separate ON/Cooling bucket memory and operating-state-specific OFF trajectories."""

    def __init__(self) -> None:
        self.current: dict[int, Bucket] = {}
        self.long_term: dict[int, Bucket] = {}
        # Each OFF group has independent duration, slope, time-to-peak and 21 shape statistics.
        self.responses: dict[str, dict[str, dict[str, Bucket]]] = {}
        self.last_source: str | None = None
        self.last_weight = 0.0

    def learn_buckets(self, values: dict[int, float], now: float) -> None:
        for index, value in values.items():
            bucket = self.current.setdefault(index, Bucket(value))
            bucket.update(value, now)
        # Promote only a contiguous prefix with enough independent, stable evidence.
        prefix = []
        for index in range(max(self.current, default=-1) + 1):
            bucket = self.current.get(index)
            if bucket is None or not bucket.stable(now):
                break
            prefix.append(index)
        if len(prefix) >= 2:
            for index in prefix:
                # An accepted cycle may not contain a long tail; never promote untouched tails again.
                if index in values:
                    self.long_term.setdefault(index, Bucket(self.current[index].mean)).promote(self.current[index], now)

    def estimate(self, index: int, now: float | None = None):
        prediction = blended(self.current.get(index), self.long_term.get(index), time.time() if now is None else now)
        if prediction:
            self.last_source, self.last_weight = prediction[2:]
        return prediction

    def learn_response(self, profile: dict, now: float) -> None:
        if not valid_profile(profile):
            return
        # Nearby OFF states are learned independently, avoiding Cold/Warm condition mixing.
        key = f"{int(profile['duration'] // 30)}:{int(profile['slope'] // 0.5)}"
        # Isolate delayed responses and differing initial thermal states; related: off_response.py.
        context = profile.get("context")
        if context is not None:
            bins = [int(context[name] // width) if context[name] is not None else "unknown"
                    for name, width in (("start_temperature", 1), ("on_delta", 0.3),
                                        ("pre_slope", 0.3), ("off_minutes", 30))]
            key += f":{'delayed' if profile['slope'] <= 0.1 else 'rising'}:" + ":".join(map(str, bins))
        if key not in self.responses and len(self.responses) >= MAX_RESPONSE_GROUPS:
            # Preserve old long-term state instead of deleting seasonal memories.
            return
        group = self.responses.setdefault(key, {"current": {}, "long_term": {}})
        peak_time = profile["points"][-1][0]
        values = {"duration": profile["duration"], "slope": profile["slope"], "peak_minutes": peak_time}
        if context is not None:
            values.update(_context_values(context))
        values.update({str(i): interpolate(profile["points"], peak_time * i / RESPONSE_GRID)
                       for i in range(RESPONSE_GRID + 1)})
        for name, value in values.items():
            group["current"].setdefault(name, Bucket(value)).update(value, now)
        shape_stable = all(group["current"][str(i)].stable(now, scale=0.5) for i in range(RESPONSE_GRID + 1))
        conditions_stable = (group["current"]["duration"].stable(now, scale=30)
                             and group["current"]["slope"].stable(now, scale=0.5)
                             and group["current"]["peak_minutes"].stable(now, scale=30))
        conditions_stable = conditions_stable and all(
            group["current"][name].stable(now, scale=_response_scale(name))
            for name in CONTEXT_FIELDS if name in group["current"]
        )
        if shape_stable and conditions_stable:
            for name, bucket in group["current"].items():
                group["long_term"].setdefault(name, Bucket(bucket.mean)).promote(bucket, now)

    def predict_response(self, elapsed: float, slope: float | None, now: float | None = None,
                         *, context: dict | None = None):
        if (slope is None or not isfinite(slope) or not -10 <= slope <= 10
                or not isfinite(elapsed) or not 10 <= elapsed <= 1440):
            return None
        now = time.time() if now is None else now
        matches = []
        required = {"duration", "slope", "peak_minutes", *(str(i) for i in range(RESPONSE_GRID + 1))}
        for group in self.responses.values():
            fields = required | (CONTEXT_FIELDS if any(
                name in rows for rows in group.values() for name in CONTEXT_FIELDS
            ) else set())
            confidences = {}
            for layer, rows in group.items():
                confidences[layer] = (min(rows[name].confidence(
                    now, long_term=layer == "long_term",
                    scale=_response_scale(name),
                ) for name in fields) if fields.issubset(rows) else 0.0)
            current_conf, long_conf = confidences["current"], confidences["long_term"]
            has_current = current_conf >= MIN_PREDICTION_CONFIDENCE
            has_long = long_conf >= MIN_PREDICTION_CONFIDENCE
            if not has_current and not has_long:
                continue
            # A single confidence weight preserves a coherent monotonic OFF trajectory.
            # Per-point weights could create a false peak when Current and Long-term shapes differ.
            blend_weight = current_conf if has_current and has_long else 1.0 if has_current else 0.0
            source = "blended" if has_current and has_long else "current" if has_current else "long_term"
            confidence = blend_weight * current_conf + (1 - blend_weight) * long_conf
            values = {}
            for name in fields:
                values[name] = ((blend_weight * group["current"][name].mean if has_current else 0)
                                + ((1 - blend_weight) * group["long_term"][name].mean if has_long else 0))
            duration, past_slope = values["duration"], values["slope"]
            past_context = _context_from_values(values)
            state = {"duration": duration, "slope": past_slope}
            if past_context is not None:
                state["context"] = past_context
            match = match_state(state, elapsed, slope, context)
            if match is None:
                continue
            match_weight, ratio = match
            if slope <= 0.1:
                confidence *= match_weight
                if confidence < MIN_PREDICTION_CONFIDENCE:
                    continue
            peak_time = values["peak_minutes"]
            if not 0 <= peak_time <= 180:
                continue
            points = (((0.0, 0.0),) if peak_time == 0 else
                      tuple((peak_time * i / RESPONSE_GRID, values[str(i)] * ratio)
                            for i in range(RESPONSE_GRID + 1)))
            weight = confidence * match_weight
            matches.append((points, weight, confidence, source, blend_weight))
        if not matches:
            return None
        times = sorted({t for points, *_ in matches for t, _ in points})
        total = sum(row[1] for row in matches)
        points = tuple((t, sum(interpolate(p, t) * w for p, w, *_ in matches) / total) for t in times)
        rise = max(value for _, value in points)
        peak_time = next(t for t, value in reversed(points) if abs(value - rise) < 1e-9)
        confidence = sum(w * conf for _, w, conf, *_ in matches) / total
        sources = {row[3] for row in matches}
        self.last_source = sources.pop() if len(sources) == 1 else "blended"
        self.last_weight = sum(row[1] * row[4] for row in matches) / total
        return OffPrediction(rise, peak_time, confidence, points)

    def dump(self) -> dict:
        return {"current": {str(i): v.dump() for i, v in self.current.items()},
                "long_term": {str(i): v.dump() for i, v in self.long_term.items()},
                "responses": {key: {layer: {name: bucket.dump() for name, bucket in values.items()}
                                     for layer, values in group.items()} for key, group in self.responses.items()}}

    @classmethod
    def restore(cls, data) -> "CurveMemory":
        if not isinstance(data, dict) or set(data) != {"current", "long_term", "responses"}:
            raise ValueError("Invalid curve memory")
        memory = cls()
        for layer in ("current", "long_term"):
            rows = data[layer]
            if not isinstance(rows, dict) or len(rows) > 300:
                raise ValueError("Invalid curve memory coverage")
            for index, bucket in rows.items():
                if not isinstance(index, str) or not index.isdigit() or not 0 <= int(index) < 300:
                    raise ValueError("Invalid curve memory index")
                restored_bucket = Bucket.restore(bucket)
                if abs(restored_bucket.mean) > 3:
                    raise ValueError("Invalid temperature delta memory")
                getattr(memory, layer)[int(index)] = restored_bucket
        groups = data["responses"]
        if not isinstance(groups, dict) or len(groups) > MAX_RESPONSE_GROUPS:
            raise ValueError("Invalid OFF memory groups")
        required = {"duration", "slope", "peak_minutes", *(str(i) for i in range(RESPONSE_GRID + 1))}
        for key, group in groups.items():
            if (not isinstance(key, str) or not isinstance(group, dict)
                    or set(group) != {"current", "long_term"}):
                raise ValueError("Invalid OFF memory group")
            restored = {}
            for layer, rows in group.items():
                if not isinstance(rows, dict) or (rows and set(rows) not in (required, required | CONTEXT_FIELDS)):
                    raise ValueError("Incomplete OFF memory curve")
                restored[layer] = {name: Bucket.restore(bucket) for name, bucket in rows.items()}
                if rows:
                    values = restored[layer]
                    if (not 10 <= values["duration"].mean <= 1440
                            or not -10 <= values["slope"].mean <= 10
                            or not 0 <= values["peak_minutes"].mean <= 180
                            or abs(values["0"].mean) > 0.001
                            or any(not -3 <= values[str(i)].mean <= 10 for i in range(RESPONSE_GRID + 1))
                            or any(values[str(i)].mean > values[str(RESPONSE_GRID)].mean + 0.001
                                   for i in range(RESPONSE_GRID))
                            or (values["peak_minutes"].mean == 0
                                and any(abs(values[str(i)].mean) > 0.001 for i in range(RESPONSE_GRID + 1)))):
                        raise ValueError("Invalid OFF memory response")
                    means = {name: value.mean for name, value in values.items()}
                    context = _context_from_values(means)
                    if CONTEXT_FIELDS.issubset(rows) and (
                            not valid_context(context) or any(means[f"ctx_{name}_known"] not in (0, 1)
                                                             for name in ("pre_slope", "off_minutes"))):
                        raise ValueError("Invalid OFF memory context")
            if restored["current"] and restored["long_term"] and set(restored["current"]) != set(restored["long_term"]):
                raise ValueError("Inconsistent OFF memory fields")
            memory.responses[key] = restored
        return memory

    def diagnostics(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        def confidence(rows, long_term=False):
            prefix = []
            for index in range(max(rows, default=-1) + 1):
                bucket = rows.get(index)
                if bucket is None:
                    break
                prefix.append(bucket.confidence(now, long_term=long_term))
            return min(prefix) if len(prefix) >= 2 else 0.0
        def off_confidence(layer):
            values = []
            for group in self.responses.values():
                rows = group[layer]
                if not rows:
                    continue
                values.append(min(bucket.confidence(
                    now, long_term=layer == "long_term",
                    scale=_response_scale(name),
                ) for name, bucket in rows.items()))
            return max(values, default=0.0)
        long_stats = [*self.long_term.values(), *(bucket for group in self.responses.values()
                                                 for bucket in group["long_term"].values())]
        return {"current_confidence": confidence(self.current),
                "long_term_confidence": confidence(self.long_term, True),
                "off_current_confidence": off_confidence("current"),
                "off_long_term_confidence": off_confidence("long_term"),
                "current_bucket_count": len(self.current), "long_term_bucket_count": len(self.long_term),
                "long_term_promotions": max((v.promotions for v in long_stats), default=0),
                "response_groups": len(self.responses),
                "long_term_response_groups": sum(bool(group["long_term"]) for group in self.responses.values()),
                "last_long_term_update": max((v.updated_at for v in long_stats), default=None),
                "prediction_source": self.last_source,
                "current_weight": self.last_weight}
