# File: backends/india.py
#
# India (TAP protocol) backend for the MG/SAIC Home Assistant integration.
# Based on mg-ismart-india-ha by John Lazarus (github.com/john-lazarus).
# Copyright (c) 2026 John Lazarus. Licensed under the MIT License.
from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

from aiohttp import ClientSession
from mg_ismart_india_client import (
    MgIndiaApiError, MgIndiaClient, hash_control_pin,
)

from ..const import (
    CHARGING_CURRENT_FACTOR, CHARGING_VOLTAGE_FACTOR,
)
from ..logic import derive_battery_capacity_kwh
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



_SECONDS_PER_MINUTE = 60
_CHARGING_DURATION_UNITS_PER_MINUTE = 100

def _charging_duration_units(seconds: int | None) -> int | None:
    """Convert elapsed seconds into the hundredths-of-a-minute the sensor decodes.

    :param seconds: :attr:`~mg_ismart_india_client.models.ChargeStatus.charge_time_elapsed_s`.
    :returns: the value for ``rvsChargeStatus.chargingDuration``, or ``None``.
    """
    if seconds is None:
        return None
    return round(
        seconds / _SECONDS_PER_MINUTE * _CHARGING_DURATION_UNITS_PER_MINUTE
    )


def _last_charge_ending_power(charge) -> int | None:
    """What the pack held when the last charge ended, in tenths of a kWh.

    The India frame has a ``lastChargeEndingPower`` field, but it mirrors live
    pack energy rather than reporting the last charge's ending energy, so the
    client decodes it as sent and leaves the interpretation here. Adding back
    the energy drawn since that charge recovers the real figure, which is what
    the global ``ending - powerUsageSinceLastCharge`` identity expects.

    :param charge: a :class:`~mg_ismart_india_client.models.ChargeStatus`.
    :returns: the value for ``rvsChargeStatus.lastChargeEndingPower``, or
        ``None`` when either term is missing.
    """
    energy = _tenths(charge.battery_energy_kwh)
    used = _tenths(charge.power_usage_since_last_charge_kwh)
    if energy is None or used is None:
        return None
    return energy + used


def _micro_degrees(value: float | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value) * 1_000_000))


def _gps_position(gps):
    """Map a decoded India GpsPosition onto the SAIC GpsPosition shape.

    The device tracker, the speed sensor and the ABRP sender all read the
    position through the EU/global protocol's own layout and units: a nested
    wayPoint/position, coordinates in micro-degrees, speed in tenths of a
    km/h. mg-ismart-india-client hands over already-decoded degrees and km/h,
    so convert back into that convention rather than teaching every consumer
    a second shape.

    Those consumers skip the block entirely when wayPoint is None. Build it
    only for a usable fix with both coordinates while retaining the status
    metadata for diagnostics when there is no fix.
    """
    latitude = _micro_degrees(getattr(gps, "latitude", None))
    longitude = _micro_degrees(getattr(gps, "longitude", None))
    if (
        not getattr(gps, "has_fix", False)
        or latitude is None
        or longitude is None
    ):
        way_point = None
    else:
        way_point = _ns(
            position=_ns(
                latitude=latitude,
                longitude=longitude,
                altitude=getattr(gps, "altitude_m", None),
            ),
            heading=getattr(gps, "heading_deg", None),
            speed=_tenths(getattr(gps, "speed_kmh", None)),
            hdop=getattr(gps, "hdop", None),
            satellites=getattr(gps, "satellites", None),
        )
    # GpsStatus is an IntEnum whose names and values match the SAIC schema's,
    # so it can stand in for the decoded status the ABRP sender looks for.
    status = getattr(gps, "gps_status", None)
    return _ns(
        wayPoint=way_point,
        gpsStatus=getattr(status, "value", None),
        gps_status_decoded=status,
        timeStamp=getattr(gps, "position_time", None),
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
            getattr(vehicle, "model", None),
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
    _india_backend = True

    def __init__(self, username, password, vin=None, pin_hash=None, country_code=None):
        self.username = username
        self.password = password
        self.vin = vin
        self.pin_hash = pin_hash
        self.country_code = country_code
        self.region_name = "India"
        self._session: ClientSession | None = None
        self._client: MgIndiaClient | None = None
        self._charge_status_by_vin = {}
        self._electric_vins = set()
        self._seat_levels_by_vin = {}
        self._api_lock = None
        self._closed = False
        self._terminal_login_error = None

    def set_api_lock(self, lock):
        """Use the account lock owned by HA; reads are locked by their cycle."""
        self._api_lock = lock

    @asynccontextmanager
    async def _command(self, vin):
        """Bind the VIN and finish PIN/control on the existing account lock."""
        from mg_ismart_india_client import MgIndiaLoginRejected

        async with self._api_lock or nullcontext():
            if self._closed:
                raise MgIndiaApiError("India account session is closed")
            if self._terminal_login_error is not None:
                raise self._terminal_login_error
            self._set_vin(vin)
            try:
                yield
            except MgIndiaLoginRejected as error:
                self._terminal_login_error = error
                raise

    async def _ensure_client(self) -> MgIndiaClient:
        if self._closed:
            raise MgIndiaApiError("India account session is closed")
        if self._terminal_login_error is not None:
            raise self._terminal_login_error
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
        from mg_ismart_india_client import MgIndiaLoginRejected

        try:
            await (await self._ensure_client()).login()
        except MgIndiaLoginRejected as error:
            self._terminal_login_error = error
            raise

    async def close(self):
        # Mark closed before waiting, so queued commands cannot reopen the session.
        self._closed = True
        async with self._api_lock or nullcontext():
            if self._session is not None:
                await self._session.close()
                self._session = None
            self._client = None

    async def get_vehicle_info(self, vin=None):
        from mg_ismart_india_client import MgIndiaLoginRejected

        try:
            self._set_vin(vin)
            client = await self._ensure_client()
            vehicles = await client.vehicles()
            self._electric_vins = {
                vehicle.vin for vehicle in vehicles if _looks_electric(vehicle)
            }
            if self.vin is None and vehicles:
                self._set_vin(vehicles[0].vin)
            return [self._map_vehicle(vehicle) for vehicle in vehicles]
        except MgIndiaLoginRejected as error:
            self._terminal_login_error = error
            raise

    async def get_vehicle_status(self, vin: str | None = None):
        from mg_ismart_india_client import MgIndiaLoginRejected

        try:
            self._set_vin(vin)
            self._charge_status_by_vin.pop(self.vin, None)
            status = await (await self._ensure_client()).status(
                include_charge=self.vin in self._electric_vins
            )
            self._charge_status_by_vin[self.vin] = status.charge
            return self._map_status(status)
        except MgIndiaLoginRejected as error:
            self._terminal_login_error = error
            raise

    def _map_vehicle(self, vehicle):
        model_name = (
            getattr(vehicle, "model", None)
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
            series=getattr(vehicle, "series", None) or "",
            colorName=getattr(vehicle, "color_name", None),
            vehicleModelConfiguration=configs,
            raw=getattr(vehicle, "raw", None),
        )

    def _map_status(self, status):
        timestamp = getattr(status, "status_time", None)
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
            powerMode=getattr(status, "power_mode_raw", None),
            engineStatus=getattr(status, "engine_status_raw", None),
            dippedBeamStatus=_flag(getattr(status, "dipped_beam_on", None)),
            sideLightStatus=_flag(getattr(status, "side_lights_on", None)),
            mainBeamStatus=_flag(getattr(status, "main_beam_on", None)),
            frontLeftTyrePressure=_tyre_pressure(status, "front_left_tyre_psi"),
            frontRightTyrePressure=_tyre_pressure(status, "front_right_tyre_psi"),
            rearLeftTyrePressure=_tyre_pressure(status, "rear_left_tyre_psi"),
            rearRightTyrePressure=_tyre_pressure(status, "rear_right_tyre_psi"),
            wheelTyreMonitorStatus=getattr(status, "tyre_monitor_status", None),
            frontLeftSeatHeatLevel=getattr(status, "front_left_seat_heat_level", None),
            frontRightSeatHeatLevel=getattr(status, "front_right_seat_heat_level", None),
            secondRowLeftSeatHeatLevel=_raw_basic(
                status, "secondRowLeftSeatHeatLevel", 0
            ),
            secondRowRightSeatHeatLevel=_raw_basic(
                status, "secondRowRightSeatHeatLevel", 0
            ),
            rmtHtdRrWndSt=_raw_basic(status, "rmtHtdRrWndSt", 0),
        )
        basic.india = True
        for seat_key, attr in (
            ("front_left", "frontLeftSeatHeatLevel"),
            ("front_right", "frontRightSeatHeatLevel"),
        ):
            level = getattr(basic, attr, None)
            if isinstance(level, int) and not isinstance(level, bool) and level in range(4):
                self._seat_levels_by_vin.setdefault(self.vin, {})[seat_key] = int(level)

        return _ns(
            statusTime=timestamp,
            basicVehicleStatus=basic,
            gpsPosition=_gps_position(getattr(status, "gps", None)),
            raw=getattr(status, "raw", None),
        )

    async def lock_vehicle(self, vin):
        async with self._command(vin):
            await (await self._ensure_client()).control_door_lock(True)


    async def unlock_vehicle(self, vin):
        async with self._command(vin):
            await (await self._ensure_client()).control_door_lock(False)


    async def open_tailgate(self, vin):
        async with self._command(vin):
            await (await self._ensure_client()).release_tailgate()


    async def control_windows(self, vin, action):
        async with self._command(vin):
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
        async with self._command(vin):
            if isinstance(action, bool):
                action = "open" if action else "close"
            action = str(action or "").lower()
            if action not in {"open", "close"}:
                raise MgIndiaApiError(f"Unsupported India sunroof action: {action}")
            await (await self._ensure_client()).control_sunroof(action == "open")


    async def start_ac(self, vin, temperature_idx=None):
        async with self._command(vin):
            if temperature_idx is not None:
                raise MgIndiaApiError("India climate does not support temperature settings")
            await (await self._ensure_client()).control_climate(True)


    async def start_climate(self, vin, temperature_idx, fan_speed, ac_on):
        async with self._command(vin):
            raise MgIndiaApiError("India climate supports only start_ac without settings and stop_ac")


    async def stop_ac(self, vin):
        async with self._command(vin):
            await (await self._ensure_client()).control_climate(False)


    async def get_charging_info(self, vin):
        """Map the India EV charging status onto the ``chrgMgmtData`` /
        ``rvsChargeStatus`` shapes the shared charging sensors read.

        Voltage, current, SOC and range come from the declared-unit fields of
        :class:`~mg_ismart_india_client.models.ChargeStatus` (volts, amps,
        percent, km) and are re-encoded onto the global SAIC raw scales the
        shared sensors decode (:data:`~..const.CHARGING_VOLTAGE_FACTOR` /
        :data:`~..const.CHARGING_CURRENT_FACTOR` / tenths), rather than assuming
        the India protocol's raw field values happen to share the global
        protocol's raw scale. ``rvsChargeStatus`` is likewise built field by
        field, so every value the sensors read has a named source and a stated
        scale assumption.

        Pack energy also goes out as ``packEnergyKwh``, in real kWh, so the
        coordinator can take it without applying scales it never went through.
        ``lastChargeEndingPower`` is reconstructed here (see
        :func:`_last_charge_ending_power`) rather than passed through, because
        the India frame's field of that name reports live pack energy and would
        break the global ``ending - powerUsageSinceLastCharge`` identity.

        ``startTime`` and ``endTime`` are not mapped: ``endTime`` is not a real
        estimate, and there is no charge-start entity for ``startTime`` to
        feed.

        The preceding status refresh requests both frames from the shared TAP
        stream and stores the charging frame here. This method only maps that
        result; it must not start a second poll.

        :param vin: VIN to report charging status for.
        :returns: a namespace carrying ``chrgMgmtData`` and ``rvsChargeStatus``,
            or ``None`` when the status poll did not receive a charging frame.
        """
        self._set_vin(vin)
        charge = self._charge_status_by_vin.pop(self.vin, None)
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
            # Already minutes, which is what the sensor's 1.0 factor expects.
            chrgngRmnngTime=charge.charge_time_remaining_min,
        )
        rvs = _ns(
            # Range and odometer come back in real units and re-encode to tenths.
            fuelRangeElec=_tenths(charge.range_km),
            mileage=_tenths(charge.odometer_km),
            chargingGunState=charge.is_plugged_in,
            chargingDuration=_charging_duration_units(charge.charge_time_elapsed_s),
            totalBatteryCapacity=_tenths(charge.total_battery_capacity_kwh),
            mileageSinceLastCharge=_tenths(charge.distance_since_last_charge_km),
            # Ours, not the car's: neither packEnergyKwh nor
            # derivedBatteryCapacityKwh appears in any SAIC frame. Both are
            # synthesised onto rvsChargeStatus for the coordinator to read, so
            # don't go looking for them in a protocol capture.
            #
            # Real kWh, so the coordinator takes it without rescaling.
            packEnergyKwh=charge.battery_energy_kwh,
            # The frame carries no capacity, so derive one from that energy and
            # the SOC. The coordinator offers it as the last-resort tier.
            derivedBatteryCapacityKwh=derive_battery_capacity_kwh(
                charge.battery_energy_kwh, charge.soc
            ),
            powerUsageSinceLastCharge=_tenths(
                charge.power_usage_since_last_charge_kwh
            ),
            lastChargeEndingPower=_last_charge_ending_power(charge),
        )
        return _ns(chrgMgmtData=chrg_mgmt, rvsChargeStatus=rvs)

    async def control_heated_seat(self, vin, seat, level):
        async with self._command(vin):
            if seat not in {"front_left", "front_right"}:
                raise MgIndiaApiError("India heated-seat control supports front seats only")
            levels = dict(self._seat_levels_by_vin.get(vin, {}))
            other = "front_right" if seat == "front_left" else "front_left"
            if other not in levels:
                raise MgIndiaApiError("Other front-seat state is unknown; set both seat levels")
            if isinstance(level, bool) or not isinstance(level, int) or level not in range(4):
                raise MgIndiaApiError("India heated-seat levels must be integers from 0 to 3")
            levels[seat] = level
            await (await self._ensure_client()).control_heated_seats(
                levels["front_left"], levels["front_right"]
            )
            self._seat_levels_by_vin[vin] = levels


    async def control_heated_seats(self, vin, left_side_level=0, right_side_level=0):
        async with self._command(vin):
            if any(isinstance(level, bool) or not isinstance(level, int) or level not in range(4) for level in (left_side_level, right_side_level)):
                raise MgIndiaApiError("India heated-seat levels must be integers from 0 to 3")
            levels = {"front_left": left_side_level, "front_right": right_side_level}
            await (await self._ensure_client()).control_heated_seats(
                levels["front_left"], levels["front_right"]
            )
            self._seat_levels_by_vin[vin] = levels


    async def trigger_alarm(
        self, vin, with_horn=True, with_lights=True, should_stop=False
    ):
        async with self._command(vin):
            if should_stop or with_horn is not True or with_lights is not True:
                raise MgIndiaApiError("India supports only the default combined find-my-car command")
            await (await self._ensure_client()).find_my_car()
