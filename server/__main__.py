"""Entry point: `uv run python -m server [--demo]`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # The history DB is a log of where the car has been; keep it (and its WAL
    # files) readable by this user only.
    os.umask(0o077)
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Standalone Polestar dashboard")
    parser.add_argument("--demo", action="store_true", help="Run with simulated data (no account needed)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8766, help="Port (default: 8766)")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.debug:
        # pypolestar/gql/httpx are chatty at INFO; keep our own logs readable
        for name in ("httpx", "gql", "pypolestar"):
            logging.getLogger(name).setLevel(logging.WARNING)

    username = os.getenv("POLESTAR_USERNAME", "").strip()
    password = os.getenv("POLESTAR_PASSWORD", "")
    vins = [v.strip() for v in os.getenv("POLESTAR_VIN", "").split(",") if v.strip()] or None
    interval = int(os.getenv("POLESTAR_SCAN_INTERVAL", "60") or 60)
    osrm_url = os.getenv("OSRM_URL", "").strip() or None
    # Host names the dashboard may be reached by, beyond 127.0.0.1/localhost
    # (e.g. a Tailscale name when binding --host to a non-loopback address)
    extra_hosts = [h.strip() for h in os.getenv("DASHBOARD_ALLOWED_HOSTS", "").split(",") if h.strip()]
    loopback = args.host in ("127.0.0.1", "localhost", "::1")
    allowed_hosts = ["127.0.0.1", "localhost", *extra_hosts]
    if not loopback and args.host not in ("0.0.0.0", "::"):
        allowed_hosts.append(args.host)
    if not loopback:
        logging.getLogger(__name__).warning(
            "Binding to %s: the dashboard has no login, so anyone who can reach this address "
            "can see the car's location history. Prefer 127.0.0.1 behind an authenticating "
            "proxy or VPN.", args.host,
        )

    if not args.demo and not (username and password):
        sys.exit(
            "No credentials found. Copy .env.example to .env and set POLESTAR_USERNAME / "
            "POLESTAR_PASSWORD, or start with --demo to use simulated data."
        )

    from .app import create_app

    data_dir = ROOT / "data"
    db_path = data_dir / ("demo.sqlite3" if args.demo else "history.sqlite3")
    if args.demo:
        # The simulated car's state lives in memory, so start its history afresh too
        db_path.unlink(missing_ok=True)
    app = create_app(
        demo=args.demo,
        db_path=db_path,
        interval=min(interval, 15) if args.demo else max(interval, 30),
        username=username,
        password=password,
        vins=vins,
        osrm_url=osrm_url,
        allowed_hosts=allowed_hosts,
    )

    print(f"\n  Polestar dashboard → http://{args.host}:{args.port}\n", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
