"""HTTP layer: a small JSON API plus the static dashboard in ../web."""

from __future__ import annotations

import contextlib
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .history import History
from .poller import Poller
from .routing import RouteInferrer
from .demo import DemoSource
from .snapshot import history_row
from .sources import LiveSource
from .trips import compute_trips

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# A manual refresh polls Polestar immediately; don't let a script hammer it
REFRESH_MIN_INTERVAL_S = 15

LOOPBACK_HOSTS = ["127.0.0.1", "localhost"]

# Leaflet (cdnjs) and OSM tiles are the only third parties; car images come
# from Polestar's CDN, whose host isn't fixed, hence https: for images.
CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://cdnjs.cloudflare.com",
        "style-src 'self' https://cdnjs.cloudflare.com 'unsafe-inline'",
        "img-src 'self' data: https:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    # OSM's tile servers require a Referer; this sends only the origin
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], *SECURITY_HEADERS]
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _same_origin(request: Request) -> bool:
    """Reject cross-site requests to state-changing endpoints (CSRF).

    Browsers send Sec-Fetch-Site on every request and Origin on every POST;
    non-browser clients (curl, service.sh) send neither and are allowed.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return True
    return origin.split("://", 1)[-1] == request.headers.get("host")


def create_app(
    *,
    demo: bool,
    db_path: Path,
    interval: int,
    username: str | None = None,
    password: str | None = None,
    vins: list[str] | None = None,
    osrm_url: str | None = None,
    allowed_hosts: list[str] | None = None,
) -> Starlette:
    history = History(db_path)
    routes = RouteInferrer(db_path, osrm_url)

    if demo:
        source = DemoSource()
        for vin in source.vins:
            if not history.has_positions(vin):
                rows, fixes = [], []
                for ts, snap in source.backfill(vin):
                    rows.append((ts, history_row(snap)))
                    fixes.extend(snap.pop("_fixes", []))
                history.record_many(vin, rows)
                history.record_fixes(vin, fixes)
    else:
        source = LiveSource(username or "", password or "", vins)

    poller = Poller(source, history, interval)

    def _vin(request: Request) -> str | None:
        vin = request.query_params.get("vin")
        if vin and vin in poller.snapshots:
            return vin
        return next(iter(poller.snapshots), None)

    def _int_param(request: Request, name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(request.query_params.get(name, default))))
        except ValueError:
            return default

    async def status(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                **poller.status,
                "demo": demo,
                "interval": interval,
                "vins": list(poller.snapshots),
            }
        )

    async def cars(request: Request) -> JSONResponse:
        return JSONResponse(list(poller.snapshots.values()))

    async def history_endpoint(request: Request) -> JSONResponse:
        vin = _vin(request)
        if vin is None:
            return JSONResponse([])
        hours = _int_param(request, "hours", 24, 1, 24 * 365)
        since = datetime.now(tz=timezone.utc) - timedelta(hours=hours)
        return JSONResponse(history.query(vin, since))

    async def daily_endpoint(request: Request) -> JSONResponse:
        vin = _vin(request)
        if vin is None:
            return JSONResponse([])
        days = _int_param(request, "days", 14, 1, 365)
        since = datetime.now(tz=timezone.utc) - timedelta(days=days)
        return JSONResponse(history.daily_distance(vin, since))

    async def trips_endpoint(request: Request) -> JSONResponse:
        vin = _vin(request)
        if vin is None:
            return JSONResponse({"trips": [], "location": None})
        days = _int_param(request, "days", 7, 1, 365)
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(days=days)
        # Look back a little further so the first trip in range has its origin.
        lookback = since - timedelta(days=2)
        trips = compute_trips(
            history.positions(vin, lookback),
            history.samples(vin, lookback, ("odometer_m", "soc", "range_km")),
            now=now.timestamp(),
        )
        trips = [t for t in trips if (t["last_ts"] or 0) >= since.timestamp()]
        routes.annotate(vin, trips)
        trips.reverse()  # newest first
        return JSONResponse({
            "trips": trips,
            "location": (poller.snapshots.get(vin) or {}).get("location"),
        })

    last_refresh = 0.0

    async def refresh(request: Request) -> JSONResponse:
        nonlocal last_refresh
        if not _same_origin(request):
            return JSONResponse({"ok": False, "error": "cross-site request"}, status_code=403)
        now = time.monotonic()
        if now - last_refresh < REFRESH_MIN_INTERVAL_S:
            # Not an error for the UI: a poll is already under way or just happened
            return JSONResponse({"ok": True, "throttled": True})
        last_refresh = now
        poller.refresh_now()
        return JSONResponse({"ok": True})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        poller.start()
        routes.start()
        try:
            yield
        finally:
            await poller.stop()
            await routes.stop()
            history.close()

    return Starlette(
        routes=[
            Route("/api/status", status),
            Route("/api/cars", cars),
            Route("/api/history", history_endpoint),
            Route("/api/daily", daily_endpoint),
            Route("/api/trips", trips_endpoint),
            Route("/api/refresh", refresh, methods=["POST"]),
            Mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web"),
        ],
        middleware=[
            # Rejects requests whose Host isn't ours, which defeats DNS rebinding
            # (a web page re-pointing its own hostname at 127.0.0.1 to read the API)
            Middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or LOOPBACK_HOSTS),
            Middleware(SecurityHeadersMiddleware),
        ],
        lifespan=lifespan,
    )
