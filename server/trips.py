"""Derive trips from stored position fixes and telemetry samples.

Polestar has no trip history API, so trips are reconstructed:

1. Consecutive fixes within STAY_RADIUS_M of each other collapse into a cluster.
2. A cluster is a *stop* when the car demonstrably sat there: it spans at least
   STAY_MIN_S, contains a "parked" fix, or is followed by a gap of STOP_GAP_S
   with no new fixes (the car only reports position while awake/driving).
3. A trip is the movement from one stop to the next. Distance comes from the
   odometer samples when available (GPS route length otherwise), battery use
   from the state-of-charge samples.

Everything is computed on request, so the rules can be tuned without losing data.
"""

from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass, field
from itertools import compress
from typing import Any

STAY_RADIUS_M = 150
STAY_MIN_S = 5 * 60
STOP_GAP_S = 10 * 60
MIN_TRIP_KM = 0.3
SOC_AFTER_ARRIVAL_S = 30 * 60
# After parking, the car keeps reporting for a few minutes (e.g. the final
# odometer value ~1 min later, a range recalculation ~7 min later). Changes in
# this window belong to the trip that just ended, not the next departure.
SETTLE_S = 10 * 60
# While driving the car reports SoC/range/odometer every few minutes. A longer
# quiet spell means it was parked, so a departure is the start of the unbroken
# run of driving activity that leads up to arrival.
QUIET_GAP_S = 30 * 60
# Telemetry columns compute_trips() reads
SAMPLE_COLUMNS = ("odometer_m", "soc", "range_km")


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class Cluster:
    points: list[dict[str, Any]] = field(default_factory=list)

    @property
    def first(self) -> dict[str, Any]:
        return self.points[0]

    @property
    def last(self) -> dict[str, Any]:
        return self.points[-1]

    @property
    def span(self) -> int:
        return self.last["ts"] - self.first["ts"]

    @property
    def parked(self) -> bool:
        return any(p["kind"] == "parked" for p in self.points)


def _clusters(positions: list[dict[str, Any]]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for p in positions:
        if clusters:
            anchor = clusters[-1].first
            if haversine_m(anchor["lat"], anchor["lon"], p["lat"], p["lon"]) <= STAY_RADIUS_M:
                clusters[-1].points.append(p)
                continue
        clusters.append(Cluster([p]))
    return clusters


class _Series:
    """Time-indexed lookups over one sample column (nulls dropped)."""

    def __init__(self, samples: dict[str, list[Any]], column: str) -> None:
        values = samples[column]
        present = [v is not None for v in values]
        self.ts = list(compress(samples["ts"], present))
        self.values = list(compress(values, present))

    def at_or_before(self, ts: int) -> float | None:
        i = bisect.bisect_right(self.ts, ts) - 1
        return self.values[i] if i >= 0 else None

    def first_after(self, ts: int, within: int) -> float | None:
        i = bisect.bisect_left(self.ts, ts)
        if i < len(self.ts) and self.ts[i] - ts <= within:
            return self.values[i]
        return None



def _activity_times(samples: dict[str, list[Any]]) -> list[int]:
    """Timestamps of telemetry changes that indicate driving.

    Only the directions driving produces count: odometer up, SoC down, range
    down. Charging (SoC/range up) and wake-up range recalculations upwards are
    ignored. On the Polestar 2 position fixes only arrive at parking, so this
    is what dates the departure.
    """
    moved = bytearray(len(samples["ts"]))
    for col, sign in (("odometer_m", 1), ("soc", -1), ("range_km", -1)):
        prev = None
        for i, v in enumerate(samples[col]):
            if v is None:
                continue
            if prev is not None and (v - prev) * sign > 0:
                moved[i] = 1
            prev = v
    return list(compress(samples["ts"], moved))


def compute_trips(
    positions: list[dict[str, Any]],
    samples: dict[str, list[Any]],
    now: float | None = None,
) -> list[dict[str, Any]]:
    """`samples` is columnar ({"ts": [...], column: [...]}, see
    History.sample_columns) with at least SAMPLE_COLUMNS."""
    now = now or time.time()
    if len(positions) < 2:
        return []

    clusters = _clusters(positions)
    n = len(clusters)

    def is_stop(i: int) -> bool:
        c = clusters[i]
        if i == 0 or c.parked or c.span >= STAY_MIN_S:
            return True
        next_ts = clusters[i + 1].first["ts"] if i + 1 < n else now
        return next_ts - c.last["ts"] >= STOP_GAP_S

    stops = [i for i in range(n) if is_stop(i)]
    odo = _Series(samples, "odometer_m")
    soc = _Series(samples, "soc")
    activity = _activity_times(samples)

    trips: list[dict[str, Any]] = []
    for a, b in zip(stops, stops[1:]):
        trips.append(_build_trip(clusters, a, b, odo, soc, activity, ongoing=False))

    # Movement after the last stop that hasn't settled yet = trip in progress.
    if stops and stops[-1] < n - 1:
        trips.append(_build_trip(clusters, stops[-1], n - 1, odo, soc, activity, ongoing=True))

    trips = [t for t in trips if t is not None]

    # Odometer at the end of a trip is best read just before the next departure,
    # since the car may report the final odometer value a little after parking.
    for t, nxt in zip(trips, trips[1:] + [None]):
        until = nxt["start_ts"] if nxt and nxt["start_ts"] else int(now)
        end_odo = odo.at_or_before(until)
        if t["_odo_start"] is not None and end_odo is not None and not t["ongoing"]:
            km = (end_odo - t["_odo_start"]) / 1000
            if 0 < km < 2000:
                t["distance_km"] = round(km, 1)
                t["distance_source"] = "odometer"

    out = []
    for t in trips:
        t.pop("_odo_start", None)
        if t["distance_km"] < MIN_TRIP_KM and not t["ongoing"]:
            continue
        if t["duration_s"] and t["distance_km"]:
            t["avg_speed_kmh"] = round(t["distance_km"] / (t["duration_s"] / 3600), 0)
        out.append(t)
    return out


def _build_trip(clusters, a: int, b: int, odo: _Series, soc: _Series,
                activity: list[int], ongoing: bool):
    start_c, end_c = clusters[a], clusters[b]
    moving = [p for c in clusters[a + 1 : b] for p in c.points]
    origin = start_c.last
    dest = end_c.first if not ongoing else end_c.last
    arrive = dest["ts"]

    # Departure: the first telemetry activity after the car settled at `origin`
    # (or the first moving fix, if the car reports positions while driving).
    limit = moving[0]["ts"] if moving else arrive
    depart = None
    for floor in (origin["ts"] + SETTLE_S, origin["ts"]):  # short stop: drop the settle window
        lo = bisect.bisect_right(activity, floor)
        hi = bisect.bisect_right(activity, limit)
        chain = activity[lo:hi]
        if chain:
            # Walk back from the last activity before arrival while gaps stay short
            j = len(chain) - 1
            while j > 0 and chain[j] - chain[j - 1] < QUIET_GAP_S:
                j -= 1
            depart = chain[j]
            break
    if moving:
        depart = min(depart, moving[0]["ts"]) if depart else moving[0]["ts"]

    route = [[origin["lat"], origin["lon"]]] + [[p["lat"], p["lon"]] for p in moving]
    route.append([dest["lat"], dest["lon"]])
    gps_m = sum(haversine_m(*route[i], *route[i + 1]) for i in range(len(route) - 1))

    # Reading just before departure (the departure sample may already show the first drop)
    soc_start = soc.at_or_before(depart - 1 if depart else origin["ts"])
    soc_end = soc.first_after(arrive, SOC_AFTER_ARRIVAL_S) if not ongoing else soc.at_or_before(arrive)
    if soc_end is None:
        soc_end = soc.at_or_before(arrive)

    return {
        "id": f"{origin['ts']}-{arrive}",
        "start_ts": depart,
        "end_ts": None if ongoing else arrive,
        "last_ts": arrive,
        "duration_s": (arrive - depart) if depart and not ongoing else None,
        "distance_km": round(gps_m / 1000, 1),
        "distance_source": "gps",
        "soc_start": soc_start,
        "soc_end": soc_end,
        "soc_used": round(soc_start - soc_end, 1) if soc_start is not None and soc_end is not None else None,
        "avg_speed_kmh": None,
        "start": {"lat": origin["lat"], "lon": origin["lon"]},
        "end": {"lat": dest["lat"], "lon": dest["lon"]},
        "route": route,
        "points": len(route),
        "ongoing": ongoing,
        "_odo_start": odo.at_or_before(depart - 1 if depart else origin["ts"]),
    }
