"""Turn pypolestar model objects into JSON-friendly dicts for the web UI.

The field names deliberately match pypolestar's attribute names (and therefore the
`data_state_attribute` values used by the Home Assistant integration's sensors), so
anything documented for the HA sensors maps 1:1 onto this payload.
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime
from enum import Enum
from typing import Any

# Derived @property values on CarBatteryData that HA exposes as sensors but that
# dataclasses.asdict() does not include.
BATTERY_DERIVED = ("estimated_full_charge_range_km", "estimated_fully_charged")


def to_jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if not f.name.startswith("_")
        }
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    return value


def model_to_dict(obj: Any, derived: tuple[str, ...] = ()) -> dict[str, Any] | None:
    if obj is None:
        return None
    data = to_jsonable(obj)
    for name in derived:
        data[name] = to_jsonable(getattr(obj, name, None))
    return data


def history_row(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Flatten the fields we chart over time from a snapshot."""
    battery = snapshot.get("battery") or {}
    grpc = snapshot.get("grpc_battery") or {}
    odometer = snapshot.get("odometer") or {}
    target = snapshot.get("grpc_target_soc") or {}

    def first(*values):
        for v in values:
            if v is not None:
                return v
        return None

    return {
        "soc": first(battery.get("battery_charge_level_percentage"),
                     grpc.get("battery_charge_level_percentage")),
        "range_km": first(battery.get("estimated_distance_to_empty_km"),
                          grpc.get("estimated_distance_to_empty_km")),
        "odometer_m": odometer.get("odometer_meters"),
        "charging_power_w": grpc.get("charging_power_watts"),
        "charging_status": first(grpc.get("charging_status"), battery.get("charging_status")),
        "connection_status": grpc.get("charger_connection_status"),
        "target_soc": target.get("battery_charge_target_level"),
        "consumption_kwh_100km": grpc.get("average_energy_consumption_kwh_per_100km"),
    }
