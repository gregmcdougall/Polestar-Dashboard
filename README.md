# Polestar Dashboard

A standalone web dashboard for Polestar cars. It needs **no Home Assistant**. It uses the same
access method as the [polestar_api](https://github.com/pypolestar/polestar_api) Home Assistant
integration: the [`pypolestar`](https://github.com/pypolestar/pypolestar) library, pinned to the
same version (1.12.7).

> Unofficial. Not affiliated with Polestar.

## Why there's a small server

The browser can't talk to Polestar directly:

- **Login** is an OIDC/PKCE flow against `polestarid.eu.polestar.com`. It posts the username and
  password to a PingFederate form, follows cross-origin redirects, and reads the auth code from
  a `302 Location` header. CORS and cookie rules block all of that in a browser.
- **Charging and location data** (plug status, power, current, voltage, charge limit, position)
  comes over native **gRPC**, which browsers can't call. The servers are Volvo's C3 backend,
  found via `cnepmob.volvocars.com`, and Polestar's `api.pccs-prod.plstr.io`.

So a thin local Python server (`server/`) does the API work and serves the static HTML/JS
dashboard (`web/`) plus a small JSON API. By default it binds to `127.0.0.1` only.

## Quick start

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

Try it with simulated data (no account needed), then open <http://127.0.0.1:8777>:

```bash
uv run python -m server --demo --port 8777
```

The demo uses its own port so it can run alongside the real dashboard.

Use it with your car:

```bash
cp .env.example .env
```

Edit `.env` with your Polestar ID email and password, then start the server:

```bash
uv run python -m server
```

Open <http://127.0.0.1:8766>.

Options: `--host` (default `127.0.0.1`), `--port` (default `8766`), `--demo`, `--debug`.

Settings in `.env`:

| Setting | Meaning |
|---|---|
| `POLESTAR_USERNAME`, `POLESTAR_PASSWORD` | Your Polestar ID login (required) |
| `POLESTAR_VIN` | Optional: limit to these cars (comma-separated) |
| `POLESTAR_SCAN_INTERVAL` | Seconds between polls (default 60, minimum 30) |
| `OSRM_URL` | Routing server for inferred trip routes (default: the public OSRM demo) |
| `DASHBOARD_ALLOWED_HOSTS` | Extra host names the server answers to, comma-separated, e.g. a Tailscale name. Only needed with `--host` set to a non-loopback address (see below) |

The server binds to `127.0.0.1` and only accepts requests addressed to `127.0.0.1` or
`localhost`, which protects against DNS-rebinding attacks. To reach it from another device, start
it with `--host` and list the names you'll use in `DASHBOARD_ALLOWED_HOSTS`. A specific bind
address is allowed automatically; `0.0.0.0` is not. The dashboard has no login and shows your
car's location history, so only expose it through a VPN or an authenticating proxy. The
startup service always binds to `127.0.0.1`.

## Run at startup (macOS)

`scripts/service.sh` installs the server as a LaunchAgent in `~/Library/LaunchAgents`, so it
starts when you log in and restarts if it crashes. With FileVault on, unlocking the Mac after
a restart logs you in, so in practice it starts at boot. No `sudo` is needed.

Install and start it (stop any server you started by hand first):

```bash
scripts/service.sh install
```

Restart it after changing server code (changes under `web/` only need a page reload):

```bash
scripts/service.sh restart
```

Check it's running:

```bash
scripts/service.sh status
```

Follow the log (`~/Library/Logs/polestar-dashboard.log`):

```bash
scripts/service.sh logs
```

Remove it:

```bash
scripts/service.sh uninstall
```

It serves on port 8766, the same default as running by hand, so only one of the two can run at
a time. Set `DASHBOARD_PORT` when running `install` to use a different port.
The service stores the project path, so re-run `install` if you move the folder.

## What it shows

The same data the HA integration's sensors show. A few of those sensors, such as trip meters
and average speed, are always empty with the current API, so they're left out. Distances are shown in miles by default; the
**mi | km** switch in the top bar changes this (remembered per browser). Clicking a tile or chart
heading collapses it.

| Area | Data | Source |
|---|---|---|
| Car | model, model year, registration, VIN, images from 6 angles (drag to rotate) | GraphQL `getConsumerCarsV2`, `getCarImages` |
| Battery | charge %, est. range, est. full-charge range | GraphQL `carTelematicsV2.battery` |
| Charging | status, plugged in, AC/DC, power, current, voltage, time to full, ready-at | gRPC `BatteryService/GetLatestBattery` |
| Charge limit | target SoC and mode (Daily / Long trip / Custom) | gRPC `TargetSocService/GetTargetSoc` |
| Odometer | total distance | GraphQL `carTelematicsV2.odometer` |
| Health | days and distance to service, service, brake fluid, coolant and oil warnings | GraphQL `carTelematicsV2.health` |
| Diagnostics | API connected, status codes, token expiry, data timestamps | pypolestar client state |
| **Location** | last known + last parked position, live stream | gRPC `dtlinternet.DtlInternetService` (C3), *not part of pypolestar* |

**History.** HA's recorder normally turns sensors into graphs. Here, every poll is stored in a local
SQLite file (`data/history.sqlite3`), and four charts read from it:

- battery level (with the charge limit)
- estimated range
- charging power
- distance per day

Each chart has a 24 h / 7 d / 30 d range, a hover tooltip and a table view.

**Trips.** Polestar has no trip-history API, so trips are reconstructed from the stored
position fixes (`positions` table). A trip runs from one stop to the next:

- A stop is somewhere the car stayed within 150 m for 5+ minutes, reported as parked, or went
  quiet for 10+ minutes.
- The departure time is the start of the run of driving signals (odometer up, battery % or
  range down) leading up to arrival. Readings in the first 10 minutes after parking are ignored,
  because the car sends its final odometer value a little late.
- Distance comes from the odometer, or from the recorded positions when no odometer data is
  available (shown as "≈").
- Battery use comes from state-of-charge samples.

Trips are computed on request (`server/trips.py`), so the rules can be tuned without losing
data. They're shown on an OpenStreetMap map (Leaflet) next to a list grouped by day, for the
last 7, 30 or 90 days.

Location uses the same undocumented gRPC backend the Polestar app uses, reverse-engineered by
[unofficial-polestar-api](https://github.com/kildahldev/unofficial-polestar-api). It's called
over pypolestar's existing C3 connection and token, so there's no second login. It's
best-effort: if the backend refuses, the feature switches itself off and diagnostics shows why.
Both last-known and last-parked positions are fetched, because on some Polestar 2 software
versions last-known goes stale.

**Inferred routes.** The Polestar 2 only reports its position when it parks, so a trip is known
by its start, its end and the odometer distance. The route is inferred from those:

1. Ask an [OSRM](https://project-osrm.org/) routing server for up to 3 alternative driving routes
   between the two points.
2. Keep the alternative whose length is closest to the odometer distance. For example, a 17.5 km
   trip picks the 17.4 km alternative over the 9.1 km fastest route.

In the list, a trip is marked "approx. route" when no alternative is within 15%, and "start &
end only" (a dotted line) when no route can be inferred, such as for round trips.

- **Privacy:** only the two coordinates are sent, rounded to about 10 m; no times or car data.
- **Default server:** the public OSRM demo, `router.project-osrm.org`, rate-limited to 1
  request a second.
- **Caching:** alternatives are cached per start/end pair (`route_alternatives` table), so a
  regular commute is only requested once.
- **Self-hosting:** set `OSRM_URL` in `.env` to use your own OSRM server and keep everything
  local.

Trips are only recorded while the server is running.

Some fields are empty for some models. That's a limitation of Polestar's API, not this app.
For example, pypolestar notes that some Polestar 2s aren't set up for the charge-limit service.
When a service refuses a car, the app stops asking and the field shows "—".

## Layout

```
scripts/
  service.sh    install/manage the macOS LaunchAgent
server/
  __main__.py   CLI entry point, loads .env
  app.py        Starlette app: /api/status, /api/cars, /api/history, /api/daily, /api/trips,
                POST /api/refresh; host checking, security headers, trip caching
  poller.py     background loop (HA DataUpdateCoordinator equivalent), backoff, auth-failure stop
  sources.py    LiveSource (pypolestar) mirrors HA coordinator.py, plus location
  location.py   gRPC location client (raw protobuf) + snapshot summary
  trips.py      trip reconstruction from positions + odometer/SoC/range samples
  routing.py    route inference via OSRM (alternatives matched to odometer distance)
  demo.py       DemoSource: simulated Polestar 2 commuting around Gothenburg
  snapshot.py   pypolestar dataclasses -> JSON (field names match HA sensor attributes)
  history.py    SQLite history store
web/
  index.html, styles.css, app.js   dashboard (no build step; only Leaflet is external)
  units.js, collapse.js            mi/km switching, collapsible headings
  charts.js                        small SVG line/bar charts with tooltips + table view
  trips.js                         trip map (Leaflet, from cdnjs) + trip list
```

## Notes

- `backoff` is listed explicitly because pypolestar 1.12.7 imports it without declaring it.
- Credentials stay in `.env` (git-ignored). Your password is only sent to Polestar ID. The
  access token it returns is used with Polestar's API and with Volvo's C3 backend, which serves
  Polestar 2 charging and location data.
- `scripts/service.sh install` makes `.env` and the log file readable only by you (`chmod 600`).
- Security: the server only answers to its allowed host names (see `DASHBOARD_ALLOWED_HOSTS`),
  rejects cross-site requests to `POST /api/refresh`, and limits refreshes to one every 15
  seconds. It also sends a strict Content-Security-Policy and other security headers. Leaflet is
  loaded with a Subresource Integrity hash, so the page refuses it if the copy on cdnjs changes.
- `data/` (history databases) is git-ignored. Demo mode rebuilds `data/demo.sqlite3` on each start.
- Map tiles are loaded from `tile.openstreetmap.org`, which sees the map areas you view (not your
  trip data). Leaflet loads from cdnjs.
- Trip start/end coordinates go to the OSRM server in `OSRM_URL` (public demo by default) to
  infer routes.
