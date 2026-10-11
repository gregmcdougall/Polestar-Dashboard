"""Background polling loop (stand-in for Home Assistant's DataUpdateCoordinator)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from .history import History
from .snapshot import history_row
from .sources import AuthFailed, Source, SourceError

_LOGGER = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 15 * 60
STREAM_RETRY_SECONDS = (30, 60, 120, 300)
# Give up on the live stream (polling continues) if it never delivers a single
# position in this many consecutive connections.
STREAM_MAX_EMPTY = 6


class Poller:
    def __init__(self, source: Source, history: History, interval: int = 60) -> None:
        self.source = source
        self.history = history
        self.interval = interval
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.status: dict[str, Any] = {"state": "starting", "message": None, "last_poll": None}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._streams: dict[str, asyncio.Task] = {}
        self.stream_status: dict[str, dict[str, Any]] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="polestar-poller")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, *self._streams.values()) if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        await self.source.close()

    def refresh_now(self) -> None:
        self._wake.set()

    def _set_status(self, state: str, message: str | None = None) -> None:
        self.status = {
            "state": state,
            "message": message,
            "last_poll": datetime.now(tz=timezone.utc).isoformat(),
        }

    async def _run(self) -> None:
        failures = 0

        # Log in (retrying with backoff unless the credentials are simply wrong).
        while True:
            try:
                await self.source.start()
                break
            except AuthFailed as exc:
                _LOGGER.error("Polestar login rejected: %s", exc)
                self._set_status("auth_failed", "Polestar rejected the username/password.")
                return
            except Exception as exc:  # noqa: BLE001
                failures += 1
                delay = min(MAX_BACKOFF_SECONDS, self.interval * 2**failures)
                _LOGGER.warning("Login failed (%s); retrying in %ss", exc, delay)
                self._set_status("error", f"Login failed: {exc}. Retrying in {delay}s.")
                await self._sleep(delay)

        _LOGGER.info("Polling %s car(s): %s", len(self.source.vins), ", ".join(self.source.vins))
        failures = 0

        while True:
            errors = []
            for vin in self.source.vins:
                try:
                    snap = await self.source.fetch(vin)
                except AuthFailed as exc:
                    _LOGGER.error("Authentication failed for VIN %s: %s", vin, exc)
                    self._set_status("auth_failed", "Polestar rejected the stored credentials.")
                    return
                except (SourceError, Exception) as exc:  # noqa: BLE001
                    _LOGGER.warning("Update failed for VIN %s: %s", vin, exc)
                    errors.append(f"{vin[-4:]}: {exc}")
                    if vin in self.snapshots:
                        self.snapshots[vin]["api"] = {
                            **(self.snapshots[vin].get("api") or {}),
                            "api_connected": False,
                        }
                    continue

                now = datetime.now(tz=timezone.utc)
                fixes = snap.pop("_fixes", [])
                snap["fetched_at"] = now.isoformat()
                # Keep a fresher streamed position if we have one
                prev_loc = (self.snapshots.get(vin) or {}).get("location") or {}
                if _newer(prev_loc.get("current"), (snap.get("location") or {}).get("current")):
                    snap["location"]["current"] = prev_loc["current"]
                if snap.get("location") is not None:
                    snap["location"]["stream"] = self.stream_status.get(vin)
                self.snapshots[vin] = snap
                self.history.record(vin, history_row(snap), now)
                if fixes:
                    self.history.record_fixes(vin, fixes)
                self._ensure_stream(vin)

            if errors:
                failures += 1
                self._set_status("error", "; ".join(errors))
                delay = min(MAX_BACKOFF_SECONDS, self.interval * 2 ** min(failures, 4))
            else:
                failures = 0
                self._set_status("ok")
                delay = self.interval

            await self._sleep(delay)

    def _set_stream(self, vin: str, **changes: Any) -> None:
        st = self.stream_status.setdefault(vin, {"state": None, "fixes": 0, "last_fix": None})
        st.update(changes)
        loc = (self.snapshots.get(vin) or {}).get("location")
        if loc is not None:
            loc["stream"] = st

    def _ensure_stream(self, vin: str) -> None:
        if (self.stream_status.get(vin) or {}).get("state") == "off":
            return
        task = self._streams.get(vin)
        if task is None or task.done():
            if self.source.location_stream(vin) is None:
                return
            self._streams[vin] = asyncio.create_task(self._stream_location(vin), name=f"location-{vin[-4:]}")

    async def _stream_location(self, vin: str) -> None:
        """Consume the live location stream; reconnect with backoff when it closes."""
        attempt = 0
        while True:
            stream = self.source.location_stream(vin)
            if stream is None:
                self._set_stream(vin, state="off", note="Not available for this car")
                return
            self._set_stream(vin, state="connected")
            received = 0
            async for fix in stream:
                received += 1
                total = (self.stream_status[vin]["fixes"] or 0) + 1
                self._set_stream(vin, fixes=total, last_fix=fix.ts.isoformat())
                if self.history.record_fixes(vin, [fix]):
                    loc = (self.snapshots.get(vin) or {}).get("location")
                    if loc is not None and _newer(fix.to_json(), loc.get("current")):
                        loc["current"] = fix.to_json()
                        loc["known"] = fix.to_json()

            attempt = 0 if received else attempt + 1
            if attempt >= STREAM_MAX_EMPTY and not self.stream_status[vin]["fixes"]:
                _LOGGER.info(
                    "Live location stream for %s never delivered a position after %d tries; "
                    "using polling only", vin[-4:], attempt,
                )
                self._set_stream(vin, state="off", note="No data from stream; using polling")
                return
            delay = STREAM_RETRY_SECONDS[min(attempt, len(STREAM_RETRY_SECONDS) - 1)]
            self._set_stream(vin, state="reconnecting")
            _LOGGER.debug("Location stream for %s closed after %d fixes; reconnecting in %ss",
                          vin[-4:], received, delay)
            await asyncio.sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except TimeoutError:
            pass


def _newer(a: dict | None, b: dict | None) -> bool:
    """True if fix dict `a` is strictly newer than `b`."""
    if not a:
        return False
    if not b:
        return True
    return a["ts"] > b["ts"]
