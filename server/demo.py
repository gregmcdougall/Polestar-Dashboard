"""Offline demo source: a simulated Polestar that commutes, runs errands and charges.

Drives follow a daily schedule along fixed routes in Gothenburg. Like a real
Polestar 2, the car reports its position only when it parks; the route in
between is inferred (see routing.py).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .location import Fix, summarize
from .trips import haversine_m

DEMO_IMAGE_BASE = (
    "https://car-images.polestar.com/359/2024/summary-transparent/"
    "EA/72300/001190/R80000/_/19/_/XPLUSS/_/1/_/default/{angle}.png"
)
DEMO_OPAQUE_BASE = DEMO_IMAGE_BASE.replace("summary-transparent", "summary").replace(".png", ".jpg")

PLACES = {
    "home": (57.65790, 11.91370),
    "work": (57.70730, 11.93810),
    "store": (57.68850, 11.99050),
    "lake": (57.74030, 12.10980),
}

# Waypoints between places (reversed for the return leg).
ROUTES = {
    ("home", "work"): [
        (57.65790, 11.91370), (57.66320, 11.91050), (57.67080, 11.91540), (57.67810, 11.91320),
        (57.68560, 11.91100), (57.69280, 11.91860), (57.69850, 11.92820), (57.70310, 11.93290),
        (57.70730, 11.93810),
    ],
    ("home", "store"): [
        (57.65790, 11.91370), (57.66120, 11.92900), (57.66650, 11.94350), (57.67300, 11.95800),
        (57.67920, 11.97250), (57.68440, 11.98300), (57.68850, 11.99050),
    ],
    ("home", "lake"): [
        (57.65790, 11.91370), (57.66650, 11.94350), (57.67920, 11.97250), (57.69270, 12.00410),
        (57.70450, 12.03050), (57.71480, 12.05380), (57.72590, 12.07720), (57.73420, 12.09560),
        (57.74030, 12.10980),
    ],
}


def _route(a: str, b: str) -> list[tuple[float, float]]:
    if (a, b) in ROUTES:
        return ROUTES[(a, b)]
    return list(reversed(ROUTES[(b, a)]))


def _local(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, time(hh, mm)).astimezone(timezone.utc)


class DemoCar:
    capacity_kwh = 78.0
    full_range_km = 480.0

    def __init__(self, vin: str, model: str, reg: str, seed: int) -> None:
        self.vin, self.model, self.reg = vin, model, reg
        self.seed = seed
        self.rng = random.Random(seed)
        self.soc = 62.0
        self.odometer_m = 18_420_000.0
        self.target = 80
        self.place = "home"
        self.connected = False
        self.charging = False
        self.power_w = 0
        self.t: datetime | None = None
        # active drive
        self.leg: list[tuple[float, float]] | None = None
        self.leg_to: str | None = None
        self.leg_len_m = 0.0
        self.leg_pos_m = 0.0
        self.leg_speed_kmh = 0.0
        self.done_departures: set[datetime] = set()
        self.known: Fix | None = None
        self.parked: Fix | None = None

    # -------------------------------------------------- schedule

    def _plan(self, d: date) -> list[tuple[datetime, str, str]]:
        r = random.Random(f"{self.seed}-{d.isoformat()}")
        jitter = lambda: r.randint(-12, 12)  # noqa: E731
        wd = d.weekday()
        plan = []
        if wd < 5:
            plan += [(_local(d, 7, 45) + timedelta(minutes=jitter()), "home", "work"),
                     (_local(d, 17, 10) + timedelta(minutes=jitter()), "work", "home")]
            if wd in (1, 3):
                plan += [(_local(d, 18, 40) + timedelta(minutes=jitter()), "home", "store"),
                         (_local(d, 19, 25) + timedelta(minutes=jitter()), "store", "home")]
        elif wd == 5:
            plan += [(_local(d, 10, 30) + timedelta(minutes=jitter()), "home", "store"),
                     (_local(d, 11, 40) + timedelta(minutes=jitter()), "store", "home")]
        else:
            plan += [(_local(d, 13, 15) + timedelta(minutes=jitter()), "home", "lake"),
                     (_local(d, 16, 30) + timedelta(minutes=jitter()), "lake", "home")]
        return plan

    def _position(self) -> tuple[float, float]:
        if not self.leg:
            return PLACES[self.place]
        remaining = self.leg_pos_m
        for a, b in zip(self.leg, self.leg[1:]):
            seg = haversine_m(*a, *b)
            if remaining <= seg:
                f = remaining / seg if seg else 0
                return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
            remaining -= seg
        return self.leg[-1]

    # -------------------------------------------------- simulation

    def step(self, t: datetime) -> None:
        if self.t is None:
            self.t = t
            lat, lon = PLACES[self.place]
            self.parked = self.known = Fix(ts=t, lat=lat, lon=lon, kind="parked")
            return
        dt_h = max(0.0, (t - self.t).total_seconds() / 3600)
        prev = self.t
        self.t = t

        # Depart?
        if self.leg is None:
            for day in {prev.astimezone().date(), t.astimezone().date()}:
                for when, src, dst in self._plan(day):
                    if src == self.place and prev < when <= t and when not in self.done_departures:
                        self.done_departures.add(when)
                        self.leg = _route(src, dst)
                        self.leg_to = dst
                        self.leg_len_m = sum(haversine_m(*a, *b) for a, b in zip(self.leg, self.leg[1:]))
                        self.leg_pos_m = 0.0
                        self.leg_speed_kmh = self.rng.uniform(30, 48)
                        self.connected = self.charging = False
                        self.power_w = 0
                        dt_h = (t - when).total_seconds() / 3600
                        break

        # Drive
        if self.leg is not None:
            km = min(self.leg_speed_kmh * dt_h, (self.leg_len_m - self.leg_pos_m) / 1000)
            self.leg_pos_m += km * 1000
            self.odometer_m += km * 1000 * 1.08  # roads are longer than our waypoints
            self.soc = max(5.0, self.soc - km * 1.08 / self.full_range_km * 100 * self.rng.uniform(0.95, 1.15))
            lat, lon = self._position()
            if self.leg_pos_m >= self.leg_len_m - 1:
                self.place = self.leg_to
                self.leg = None
                self.parked = Fix(ts=t, lat=lat, lon=lon, kind="parked")
                self.known = Fix(ts=t, lat=lat, lon=lon, kind="known")
            # Like the Polestar 2, no position updates while driving: the last
            # known position stays at the origin until the car parks.
            return

        # Charge at home overnight
        hour = t.astimezone().hour
        if self.place == "home" and (hour >= 18 or hour < 7) and self.soc < self.target - 5:
            self.connected = True
        if self.connected and self.soc < self.target:
            self.charging = True
            taper = 1.0 if self.soc < 70 else max(0.35, 1 - (self.soc - 70) / 40)
            self.power_w = int(11_000 * taper * self.rng.uniform(0.96, 1.02))
            self.soc = min(self.target, self.soc + self.power_w / 1000 * dt_h / self.capacity_kwh * 100)
        else:
            self.charging = False
            self.power_w = 0

    def fixes(self) -> list[Fix]:
        return [f for f in (self.known, self.parked) if f]

    def snapshot(self) -> dict[str, Any]:
        now = self.t or datetime.now(tz=timezone.utc)
        soc = round(self.soc)
        range_km = round(self.soc / 100 * self.full_range_km * 0.93)
        to_full = (
            round((self.target - self.soc) / 100 * self.capacity_kwh / max(self.power_w, 1) * 1000 * 60)
            if self.charging
            else None
        )
        charging_status = "Charging" if self.charging else ("Done" if self.connected else "Idle")
        fully_charged = (
            (now.replace(second=0, microsecond=0) + timedelta(minutes=to_full)).isoformat()
            if self.charging and to_full
            else None
        )
        return {
            "vin": self.vin,
            "info": {
                "vin": self.vin,
                "internal_vehicle_identifier": "00000000-demo-0000-0000-000000000000",
                "registration_no": self.reg,
                "model_name": self.model,
                "model_year": "2025",
            },
            "images": {
                "transparent": [{"url": DEMO_IMAGE_BASE.format(angle=a), "angle": a} for a in range(6)],
                "opaque": [{"url": DEMO_OPAQUE_BASE.format(angle=a), "angle": a} for a in range(6)],
            },
            "odometer": {
                "odometer_meters": int(self.odometer_m),
                "average_speed_km_per_hour": None,
                "trip_meter_automatic_km": None,
                "trip_meter_manual_km": None,
                "event_updated_timestamp": now.isoformat(),
            },
            "battery": {
                "battery_charge_level_percentage": soc,
                "estimated_distance_to_empty_km": range_km,
                "estimated_charging_time_to_full_minutes": to_full or 0,
                "charging_status": "Charging" if self.charging else "Idle",
                "estimated_full_charge_range_km": round(range_km / max(soc, 1) * 100, 2),
                "estimated_fully_charged": fully_charged,
                "event_updated_timestamp": now.isoformat(),
                "average_energy_consumption_kwh_per_100km": None,
                "charger_connection_status": None,
                "charging_current_amps": None,
                "charging_power_watts": None,
                "estimated_charging_time_minutes_to_target_distance": None,
            },
            "health": {
                "brake_fluid_level_warning": "No Warning",
                "days_to_service": 180 - now.timetuple().tm_yday % 180,
                "distance_to_service_km": 21_340,
                "engine_coolant_level_warning": "No Warning",
                "oil_level_warning": "No Warning",
                "service_warning": "No Warning",
                "event_updated_timestamp": now.isoformat(),
            },
            "grpc_battery": {
                "charger_connection_status": "Connected" if self.connected else "Disconnected",
                "charging_status": charging_status,
                "battery_charge_level_percentage": round(self.soc, 1),
                "estimated_distance_to_empty_km": range_km,
                "estimated_charging_time_to_full_minutes": to_full or 0,
                "charging_power_watts": self.power_w,
                "charging_current_amps": round(self.power_w / 690) if self.charging else 0,
                "charging_voltage_volts": 230 if self.charging else 0,
                "charging_type": "AC" if self.connected else "None",
                "average_energy_consumption_kwh_per_100km": round(17.8 + self.rng.uniform(-0.3, 0.3), 1),
                "estimated_charging_time_minutes_to_target_distance": 0,
                "estimated_charging_time_minutes_to_minimum_soc": 0,
                "timestamp": now.isoformat(),
            },
            "grpc_target_soc": {
                "battery_charge_target_level": self.target,
                "charge_target_level_setting_type": "Daily",
                "pending_battery_charge_target_level": None,
                "pending_charge_target_level_setting_type": "Unspecified",
            },
            "location": summarize(self.known, self.parked, now=now),
            "_fixes": self.fixes(),
            "api": {
                "api_connected": True,
                "api_token_expires_at": (now + timedelta(minutes=45)).isoformat(),
                "api_status_code_data": 200,
                "api_status_code_auth": 200,
            },
        }


class DemoSource:
    """Offline stand-in for the live API so the UI can be developed without an account."""

    def __init__(self) -> None:
        self.cars = {
            "LPSVSEDEMO0000000": DemoCar("LPSVSEDEMO0000000", "Polestar 2", "DEMO 2", seed=2),
        }
        self.vins = list(self.cars)

    async def start(self) -> None:
        pass

    def backfill(self, vin: str, days: int = 14, every: timedelta = timedelta(minutes=2)):
        """Simulate `days` of history ending now; yields (ts, snapshot) pairs."""
        car = self.cars[vin]
        end = datetime.now(tz=timezone.utc)
        t = end - timedelta(days=days)
        while t < end:
            car.step(t)
            yield t, car.snapshot()
            t += every

    async def fetch(self, vin: str) -> dict[str, Any]:
        car = self.cars[vin]
        car.step(datetime.now(tz=timezone.utc))
        return car.snapshot()

    def location_stream(self, vin: str) -> AsyncIterator[Fix] | None:
        return None

    async def close(self) -> None:
        pass
