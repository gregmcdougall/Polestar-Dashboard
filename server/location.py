"""Vehicle location over Polestar's gRPC backend.

pypolestar doesn't expose location, but it already opens the gRPC channel to the
"C3" backend (cnepmob.volvocars.com discovery) that the Polestar 2 is served
from, and holds a valid access token. We reuse both and call the location
service directly with hand-encoded protobuf.

Service paths and message layouts follow the reverse-engineering in
https://github.com/kildahldev/unofficial-polestar-api (services/location.py,
backend.py). They are undocumented and may change.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

_LOGGER = logging.getLogger(__name__)

C3_LOCATION_SVC = "/dtlinternet.DtlInternetService"
GRPC_TIMEOUT = 30


@dataclass(frozen=True)
class Fix:
    ts: datetime
    lat: float
    lon: float
    kind: str  # "known" | "parked" | "stream"
    altitude: float | None = None
    heading: float | None = None
    speed: float | None = None

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["ts"] = self.ts.isoformat()
        return d


# --------------------------------------------------------------- protobuf wire


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = data[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return result, pos
        shift += 7


def decode(data: bytes) -> dict[int, Any]:
    """Decode one protobuf message into {field_number: value} (last value wins)."""
    out: dict[int, Any] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value = struct.unpack_from("<d", data, pos)[0]
            pos += 8
        elif wire == 2:
            length, pos = _varint(data, pos)
            value = data[pos : pos + length]
            pos += length
        elif wire == 5:
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out[field] = value
    return out


def encode_vin_request(vin: str) -> bytes:
    raw = vin.encode()
    return bytes([0x0A, len(raw)]) + raw  # field 1, length-delimited


# --------------------------------------------------------------- parsing


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bytes):
        value = decode(value).get(1)
    if isinstance(value, int) and value > 0:
        # Some payloads use milliseconds
        if value > 10**11:
            value //= 1000
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _valid(lat: Any, lon: Any) -> bool:
    return (
        isinstance(lat, float)
        and isinstance(lon, float)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (lat == 0.0 and lon == 0.0)
    )


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def parse_location(data: bytes, kind: str) -> Fix | None:
    """Parse the location payload shapes seen across backends.

    * compact: {1: lon(double), 2: lat(double), 3: timestamp}
      nested in field 5 or field 2 of the response
    * flat:    {2: lon, 3: lat, 4: timestamp}
    * full Location: {1: Timestamp, 2: Coordinate{1: lon, 2: lat},
                      3: altitude, 4: heading, 5: speed}, possibly nested in
      a LocationResponse {1: status, 2: Location}
    """
    try:
        raw = decode(data)
    except (ValueError, IndexError, struct.error):
        _LOGGER.debug("Undecodable location payload (%d bytes)", len(data))
        return None

    def try_message(msg: dict[int, Any]) -> Fix | None:
        # compact
        if _valid(msg.get(2), msg.get(1)):
            ts = _timestamp(msg.get(3))
            if ts:
                return Fix(ts=ts, lat=msg[2], lon=msg[1], kind=kind)
        # full Location with nested Coordinate
        if isinstance(msg.get(2), bytes):
            try:
                coord = decode(msg[2])
            except (ValueError, IndexError, struct.error):
                coord = {}
            if _valid(coord.get(2), coord.get(1)):
                ts = _timestamp(msg.get(1))
                if ts:
                    return Fix(
                        ts=ts, lat=coord[2], lon=coord[1], kind=kind,
                        altitude=_num(msg.get(3)), heading=_num(msg.get(4)), speed=_num(msg.get(5)),
                    )
        # flat
        if _valid(msg.get(3), msg.get(2)):
            ts = _timestamp(msg.get(4))
            if ts:
                return Fix(ts=ts, lat=msg[3], lon=msg[2], kind=kind)
        return None

    # Nested message fields first (5, then 2, then 1), then the top level.
    candidates = []
    for field in (5, 2, 1):
        if isinstance(raw.get(field), bytes):
            try:
                candidates.append(decode(raw[field]))
            except (ValueError, IndexError, struct.error):
                continue
    candidates.append(raw)

    for msg in candidates:
        if fix := try_message(msg):
            return fix
    _LOGGER.debug("No coordinates found in location payload: %r", raw)
    return None


# --------------------------------------------------------------- client


class LocationClient:
    """Fetches last-known / last-parked positions over pypolestar's C3 channel."""

    UNSUPPORTED_CODES = {"PERMISSION_DENIED", "UNIMPLEMENTED", "NOT_FOUND"}
    # The backend routinely drops long-lived subscriptions (GOAWAY / RST_STREAM,
    # surfacing as INTERNAL or UNAVAILABLE); that's a normal end, not a failure.
    STREAM_CLOSED_CODES = {"INTERNAL", "UNAVAILABLE", "DEADLINE_EXCEEDED", "CANCELLED"}

    def __init__(self, api: Any) -> None:
        self.api = api
        self.unsupported: dict[str, str] = {}  # vin -> reason (polling)
        self.stream_unsupported: dict[str, str] = {}  # vin -> reason (live stream only)

    def _channel(self):
        grpc_client = getattr(self.api, "grpc_client", None)
        return grpc_client.c3_channel if grpc_client else None

    def _metadata(self, vin: str) -> list[tuple[str, str]]:
        return [("authorization", f"Bearer {self.api.auth.access_token}"), ("vin", vin)]

    def is_supported(self, vin: str) -> bool:
        return vin not in self.unsupported and self._channel() is not None

    def _handle_error(self, vin: str, what: str, exc: Exception) -> None:
        code = getattr(exc, "code", lambda: None)()
        name = getattr(code, "name", str(code))
        if name in self.UNSUPPORTED_CODES:
            self.unsupported[vin] = f"{what}: {name}"
            _LOGGER.info("Location not available for this vehicle (%s), not retrying", name)
        else:
            _LOGGER.warning("gRPC %s failed: %s", what, exc)

    async def _unary(self, vin: str, method: str, kind: str) -> Fix | None:
        channel = self._channel()
        if channel is None or vin in self.unsupported:
            return None
        call = channel.unary_unary(
            f"{C3_LOCATION_SVC}/{method}",
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )
        try:
            data = await call(encode_vin_request(vin), metadata=self._metadata(vin), timeout=GRPC_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - grpc.aio.AioRpcError and transport errors
            self._handle_error(vin, method, exc)
            return None
        return parse_location(data, kind)

    async def last_known(self, vin: str) -> Fix | None:
        return await self._unary(vin, "GetLastKnownLocation", "known")

    async def last_parked(self, vin: str) -> Fix | None:
        return await self._unary(vin, "GetLastParkedLocation", "parked")

    def is_stream_supported(self, vin: str) -> bool:
        return self.is_supported(vin) and vin not in self.stream_unsupported

    async def stream(self, vin: str) -> AsyncIterator[Fix]:
        """Live position updates. Ends when the server closes the stream."""
        channel = self._channel()
        if channel is None or not self.is_stream_supported(vin):
            return
        call = channel.unary_stream(
            f"{C3_LOCATION_SVC}/StreamLastKnownLocations",
            request_serializer=lambda b: b,
            response_deserializer=lambda b: b,
        )(encode_vin_request(vin), metadata=self._metadata(vin))
        try:
            async for data in call:
                if fix := parse_location(data, "stream"):
                    yield fix
        except Exception as exc:  # noqa: BLE001
            code = getattr(exc, "code", lambda: None)()
            name = getattr(code, "name", str(code))
            if name in self.STREAM_CLOSED_CODES:
                _LOGGER.debug("Location stream closed by server (%s)", name)
            elif name in self.UNSUPPORTED_CODES:
                # Only the stream is refused; polling may still work, so don't touch self.unsupported
                self.stream_unsupported[vin] = name
                _LOGGER.info("Live location stream not available for this vehicle (%s); using polling", name)
            else:
                _LOGGER.warning("Location stream failed: %s", exc)
        finally:
            call.cancel()


# --------------------------------------------------------------- summary

DRIVING_RECENT_S = 15 * 60


def summarize(known: Fix | None, parked: Fix | None, *, available: bool = True,
              reason: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The `location` block of a car snapshot."""
    now = now or datetime.now(tz=timezone.utc)
    current = max((f for f in (known, parked) if f), key=lambda f: f.ts, default=None)
    state = None
    if current is not None:
        driving = (
            known is not None
            and (parked is None or (known.ts - parked.ts).total_seconds() > 60)
            and (now - known.ts).total_seconds() < DRIVING_RECENT_S
        )
        state = "driving" if driving else "parked"
    return {
        "available": available,
        "reason": reason,
        "state": state,
        "current": current.to_json() if current else None,
        "known": known.to_json() if known else None,
        "parked": parked.to_json() if parked else None,
    }
