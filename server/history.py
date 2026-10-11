"""Local SQLite history store.

Home Assistant's recorder is what turns the integration's sensors into history
graphs. Without HA we keep our own: one row per successful poll per car.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COLUMNS = (
    "soc",
    "range_km",
    "odometer_m",
    "charging_power_w",
    "charging_status",
    "connection_status",
    "target_soc",
    "consumption_kwh_100km",
)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS samples (
    vin TEXT NOT NULL,
    ts  INTEGER NOT NULL,
    {", ".join(f"{c} {'TEXT' if c.endswith('_status') else 'REAL'}" for c in COLUMNS)},
    PRIMARY KEY (vin, ts)
);

-- One row per distinct position fix. ts is the fix's own timestamp from the
-- car, so re-polling an unchanged position is a no-op.
CREATE TABLE IF NOT EXISTS positions (
    vin      TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    kind     TEXT NOT NULL,
    altitude REAL,
    heading  REAL,
    speed    REAL,
    PRIMARY KEY (vin, ts)
);
"""


class History:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Bumped whenever stored data changes in a way that can alter derived
        # results (trips); lets callers cache work between polls.
        self._versions: dict[str, int] = {}
        self._last_values: dict[str, list[Any]] = {}
        with self._lock:
            # WAL: readers (route cache connection, ad-hoc read-only tools) never
            # block on the poller's writes, and NORMAL avoids an fsync per poll.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def _bump(self, vin: str) -> None:
        self._versions[vin] = self._versions.get(vin, 0) + 1

    def data_version(self, vin: str) -> int:
        return self._versions.get(vin, 0)

    def record(self, vin: str, row: dict[str, Any], ts: datetime | None = None) -> None:
        epoch = int((ts or datetime.now(tz=timezone.utc)).timestamp())
        values = [row.get(c) for c in COLUMNS]
        # A poll that repeats the previous values adds a row but changes nothing derived
        if values != self._last_values.get(vin):
            self._last_values[vin] = values
            self._bump(vin)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO samples (vin, ts, {', '.join(COLUMNS)}) "
                f"VALUES (?, ?, {', '.join('?' for _ in COLUMNS)})",
                [vin, epoch, *values],
            )
            self._conn.commit()

    def record_many(self, vin: str, rows: list[tuple[datetime, dict[str, Any]]]) -> None:
        with self._lock:
            self._conn.executemany(
                f"INSERT OR REPLACE INTO samples (vin, ts, {', '.join(COLUMNS)}) "
                f"VALUES (?, ?, {', '.join('?' for _ in COLUMNS)})",
                [[vin, int(ts.timestamp()), *[r.get(c) for c in COLUMNS]] for ts, r in rows],
            )
            self._conn.commit()
        self._last_values.pop(vin, None)
        self._bump(vin)

    def has_data(self, vin: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM samples WHERE vin = ? LIMIT 1", [vin])
            return cur.fetchone() is not None

    def query(self, vin: str, since: datetime, max_points: int = 600) -> list[dict[str, Any]]:
        """Return samples since `since`, thinned to roughly `max_points` rows."""
        since_epoch = int(since.timestamp())
        with self._lock:
            (count,) = self._conn.execute(
                "SELECT COUNT(*) FROM samples WHERE vin = ? AND ts >= ?", [vin, since_epoch]
            ).fetchone()
            step = max(1, count // max_points)
            rows = self._conn.execute(
                f"SELECT ts, {', '.join(COLUMNS)} FROM ("
                f"  SELECT *, ROW_NUMBER() OVER (ORDER BY ts) AS rn FROM samples"
                f"  WHERE vin = ? AND ts >= ?"
                f") WHERE (rn - 1) % ? = 0 OR rn = ? ORDER BY ts",
                [vin, since_epoch, step, count],
            ).fetchall()
        return [dict(r) for r in rows]

    def daily_distance(self, vin: str, since: datetime) -> list[dict[str, Any]]:
        """Kilometres driven per local day.

        Each day is measured from the previous day's last odometer reading, so
        overnight gaps between polls are not lost.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, "
                "       MIN(odometer_m) AS lo, MAX(odometer_m) AS hi "
                "FROM samples WHERE vin = ? AND ts >= ? AND odometer_m IS NOT NULL "
                "GROUP BY day ORDER BY day",
                [vin, int(since.timestamp())],
            ).fetchall()
        result = []
        prev_hi = None
        for r in rows:
            start = prev_hi if prev_hi is not None else r["lo"]
            result.append({"day": r["day"], "km": round(max(0.0, r["hi"] - start) / 1000.0, 1)})
            prev_hi = r["hi"]
        return result

    def record_fixes(self, vin: str, fixes: list[Any]) -> int:
        """Store location fixes (server.location.Fix); returns how many were new."""
        rows = [
            [vin, int(f.ts.timestamp()), f.lat, f.lon, f.kind, f.altitude, f.heading, f.speed]
            for f in fixes
            if f is not None
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._conn.total_changes
            self._conn.executemany(
                "INSERT OR IGNORE INTO positions "
                "(vin, ts, lat, lon, kind, altitude, heading, speed) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._conn.commit()
            added = self._conn.total_changes - before
        if added:
            self._bump(vin)
        return added

    def positions(self, vin: str, since: datetime) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, lat, lon, kind, altitude, heading, speed FROM positions "
                "WHERE vin = ? AND ts >= ? ORDER BY ts",
                [vin, int(since.timestamp())],
            ).fetchall()
        return [dict(r) for r in rows]

    def samples(self, vin: str, since: datetime, columns: tuple[str, ...]) -> list[dict[str, Any]]:
        """Unthinned samples for the given columns (used for trip odometer/SoC lookups)."""
        cols = [c for c in columns if c in COLUMNS]
        with self._lock:
            rows = self._conn.execute(
                f"SELECT ts, {', '.join(cols)} FROM samples WHERE vin = ? AND ts >= ? ORDER BY ts",
                [vin, int(since.timestamp())],
            ).fetchall()
        return [dict(r) for r in rows]

    def sample_columns(
        self, vin: str, since: datetime, columns: tuple[str, ...]
    ) -> dict[str, list[Any]]:
        """Unthinned samples as columns: {"ts": [...], column: [...]}.

        Columnar tuples skip building a dict per row, which dominates the cost
        once history spans months of one-minute polls.
        """
        cols = [c for c in columns if c in COLUMNS]
        with self._lock:
            cur = self._conn.execute(
                f"SELECT ts, {', '.join(cols)} FROM samples WHERE vin = ? AND ts >= ? ORDER BY ts",
                [vin, int(since.timestamp())],
            )
            cur.row_factory = None
            rows = cur.fetchall()
        names = ["ts", *cols]
        if not rows:
            return {n: [] for n in names}
        return {n: list(col) for n, col in zip(names, zip(*rows))}

    def last_position_ts(self, vin: str) -> int | None:
        with self._lock:
            (ts,) = self._conn.execute(
                "SELECT MAX(ts) FROM positions WHERE vin = ?", [vin]
            ).fetchone()
        return ts

    def has_positions(self, vin: str) -> bool:
        with self._lock:
            cur = self._conn.execute("SELECT 1 FROM positions WHERE vin = ? LIMIT 1", [vin])
            return cur.fetchone() is not None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
