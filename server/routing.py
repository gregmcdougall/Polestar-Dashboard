"""Infer the road route of a trip from its start, end and odometer distance.

The Polestar 2 only reports its position when it parks, so a trip is known by
two points plus the distance driven. We ask an OSRM routing server for up to
three alternative driving routes between the points and keep the one whose
length is closest to the odometer distance.

Only the two coordinates (rounded to ~10 m) are sent. OSRM's alternatives are
cached in SQLite per start/end pair, so a repeated journey (a commute) is only
ever requested once, and the best match is chosen locally per trip. Default server: the public OSRM demo at
router.project-osrm.org (fair use: max 1 request/second). Point OSRM_URL at a
self-hosted instance to keep everything local.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx

_LOGGER = logging.getLogger(__name__)

DEFAULT_OSRM_URL = "https://router.project-osrm.org"
REQUEST_INTERVAL_S = 1.1  # public server allows 1 req/s
USER_AGENT = "polestar-dashboard/0.1 (personal use; github.com/pypolestar based)"
# A route is "matched" when its length is within this fraction of the odometer distance
MATCH_TOLERANCE = 0.15
# Start and end this close together = round trip; routing can't infer that
ROUND_TRIP_M = 300
# Parsed alternatives kept in memory (a commute is looked up on every request)
MEMORY_CACHE_PAIRS = 256
# SQLite's default limit on bound parameters is 999
_LOOKUP_CHUNK = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS route_alternatives (
    pair_key     TEXT PRIMARY KEY,   -- "lat,lon>lat,lon" rounded to 4 dp
    status       TEXT NOT NULL,      -- ok | none
    alternatives TEXT,               -- JSON [{"distance": m, "latlngs": [[lat, lon], ...]}, ...]
    created      INTEGER NOT NULL
);
"""


def pair_key(trip: dict[str, Any]) -> str:
    s, e = trip["start"], trip["end"]
    return f"{s['lat']:.4f},{s['lon']:.4f}>{e['lat']:.4f},{e['lon']:.4f}"


def needs_inference(trip: dict[str, Any]) -> bool:
    if trip.get("ongoing") or trip.get("points", 0) > 2:
        return False
    from .trips import haversine_m

    s, e = trip["start"], trip["end"]
    return haversine_m(s["lat"], s["lon"], e["lat"], e["lon"]) >= ROUND_TRIP_M


def choose_route(routes: list[dict[str, Any]], target_m: float | None) -> dict[str, Any] | None:
    """Pick the alternative whose length best matches the odometer distance."""
    if not routes:
        return None
    if target_m is None:
        return routes[0]  # OSRM orders by duration; fastest is the best guess
    return min(routes, key=lambda r: abs(r["distance"] - target_m))


class RouteInferrer:
    def __init__(self, db_path, base_url: str | None = None) -> None:
        self.base_url = (base_url or DEFAULT_OSRM_URL).rstrip("/")
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
        # pair_key -> parsed alternatives (None = no route), most recently used last
        self._memory: OrderedDict[str, list[dict[str, Any]] | None] = OrderedDict()
        # Bumped whenever a lookup result is stored; annotate() output depends on it
        self.version = 0
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        self._queued: set[str] = set()
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT})
        self._task = asyncio.create_task(self._worker(), name="route-inference")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------ cache

    def _remember(self, key: str, alternatives: list[dict[str, Any]] | None) -> None:
        self._memory[key] = alternatives
        self._memory.move_to_end(key)
        while len(self._memory) > MEMORY_CACHE_PAIRS:
            self._memory.popitem(last=False)

    def _lookup(self, keys: set[str]) -> dict[str, list[dict[str, Any]] | None]:
        """Cached alternatives for `keys` (None = no route); unknown keys are absent."""
        found: dict[str, list[dict[str, Any]] | None] = {}
        missing = []
        for key in keys:
            if key in self._memory:
                self._memory.move_to_end(key)
                found[key] = self._memory[key]
            else:
                missing.append(key)
        for i in range(0, len(missing), _LOOKUP_CHUNK):
            chunk = missing[i : i + _LOOKUP_CHUNK]
            with self._lock:
                rows = self._conn.execute(
                    "SELECT pair_key, status, alternatives FROM route_alternatives "
                    f"WHERE pair_key IN ({', '.join('?' for _ in chunk)})",
                    chunk,
                ).fetchall()
            for row in rows:
                alts = json.loads(row["alternatives"]) if row["alternatives"] else None
                self._remember(row["pair_key"], alts)
                found[row["pair_key"]] = alts
        return found

    def _store(self, key: str, status: str, alternatives: list[dict] | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO route_alternatives VALUES (?, ?, ?, ?)",
                [key, status, json.dumps(alternatives) if alternatives else None, int(time.time())],
            )
            self._conn.commit()
        self._remember(key, alternatives if status == "ok" else None)
        self.version += 1

    # ------------------------------------------------------------ public

    def annotate(self, vin: str, trips: list[dict[str, Any]]) -> None:
        """Attach cached inferred routes to trips; queue the missing ones.

        Must run on the event loop thread (it feeds an asyncio.Queue).
        """
        keyed = []
        for t in trips:
            t["route_source"] = "gps" if t.get("points", 0) > 2 else "endpoints"
            if needs_inference(t):
                keyed.append((pair_key(t), t))
        cached = self._lookup({key for key, _ in keyed})
        for key, t in keyed:
            if key not in cached:
                t["route_source"] = "pending"
                if key not in self._queued:
                    self._queued.add(key)
                    self._queue.put_nowait((key, t))
            elif cached[key]:
                target_m = t["distance_km"] * 1000 if t.get("distance_source") == "odometer" else None
                best = choose_route(cached[key], target_m)
                t["route"] = best["latlngs"]
                t["route_source"] = "inferred"
                t["route_distance_km"] = round(best["distance"] / 1000, 1)
                if target_m:
                    ratio = best["distance"] / target_m
                    t["route_match"] = round(ratio, 2)
                    t["route_approx"] = abs(ratio - 1) > MATCH_TOLERANCE

    # ------------------------------------------------------------ worker

    async def _worker(self) -> None:
        while True:
            key, trip = await self._queue.get()
            try:
                await self._fetch(key, trip)
            except Exception:  # noqa: BLE001 - never let one trip kill the worker
                _LOGGER.exception("Route inference failed")
            finally:
                self._queued.discard(key)
            await asyncio.sleep(REQUEST_INTERVAL_S)

    async def _fetch(self, key: str, trip: dict[str, Any]) -> None:
        s, e = trip["start"], trip["end"]
        url = (
            f"{self.base_url}/route/v1/driving/"
            f"{s['lon']:.4f},{s['lat']:.4f};{e['lon']:.4f},{e['lat']:.4f}"
        )
        params = {"alternatives": "3", "overview": "full", "geometries": "geojson", "steps": "false"}
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            _LOGGER.warning("Routing request failed: %s", exc)
            return  # not cached; retried next time trips are requested
        if resp.status_code == 429 or resp.status_code >= 500:
            _LOGGER.warning("Routing server busy (HTTP %s); will retry later", resp.status_code)
            await asyncio.sleep(10)
            return
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if data.get("code") != "Ok":
            _LOGGER.info("No route found (%s)", data.get("code") or resp.status_code)
            self._store(key, "none", None)
            return

        routes = [
            {
                "distance": r["distance"],
                "latlngs": [[round(lat, 5), round(lon, 5)] for lon, lat in r["geometry"]["coordinates"]],
            }
            for r in data.get("routes", [])
        ]
        self._store(key, "ok", routes)
        _LOGGER.debug("Route alternatives: %s km", [round(r["distance"] / 1000, 1) for r in routes])
