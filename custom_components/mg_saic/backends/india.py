# File: backends/india.py
#
# India (TAP protocol) backend for the MG/SAIC Home Assistant integration.
# Based on mg-ismart-india-ha by John Lazarus (github.com/john-lazarus).
# Copyright (c) 2026 John Lazarus. Licensed under the MIT License.
from __future__ import annotations

import time
from types import SimpleNamespace

from aiohttp import ClientSession
from mg_ismart_india_client import MgIndiaApiError, MgIndiaClient, hash_control_pin

from ..const import CHARGING_CURRENT_FACTOR, CHARGING_VOLTAGE_FACTOR, LOGGER
from . import INDIA_FEATURES


class IndiaBackendNotReadyError(Exception):
    """Backward-compatible name for pre-client scaffold failures."""


def hash_india_pin(pin: str) -> str:
    """Return the MG India command-PIN hash for a 4-digit PIN."""
    try:
        return hash_control_pin(str(pin).strip())
    except MgIndiaApiError as err:
        raise ValueError(str(err)) from err


def _ns(**kwargs):
    return SimpleNamespace(**kwargs)


def _flag(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _lock_flag(locked: bool | None) -> int | None:
    if locked is None:
        return None
    return 1 if locked else 0


def _tenths(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 10))


def _raw_basic(status, key: str, default=None):
    raw = getattr(status, "raw", None)
    if isinstance(raw, dict):
        basic = raw.get("basicVehicleStatus", raw)
        if isinstance(basic, dict) and key in basic:
            return basic[key]
    return default


# mg-ismart-india-client reports tyre pressure in psi, while the SAIC
# EU/global protocol uses units of 4 kPa (0.04 bar) and the shared sensor
# always multiplies by PRESSURE_TO_BAR. Rescale into that convention here
_BAR_PER_PSI = 0.0689476
_EU_TYRE_BAR_PER_UNIT = 0.04


def _tyre_pressure(status, attribute: str) -> float | None:
    psi = getattr(status, attribute, None)
    if not psi:
        return None
    return round(psi * _BAR_PER_PSI / _EU_TYRE_BAR_PER_UNIT, 2)


# The global SAIC protocol encodes charging current as an offset from a 1000 A
# zero point (decoded as 1000 - raw * CHARGING_CURRENT_FACTOR), so real amps are
# re-encoded against the same zero point below.
_CHARGING_CURRENT_ZERO_A = 1000

# bmsChrgSts codes the shared charging sensors decode; names match the mapping in
# sensor.py. India reports charging as two booleans, so only these three are used.
_BMS_CHRG_STS_UNPLUGGED = 0
_BMS_CHRG_STS_CHARGING = 3
_BMS_CHRG_STS_PLUGGED_IN = 7  # connected but not charging: paused or full

# The Charging Duration sensor reads rvsChargeStatus.chargingDuration with
# DATA_100_DECIMAL_CORRECTION and labels the result minutes, i.e. it expects
# hundredths of a minute. The India client reports elapsed session time in
# seconds, so convert instead of passing the seconds straight through.
_SECONDS_PER_MINUTE = 60
_CHARGING_DURATION_UNITS_PER_MINUTE = 100


def _charging_duration_units(seconds: int | None) -> int | None:
    """Convert elapsed seconds into the hundredths-of-a-minute the sensor decodes."""
    if seconds is None:
        return None
    return round(
        seconds / _SECONDS_PER_MINUTE * _CHARGING_DURATION_UNITS_PER_MINUTE
    )


def _vehicle_config(vehicle, code: str, default=None):
    raw = getattr(vehicle, "raw", None)
    if isinstance(raw, dict):
        for block_name in ("configuration", "config", "modelConfig"):
            block = raw.get(block_name)
            if isinstance(block, dict) and code in block:
                return block[code]
        for item in raw.get("vehicleModelConfiguration", []) or []:
            if isinstance(item, dict) and item.get("itemCode") == code:
                return item.get("itemValue", default)
    return default


def _looks_electric(vehicle) -> bool:
    text = " ".join(
        str(value or "")
        for value in (
            getattr(vehicle, "name", None),
            getattr(vehicle, "model_name", None),
            getattr(vehicle, "brand", None),
            _vehicle_config(vehicle, "EV"),
            _vehicle_config(vehicle, "BType"),
        )
    ).lower()
    if str(_vehicle_config(vehicle, "EV", "")).strip() == "1":
        return True
    if str(_vehicle_config(vehicle, "BType", "")).strip() == "1":
        return True
    return any(token in text for token in ("electric", " ev", "comet", "zs", "windsor"))


class IndiaBackend:
    """Backend for MG India vehicles (TAP binary protocol)."""

    supported_features = INDIA_FEATURES

    def __init__(self, username, password, vin=None, pin_hash=None, country_code=None):
        self.username = username
        self.password = password
        self.vin = vin
        self.pin_hash = pin_hash
        self.country_code = country_code
        self.region_name = "India"
        self._session: ClientSession | None = None
        self._client: MgIndiaClient | None = None
        self._seat_levels = {"front_left": 0, "front_right": 0}

    async def _ensure_client(self) -> MgIndiaClient:
        if self._client is not None:
            return self._client
        self._session = ClientSession()
        self._client = MgIndiaClient(
            self._session,
            self.username,
            self.password,
            vin=self.vin,
            pin_hash=self.pin_hash,
        )
        return self._client

    def _set_vin(self, vin: str | None) -> None:
        if vin:
            self.vin = vin
            if self._client is not None:
                self._client.vin = vin

    async def login(self):
        await (await self._ensure_client()).login()

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None
        self._client = None

    async def get_vehicle_info(self):
        client = await self._ensure_client()
        vehicles = await client.vehicles()
        if self.vin is None and vehicles:
            self._set_vin(vehicles[0].vin)
        return [self._map_vehicle(vehicle) for vehicle in vehicles]

    async def get_vehicle_status(self, vin: str | None = None):
        self._set_vin(vin)
        return self._map_status(await (await self._ensure_client()).status())

    def _map_vehicle(self, vehicle):
        model_name = (
            getattr(vehicle, "model_name", None)
            or getattr(vehicle, "name", None)
            or "MG India"
        )
        brand = getattr(vehicle, "brand", None) or "MG"
        is_electric = _looks_electric(vehicle)
        configs = [
            _ns(itemCode="LRD", itemValue=str(_vehicle_config(vehicle, "LRD", "1"))),
            _ns(itemCode="EV", itemValue="1" if is_electric else "0"),
            _ns(itemCode="BType", itemValue="1" if is_electric else "0"),
            _ns(
                itemCode="ENERGY",
                itemValue=str(_vehicle_config(vehicle, "ENERGY", "0")),
            ),
            _ns(itemCode="S35", itemValue=str(_vehicle_config(vehicle, "S35", "0"))),
            _ns(
                itemCode="HeatedSeat",
                itemValue=str(_vehicle_config(vehicle, "HeatedSeat", "0")),
            ),
        ]
        return _ns(
            vin=vehicle.vin,
            brandName=brand,
            modelName=model_name,
            modelYear=getattr(vehicle, "model_year", None) or "",
            series=getattr(vehicle, "name", None) or model_name,
            vehicleModelConfiguration=configs,
            raw=getattr(vehicle, "raw", None),
        )

    def _map_status(self, status):
        raw_timestamp = getattr(status, "status_time", None)
        if raw_timestamp is None:
            LOGGER.debug(
                "MG India status payload missing status_time; using local receipt time"
            )
            timestamp = int(time.time())
        else:
            timestamp = raw_timestamp
        range_tenths = _tenths(getattr(status, "range_km", None))
        basic = _ns(
            lockStatus=_lock_flag(getattr(status, "locked", None)),
            driverDoor=_flag(getattr(status, "driver_door_open", None)),
            passengerDoor=_flag(getattr(status, "passenger_door_open", None)),
            rearLeftDoor=_flag(getattr(status, "rear_left_door_open", None)),
            rearRightDoor=_flag(getattr(status, "rear_right_door_open", None)),
            bootStatus=_flag(getattr(status, "boot_open", None)),
            bonnetStatus=_flag(getattr(status, "bonnet_open", None)),
            driverWindow=_flag(getattr(status, "driver_window_open", None)),
            passengerWindow=_flag(getattr(status, "passenger_window_open", None)),
            rearLeftWindow=_flag(getattr(status, "rear_left_window_open", None)),
            rearRightWindow=_flag(getattr(status, "rear_right_window_open", None)),
            sunroofStatus=_flag(getattr(status, "sunroof_open", None)),
            remoteClimateStatus=(
                2
                if getattr(status, "climate_running", None) is True
                else 0
                if getattr(status, "climate_running", None) is False
                else None
            ),
            interiorTemperature=getattr(status, "interior_temperature", None),
            exteriorTemperature=getattr(status, "exterior_temperature", None),
            fuelLevelPrc=getattr(status, "fuel_level", None),
            extendedData1=getattr(status, "fuel_level", None),
            fuelRange=range_tenths,
            fuelRangeElec=range_tenths,
            mileage=_tenths(getattr(status, "odometer_km", None)),
            batteryVoltage=_tenths(getattr(status, "aux_battery_voltage", None)),
            canBusActive=_flag(getattr(status, "can_bus_active", None)),
            timeOfLastCANBUSActivity=getattr(status, "last_can_activity", None),
            handBrake=_flag(getattr(status, "handbrake", None)),
            powerMode=_raw_basic(status, "powerMode", 0),
            engineStatus=_raw_basic(status, "engineStatus", 0),
            dippedBeamStatus=_raw_basic(status, "dippedBeamStatus", 0),
            mainBeamStatus=_raw_basic(status, "mainBeamStatus", 0),
            frontLeftTyrePressure=_tyre_pressure(status, "front_left_tyre_psi"),
            frontRightTyrePressure=_tyre_pressure(status, "front_right_tyre_psi"),
            rearLeftTyrePressure=_tyre_pressure(status, "rear_left_tyre_psi"),
            rearRightTyrePressure=_tyre_pressure(status, "rear_right_tyre_psi"),
            wheelTyreMonitorStatus=getattr(status, "tyre_monitor_status", None),
            frontLeftSeatHeatLevel=_raw_basic(status, "frontLeftSeatHeatLevel", 0),
            frontRightSeatHeatLevel=_raw_basic(status, "frontRightSeatHeatLevel", 0),
            secondRowLeftSeatHeatLevel=_raw_basic(
                status, "secondRowLeftSeatHeatLevel", 0
            ),
            secondRowRightSeatHeatLevel=_raw_basic(
                status, "secondRowRightSeatHeatLevel", 0
            ),
            rmtHtdRrWndSt=_raw_basic(status, "rmtHtdRrWndSt", 0),
        )
        for seat_key, attr in (
            ("front_left", "frontLeftSeatHeatLevel"),
            ("front_right", "frontRightSeatHeatLevel"),
        ):
            level = getattr(basic, attr, None)
            if level is not None:
                self._seat_levels[seat_key] = int(level)

        return _ns(
            statusTime=timestamp,
            basicVehicleStatus=basic,
            gpsPosition=_ns(speed=_raw_basic(status, "speed")),
            raw=getattr(status, "raw", None),
        )

    async def lock_vehicle(self, vin):
        self._set_vin(vin)
        await (await self._ensure_client()).control_door_lock(True)

    async def unlock_vehicle(self, vin):
        self._set_vin(vin)
        await (await self._ensure_client()).control_door_lock(False)

    async def open_tailgate(self, vin):
        self._set_vin(vin)
        await (await self._ensure_client()).release_tailgate()

    async def control_windows(self, vin, action):
        self._set_vin(vin)
        action = str(action or "").lower()
        if action == "ventilate":
            raise MgIndiaApiError("ventilate not confirmed for India")
        if action == "open":
            await (await self._ensure_client()).control_windows(True, (9, 10, 11, 12))
            return
        if action == "close":
            await (await self._ensure_client()).control_windows(False, (9, 10, 11, 12))
            return
        raise MgIndiaApiError(f"Unsupported India window action: {action}")

    async def control_sunroof(self, vin, action):
        self._set_vin(vin)
        action = str(action or "").lower()
        if action not in {"open", "close"}:
            raise MgIndiaApiError(f"Unsupported India sunroof action: {action}")
        await (await self._ensure_client()).control_sunroof(action == "open")

    async def start_ac(self, vin, temperature_idx=None):
        self._set_vin(vin)
        await (await self._ensure_client()).control_climate(True)

    async def start_climate(self, vin, temperature_idx, fan_speed, ac_on):
        self._set_vin(vin)
        await (await self._ensure_client()).control_climate(True)

    async def stop_ac(self, vin):
        self._set_vin(vin)
        await (await self._ensure_client()).control_climate(False)

    async def get_charging_info(self, vin):
        """Map the India EV charging status onto the chrgMgmtData / rvsChargeStatus
        shapes the shared charging sensors read.

        Voltage, current, SOC and range come from the client's declared-unit
        ChargeStatus fields (volts, amps, percent, km) and are re-encoded onto the
        global SAIC raw scales the shared sensors decode
        (CHARGING_VOLTAGE_FACTOR / CHARGING_CURRENT_FACTOR / tenths), rather than
        assuming the India protocol's raw field values happen to share the global
        protocol's raw scale. rvsChargeStatus is likewise built field by field,
        so every value the sensors read has a named source and a stated scale
        assumption. Returns None when the vehicle sends
        no charging frame, which the coordinator handles gracefully; session and
        protocol failures propagate from the client so they are logged rather than
        silently reported as "not charging".
        """
        self._set_vin(vin)
        charge = await (await self._ensure_client()).charge_status()
        if charge is None:
            return None
        if charge.is_charging:
            bms_chrg_sts = _BMS_CHRG_STS_CHARGING
        elif charge.is_plugged_in:
            bms_chrg_sts = _BMS_CHRG_STS_PLUGGED_IN
        else:
            bms_chrg_sts = _BMS_CHRG_STS_UNPLUGGED
        chrg_mgmt = _ns(
            bmsPackVol=round(charge.charging_voltage / CHARGING_VOLTAGE_FACTOR),
            bmsPackCrnt=round(
                (_CHARGING_CURRENT_ZERO_A - charge.charging_current)
                / CHARGING_CURRENT_FACTOR
            ),
            bmsPackSOCDsp=_tenths(charge.soc),
            bmsChrgSts=bms_chrg_sts,
        )
        rvs = _ns(
            # Range and odometer come back in real units and re-encode to tenths.
            fuelRangeElec=_tenths(charge.range_km),
            mileage=_tenths(charge.odometer_km),
            chargingGunState=charge.is_plugged_in,
            chargingDuration=_charging_duration_units(charge.charge_time_elapsed_s),
            # These three have no confirmed scale on the India frame, so the
            # client hands back the vehicle's own integer and we forward it on
            # the assumption that it matches the global protocol's scale. If a
            # sensor reads wrong, this is the line to correct.
            totalBatteryCapacity=charge.total_battery_capacity_raw,
            mileageSinceLastCharge=charge.mileage_since_last_charge_raw,
            powerUsageSinceLastCharge=charge.power_usage_since_last_charge_raw,
        )
        return _ns(chrgMgmtData=chrg_mgmt, rvsChargeStatus=rvs)

    async def control_heated_seat(self, vin, seat, level):
        self._set_vin(vin)
        if seat not in self._seat_levels:
            raise MgIndiaApiError(
                f"India heated-seat control is not confirmed for {seat}"
            )
        self._seat_levels[seat] = int(level)
        await (await self._ensure_client()).control_heated_seats(
            self._seat_levels["front_left"], self._seat_levels["front_right"]
        )

    async def control_heated_seats(self, vin, left_side_level=0, right_side_level=0):
        self._set_vin(vin)
        self._seat_levels["front_left"] = int(left_side_level)
        self._seat_levels["front_right"] = int(right_side_level)
        await (await self._ensure_client()).control_heated_seats(
            self._seat_levels["front_left"], self._seat_levels["front_right"]
        )

    async def trigger_alarm(
        self, vin, with_horn=True, with_lights=True, should_stop=False
    ):
        self._set_vin(vin)
        if should_stop:
            LOGGER.debug(
                "MG India find-my-car stop requested for VIN %s; "
                "no stop command is exposed by the TAP client",
                vin,
            )
            return None
        await (await self._ensure_client()).find_my_car()
