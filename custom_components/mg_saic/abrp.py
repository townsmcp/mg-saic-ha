"""A Better Route Planner (ABRP) live-data client for the MG SAIC integration.

This module turns the vehicle data we already fetch from SAIC into the JSON
telemetry payload ABRP expects, and posts it to Iternio's live-data API. The
field mapping mirrors the SAIC MQTT gateway's ABRP integration
(SAIC-iSmart-API/saic-python-mqtt-gateway, src/integrations/abrp/api.py), adapted
to use Home Assistant's shared aiohttp session and to never raise into the
coordinator's update loop.

Two credentials are required (see const.py): an application API key and a
per-vehicle user token. See :class:`AbrpApi`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

from .const import ABRP_ME_URL, ABRP_SEND_URL

LOGGER = logging.getLogger(__name__)

# GPS status codes that indicate a usable fix (values from the SAIC schema's
# GpsStatus enum: 2 = 2D fix, 3 = 3D fix). We compare on the raw/decoded value
# defensively so a schema change can't crash the send.
_GPS_FIX_VALUES = {2, 3, "FIX_2D", "FIX_3d", "FIX_3D"}

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class AbrpError(Exception):
    """Base error for ABRP interactions."""


class AbrpAuthError(AbrpError):
    """The API key / user token were rejected."""


class AbrpConnectionError(AbrpError):
    """We could not reach the ABRP API."""


def _in_range(value: Any, lo: float, hi: float) -> bool:
    """True if value is a number within [lo, hi]. None/non-numeric -> False."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return lo <= value <= hi


def _gps_has_fix(gps_position: Any) -> bool:
    decoded = getattr(gps_position, "gps_status_decoded", None)
    # Enum member -> compare by name and by .value; int -> compare directly.
    name = getattr(decoded, "name", None)
    val = getattr(decoded, "value", decoded)
    return name in _GPS_FIX_VALUES or val in _GPS_FIX_VALUES


class AbrpApi:
    """Minimal async client for ABRP's live-data (telemetry) API.

    :param session: Home Assistant's shared aiohttp client session.
    :param api_key: application API key (default or user override).
    :param user_token: the per-vehicle ABRP user token.
    """

    def __init__(
        self, session: aiohttp.ClientSession, api_key: str, user_token: str
    ) -> None:
        self._session = session
        self._api_key = api_key
        self._user_token = user_token

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"APIKEY {self._api_key}"}

    async def async_validate(self) -> str | None:
        """Validate the credentials against ABRP; return the ABRP vehicle name.

        Raises AbrpAuthError on rejected credentials and AbrpConnectionError on
        network problems, so the options flow can show a precise error.
        """
        try:
            async with self._session.get(
                ABRP_ME_URL,
                # ABRP's /oauth/me expects the API key as a query parameter
                # (?access_token=...&api_key=...); we also send it in the
                # Authorization header for good measure.
                params={
                    "access_token": self._user_token,
                    "api_key": self._api_key,
                },
                headers=self._headers,
                timeout=_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403):
                    raise AbrpAuthError(f"HTTP {resp.status}")
                if resp.status != 200:
                    raise AbrpAuthError(f"HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except AbrpError:
            raise
        except aiohttp.ClientError as err:
            raise AbrpConnectionError(str(err)) from err
        except Exception as err:  # noqa: BLE001 - defensive: never leak raw errors
            raise AbrpConnectionError(str(err)) from err

        if not isinstance(data, dict) or data.get("status") != "ok":
            raise AbrpAuthError(f"Unexpected response: {data!r}")
        return data.get("vehicle_name")

    async def async_send(
        self, vehicle_status: Any, charge_info: Any
    ) -> tuple[bool, str]:
        """Build a telemetry payload from SAIC data and POST it to ABRP.

        Returns (sent, message). Never raises — a failure here must not break
        the coordinator's update cycle.
        """
        try:
            tlm = self._build_payload(vehicle_status, charge_info)
        except Exception as err:  # noqa: BLE001 - defensive
            return False, f"payload build failed: {err}"

        if "soc" not in tlm and "lat" not in tlm:
            # Nothing worth sending (no SoC and no position).
            return False, "no usable telemetry this cycle"

        try:
            async with self._session.post(
                ABRP_SEND_URL,
                params={"token": self._user_token, "tlm": json.dumps(tlm)},
                headers=self._headers,
                timeout=_TIMEOUT,
            ) as resp:
                body = await resp.text()
                if resp.status in (401, 403):
                    raise AbrpAuthError(f"HTTP {resp.status}: {body}")
                if resp.status != 200:
                    return False, f"HTTP {resp.status}: {body}"
                return True, body
        except AbrpError as err:
            raise
        except aiohttp.ClientError as err:
            raise AbrpConnectionError(str(err)) from err

    # --- payload construction ------------------------------------------------

    def _build_payload(self, vehicle_status: Any, charge_info: Any) -> dict[str, Any]:
        charge_mgmt = getattr(charge_info, "chrgMgmtData", None)
        charge_status = getattr(charge_info, "rvsChargeStatus", None)

        data: dict[str, Any] = {"utc": self._timestamp(vehicle_status)}

        # State of charge (bmsPackSOCDsp is stored x10).
        if charge_mgmt is not None:
            soc = getattr(charge_mgmt, "bmsPackSOCDsp", None)
            if _in_range(soc, 0, 1000):
                data["soc"] = soc / 10.0

            # Power / voltage / current — only when the API's validity flag is
            # sane (bmsPackCrntV == 1 marks the current reading as invalid).
            decoded_current = getattr(charge_mgmt, "decoded_current", None)
            crnt_valid = getattr(charge_mgmt, "bmsPackCrntV", None)
            raw_current = getattr(charge_mgmt, "bmsPackCrnt", None)
            if (
                crnt_valid != 1
                and _in_range(raw_current, 0, 65535)
                and isinstance(decoded_current, (int, float))
            ):
                gun_connected = bool(getattr(charge_status, "chargingGunState", False))
                data["is_charging"] = gun_connected and decoded_current < 0.0
                for key, attr in (
                    ("power", "decoded_power"),
                    ("voltage", "decoded_voltage"),
                ):
                    val = getattr(charge_mgmt, attr, None)
                    if isinstance(val, (int, float)):
                        data[key] = val
                data["current"] = decoded_current

        basic = getattr(vehicle_status, "basicVehicleStatus", None)
        if basic is not None:
            data.update(self._basic(basic))

        data.update(self._electric_range(basic, charge_status))

        gps = getattr(vehicle_status, "gpsPosition", None)
        if gps is not None:
            data.update(self._gps(gps))

        return data

    @staticmethod
    def _timestamp(vehicle_status: Any) -> int:
        import time

        status_time = getattr(vehicle_status, "statusTime", None)
        if isinstance(status_time, (int, float)) and status_time > 0:
            return int(status_time)
        return int(time.time())

    @staticmethod
    def _basic(basic: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}

        is_parked = getattr(basic, "is_parked", None)
        if is_parked is not None:
            data["is_parked"] = bool(is_parked)
            if is_parked:
                # Stationary unless GPS later supplies a speed.
                data["speed"] = 0.0

        ext_temp = getattr(basic, "exteriorTemperature", None)
        if _in_range(ext_temp, -127, 127) and ext_temp != 87:
            data["ext_temp"] = ext_temp

        mileage = getattr(basic, "mileage", None)  # stored x10, in km
        if _in_range(mileage, 1, 2147483647):
            data["odometer"] = mileage / 10.0

        return data

    def _electric_range(self, basic: Any, charge_status: Any) -> dict[str, Any]:
        candidates = [
            getattr(basic, "fuelRangeElec", None) if basic is not None else None,
            getattr(charge_status, "fuelRangeElec", None)
            if charge_status is not None
            else None,
        ]
        best = 0.0
        for raw in candidates:
            if _in_range(raw, 1, 20460):  # stored x10, km
                best = max(best, raw / 10.0)
        return {"est_battery_range": best} if best > 0 else {}

    def _gps(self, gps: Any) -> dict[str, Any]:
        data: dict[str, Any] = {}
        if not _gps_has_fix(gps):
            return data

        way_point = getattr(gps, "wayPoint", None)
        if way_point is None:
            return data

        speed = getattr(way_point, "speed", None)
        if _in_range(speed, -999, 4500):
            data["speed"] = speed / 10.0

        heading = getattr(way_point, "heading", None)
        if _in_range(heading, 0, 360):
            data["heading"] = heading

        position = getattr(way_point, "position", None)
        if position is None:
            return data

        altitude = getattr(position, "altitude", None)
        if _in_range(altitude, -500, 8900):
            data["elevation"] = altitude

        raw_lat = getattr(position, "latitude", None)
        raw_lon = getattr(position, "longitude", None)
        if isinstance(raw_lat, (int, float)) and isinstance(raw_lon, (int, float)):
            lat = raw_lat / 1_000_000.0
            lon = raw_lon / 1_000_000.0
            if abs(lat) <= 90 and abs(lon) <= 180 and (lat != 0 or lon != 0):
                data["lat"] = lat
                data["lon"] = lon

        return data
