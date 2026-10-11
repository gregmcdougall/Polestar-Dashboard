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
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser(description="Standalone Polestar dashboard")
    parser.add_argument("--demo", action="store_true", help="Run with simulated data (no account needed)")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
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
    )

    print(f"\n  Polestar dashboard → http://{args.host}:{args.port}\n", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
