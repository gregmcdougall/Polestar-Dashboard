"""Data sources: the live Polestar API (via pypolestar) and an offline demo.

`LiveSource.fetch()` follows the same sequence as the Home Assistant integration's
`PolestarCoordinator._async_update_data()`:

  * telematics (odometer / battery / health) every poll, via GraphQL
  * car information + images refreshed at most hourly
  * gRPC battery (charger connection, power, current, voltage) and target SoC,
    best-effort; pypolestar returns None / marks VINs unsupported rather than raising

On top of that (not in the HA integration) it fetches last-known and
last-parked positions over the same gRPC channel; see location.py.
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
from typing import Any, Protocol

from .location import Fix, LocationClient, summarize
from .snapshot import BATTERY_DERIVED, model_to_dict

_LOGGER = logging.getLogger(__name__)

CAR_INFORMATION_UPDATE_INTERVAL = timedelta(hours=1)


class AuthFailed(Exception):
    """Credentials were rejected; polling should stop until they are fixed."""


class SourceError(Exception):
    """A poll failed but may succeed on retry."""


class Source(Protocol):
    vins: list[str]

    async def start(self) -> None: ...
    async def fetch(self, vin: str) -> dict[str, Any]: ...
    def location_stream(self, vin: str) -> AsyncIterator[Fix] | None: ...
    async def close(self) -> None: ...


class LiveSource:
    def __init__(self, username: str, password: str, vins: list[str] | None = None) -> None:
        from pypolestar import PolestarApi

        self.api = PolestarApi(
            username=username,
            password=password,
            vins=[v.upper() for v in vins] if vins else None,
            unique_id="dashboard",
        )
        self.vins: list[str] = []
        self._info: dict[str, tuple[datetime, dict | None, dict | None]] = {}
        self.location = LocationClient(self.api)

    async def start(self) -> None:
        from pypolestar.exceptions import PolestarApiException, PolestarAuthFailedException

        try:
            await self.api.async_init()
        except PolestarAuthFailedException as exc:
            raise AuthFailed(str(exc)) from exc
        except PolestarApiException as exc:
            # pypolestar wraps most login problems in PolestarAuthException
            raise SourceError(f"Login failed: {exc}") from exc
        self.vins = sorted(self.api.get_available_vins())
        if not self.vins:
            raise SourceError("No cars found on this Polestar account")

    async def fetch(self, vin: str) -> dict[str, Any]:
        from pypolestar.exceptions import PolestarApiException, PolestarAuthFailedException

        api = self.api
        try:
            await api.update_latest_data(vin=vin)
        except PolestarAuthFailedException as exc:
            raise AuthFailed(str(exc)) from exc
        except PolestarApiException as exc:
            raise SourceError(str(exc) or type(exc).__name__) from exc

        now = datetime.now(tz=timezone.utc)
        cached = self._info.get(vin)
        if cached is None or now - cached[0] > CAR_INFORMATION_UPDATE_INTERVAL:
            info = model_to_dict(api.get_car_information(vin))
            images = model_to_dict(api.get_car_images(vin))
            self._info[vin] = cached = (now, info, images)
        _, info, images = cached

        telematics = api.get_car_telematics(vin)
        grpc_battery = (
            api.get_grpc_battery(vin) if api.is_grpc_battery_supported(vin) else None
        )
        grpc_target = (
            api.get_grpc_target_soc(vin) if api.is_grpc_target_soc_supported(vin) else None
        )

        # Fetch both: on some Polestar 2 software versions last-known goes stale
        # while last-parked keeps updating, so the newer of the two wins.
        known = await self.location.last_known(vin)
        parked = await self.location.last_parked(vin)
        location = summarize(
            known,
            parked,
            available=self.location.is_supported(vin) or bool(known or parked),
            reason=self.location.unsupported.get(vin),
        )

        token_expiry = api.auth.token_expiry
        code_data = api.get_status_code()
        code_auth = api.auth.get_status_code()

        return {
            "vin": vin,
            "info": info,
            "images": images,
            "odometer": model_to_dict(telematics.odometer if telematics else None),
            "battery": model_to_dict(
                telematics.battery if telematics else None, BATTERY_DERIVED
            ),
            "health": model_to_dict(telematics.health if telematics else None),
            "grpc_battery": model_to_dict(grpc_battery),
            "grpc_target_soc": model_to_dict(grpc_target),
            "location": location,
            "_fixes": [f for f in (known, parked) if f],
            "api": {
                "api_connected": code_data == 200
                and code_auth == 200
                and api.auth.is_token_valid(),
                "api_token_expires_at": token_expiry.isoformat() if token_expiry else None,
                "api_status_code_data": code_data,
                "api_status_code_auth": code_auth,
            },
        }

    def location_stream(self, vin: str) -> AsyncIterator[Fix] | None:
        if not self.location.is_stream_supported(vin):
            return None
        return self.location.stream(vin)

    async def close(self) -> None:
        try:
            await self.api.async_logout()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            _LOGGER.debug("Logout failed", exc_info=True)


