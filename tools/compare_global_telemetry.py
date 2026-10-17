#!/usr/bin/env python3
"""Compare representative global telemetry behavior in two local checkouts.

Example (use the same environment as the integration's Python test suite)::

    python tools/compare_global_telemetry.py --base ../mg-saic-ha-beta \
        --candidate . --python ../mg-saic-test-env/bin/python

The same probe source runs in isolated, bytecode-disabled subprocesses for both
checkouts. Production sensor/coordinator/session/guard code is exercised; the
repository's existing Home Assistant import stubs and fake network, storage,
event and timer boundaries avoid requiring a running HA instance or a car.
All timestamps are synthetic and clocks are fixed. Nothing is written to either
checkout. Exit 0 means equality for these scenarios, 1 means a difference, and
2 means a probe failed. Equality is scoped evidence, not a live-vehicle test.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import io
import json
import logging
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch


NOW = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
SCOPES = {
    "sensors": "SOC/range/mileage/current/voltage/power values, availability and attributes: charge, unplug, dropout, recovery, zero-SOC fallback",
    "storage": "two trips with parked energy; charge/dropout/restart/close; global trip/charge Store shape and nonempty sensor attributes; counter wobble/reset/restart",
    "abrp": "global combined/GPS-only/no-data/fallback-clock payloads; HTTP 200/401/403/500 and connection failure; coordinator cached/fresh/error dispatch",
    "polling": "real update cycle and retries: fresh, cached, charging dropout, recovered, and ICE charge-endpoint exclusion; calls, timer delays, state, freshness, events",
}


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = NOW if tz is None else NOW.astimezone(tz)
        return cls.fromtimestamp(value.timestamp(), tz) if tz else cls.fromtimestamp(value.timestamp())


def normalized(value):
    """Only canonicalize representation; never discard production dict keys."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {str(k): normalized(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalized(v) for v in value]
    raise TypeError(f"Probe cannot serialize {type(value).__name__}")


def fixture(stamp=None, *, charging=True):
    """Synthetic global endpoint fields in the existing SAIC raw units."""
    status = NS(
        statusTime=NOW.timestamp() - 30 if stamp is None else stamp,
        basicVehicleStatus=NS(
            powerMode=0, extendedData1=62, extendedData2=0,
            is_parked=True, mileage=39360, fuelRange=4000, fuelRangeElec=1500,
            interiorTemperature=23, exteriorTemperature=24, lockStatus=1,
        ),
        gpsPosition=NS(
            gps_status_decoded=2,
            wayPoint=NS(speed=0, heading=90,
                        position=NS(latitude=12000000, longitude=77000000, altitude=100)),
        ),
    )
    charge = NS(
        chrgMgmtData=NS(
            bmsChrgSts=1 if charging else 0, bmsPackSOCDsp=620,
            bmsPackCrntV=0, bmsPackCrnt=19700 if charging else 20000,
            bmsPackVol=1600, decoded_current=-15 if charging else 0,
            decoded_voltage=400, decoded_power=-6 if charging else 0,
            bmsOnBdChrgTrgtSOCDspCmd=7,
        ),
        rvsChargeStatus=NS(
            chargingGunState=1 if charging else 0, mileage=39360, fuelRangeElec=1600,
            mileageSinceLastCharge=260, powerUsageSinceLastCharge=62,
            lastChargeEndingPower=600, totalBatteryCapacity=660,
            startTime=int(NOW.timestamp()) - 7200, endTime=int(NOW.timestamp()) - 3600,
        ),
    )
    return {"status": status, "charging": charge}


def main_harness():
    harness = importlib.import_module("test_setup_and_config_flow")
    return harness, sys.modules["mg_saic.coordinator"]


def frozen(*modules):
    stack = ExitStack()
    for module in modules:
        if hasattr(module, "datetime"):
            stack.enter_context(patch.object(module, "datetime", FrozenDateTime))
        if hasattr(module, "utcnow"):
            stack.enter_context(patch.object(module, "utcnow", lambda: FrozenDateTime.now(timezone.utc)))
    stack.enter_context(patch("time.time", return_value=NOW.timestamp()))
    return stack


def probe_sensors():
    harness = importlib.import_module("test_erac_fallback")
    sensor = harness.SENSOR
    # The tiny HA stub omits this inherited HA default. Supply it on the stub
    # only, so new and inherited production attribute properties compare alike.
    for cls in sensor.SAICMGSOCSensor.__mro__:
        if cls.__name__ == "_SensorEntity":
            cls.extra_state_attributes = None
    c = NS(
        data=fixture(), vin_info=NS(vin="SYNTHETIC-VIN", brandName="MG", modelName="Global EV"),
        client=NS(), vehicle_type="BEV", last_update_success=True,
        backend_supports=lambda _feature: True, charging_capacity_correction=None,
        reliable_fuel_range_elec=True,
    )
    entry = NS(entry_id="entry")
    entities = {
        "soc": sensor.SAICMGSOCSensor(c, entry, "SOC", "extendedData1", "bmsPackSOCDsp", "basicVehicleStatus", None, None, None, None, "status"),
        "range": sensor.SAICMGElectricRangeSensor(c, entry, "Range", "fuelRangeElec", "basicVehicleStatus", "rvsChargeStatus", None, None, None, None, 0.1, "status"),
        "mileage": sensor.SAICMGMileageSensor(c, entry, "Mileage", "mileage", "basicVehicleStatus", "rvsChargeStatus", None, None, None, None, 0.1, "status"),
        "power": sensor.SAICMGChargingPowerSensor(c, entry, "Power", None, None, None, None),
        "current": sensor.SAICMGChargingCurrentSensor(c, entry, "Current", "bmsPackCrnt", None, None, None, None, 0.05),
        "voltage": sensor.SAICMGChargingSensor(c, entry, "Voltage", "bmsPackVol", None, None, None, None, 0.25),
    }
    rows = []
    with frozen(sensor, harness.LOGIC):
        for name in ("charging", "dropout_charging", "unplugged", "dropout_unplugged", "recovered", "zero_soc_fallback"):
            if name.startswith("dropout"):
                c.data = {"status": None, "charging": None}
                c.last_update_success = False
            else:
                c.data = fixture(charging=name != "unplugged")
                c.last_update_success = True
                if name == "recovered":
                    c.data["charging"].chrgMgmtData.bmsPackSOCDsp = 750
                    c.data["charging"].rvsChargeStatus.mileage = 39400
                if name == "zero_soc_fallback":
                    c.data["charging"].chrgMgmtData.bmsPackSOCDsp = 0
                    c.data["status"].basicVehicleStatus.extendedData1 = 61
            row = {}
            for key, entity in entities.items():
                row[key] = {"value": entity.native_value, "available": entity.available,
                            "attributes": entity.extra_state_attributes}
            rows.append({"step": name, "entities": row})
    return rows


async def save_manager(manager):
    saved = {}

    class Store:
        async def async_save(self, data):
            saved.update(copy.deepcopy(data))

    manager._store = Store()
    await manager.async_save()
    return saved


async def load_manager(trip, hass, saved):
    class Store:
        async def async_load(self):
            return copy.deepcopy(saved)

    manager = trip.TripStatsManager(hass, "entry", "SYNTHETIC-VIN")
    with patch.dict(sys.modules, {"homeassistant.helpers.storage": NS(Store=lambda *_a, **_k: Store())}):
        await manager.async_load()
    return manager


async def probe_storage():
    harness, coord = main_harness()
    trip = harness._load("mg_saic.comparison_trip", harness.PKG_DIR / "trip_stats.py")
    events = []
    hass = NS(bus=NS(async_fire=lambda name, data: events.append([name, copy.deepcopy(data)])))
    manager = trip.TripStatsManager(hass, "entry", "SYNTHETIC-VIN")
    snapshots = [
        ("07:00", 3910, 80, 0, 0), ("07:20", 3920, 77.6, 10, 1.6),
        ("13:00", 3920, 75, 10, 3.3), ("13:30", 3936, 70.6, 26, 6.2),
    ]
    snaps = [trip.TripSnapshot(ts=f"2026-10-15T{time}:00+00:00", odometer_km=odo,
                              soc_pct=soc, since_charge_km=km, since_charge_kwh=kwh)
             for time, odo, soc, km, kwh in snapshots]
    with frozen(trip, coord):
        for start, end in zip(snaps[::2], snaps[1::2]):
            manager.open(start)
            manager.close(end, capacity_kwh=66, tank_litres=None, is_electric=True, is_combustion=False)
        driving = trip.compute_driving_since_charge(manager.driving_since_charge, 26, 6.2)
        start = trip.ChargeSnapshot(ts="2026-10-16T18:00:00+00:00", soc_pct=40,
                                    pack_energy_kwh=26.4, odometer_km=3936, range_km=150)
        end = trip.ChargeSnapshot(ts="2026-10-16T20:00:00+00:00", soc_pct=80,
                                  pack_energy_kwh=52.8, odometer_km=3936, range_km=300)
        opened = manager.note_charge_state(True, start, capacity_kwh=66, now_iso=start.ts)
        dropout = manager.note_charge_state(False, None, capacity_kwh=66, now_iso="2026-10-16T19:00:00+00:00")
        before_restart = await save_manager(manager)
        manager = await load_manager(trip, hass, before_restart)
        after_restart = await save_manager(manager)
        closed = manager.note_charge_state(False, end, capacity_kwh=66, now_iso=end.ts)
        final_store = await save_manager(manager)
        guard = coord.SinceChargeCounterGuard()
        initial = dict(km=6100, kwh=266, ending=724, start=1789817185, end=1789834017,
                       soc=63.2, odo=60860, plugged=False)
        readings = [initial, {**initial, "km": 6099, "kwh": 265},
                    {**initial, "km": 0, "kwh": 0, "ending": 458, "start": 0, "end": 1790011819},
                    {**initial, "km": 250, "kwh": 40, "ending": 458, "start": 0, "end": 1790011819,
                     "soc": 58, "odo": 60885}]
        guard_rows = []
        for index, reading in enumerate(readings):
            result = guard.process((NOW + timedelta(minutes=index)).isoformat(), reading)
            stored = guard.to_dict()
            guard_rows.append({"result": result, "stored": copy.deepcopy(stored), "attributes": guard.attributes()})
            guard = coord.SinceChargeCounterGuard.from_dict(copy.deepcopy(stored))
    # Read the actual completed records through production entities as well as
    # comparing their stored representation. This catches attribute additions
    # or removals that pure numeric comparisons would miss.
    sensor = importlib.import_module("test_erac_fallback").SENSOR
    c = NS(client=NS(), trip_stats=manager,
           vin_info=NS(vin="SYNTHETIC-VIN", brandName="MG", modelName="Global EV"))
    entry = NS(entry_id="entry")
    entities = {
        "last_trip": sensor.SAICMGLastTripSensor(c, entry, "Last Trip Energy", "energy_kWh", None, None, None, with_attributes=True),
        "last_charge_energy": sensor.SAICMGLastChargeEnergySensor(c, entry),
        "last_charge_range": sensor.SAICMGLastChargeRangeSensor(c, entry),
    }
    entity_values = {name: {"value": entity.native_value, "available": entity.available,
                            "attributes": entity.extra_state_attributes}
                     for name, entity in entities.items()}
    return {"driving": driving, "charge_open": opened, "charge_dropout": dropout,
            "before_restart": before_restart, "after_restart": after_restart,
            "charge_close": closed, "final_store": final_store, "events": events,
            "entities": entity_values, "guard": guard_rows}


class Http:
    """Fake aiohttp I/O with exact request/response recording."""

    def __init__(self, outcome=200):
        self.outcome = outcome
        self.requests = []

    def post(self, url, *, params, headers, timeout):
        self.requests.append({"url": url, "token": params["token"],
                              "payload": json.loads(params["tlm"]), "headers": headers,
                              "timeout": timeout.total})
        outcome = self.outcome

        class Response:
            status = outcome

            async def __aenter__(self):
                if isinstance(outcome, Exception):
                    raise outcome
                return self

            async def __aexit__(self, *_args):
                return False

            async def text(self):
                return "ok" if outcome == 200 else "synthetic rejection"

        return Response()


async def probe_abrp():
    harness, coord = main_harness()
    abrp = harness._load("mg_saic.abrp", harness.PKG_DIR / "abrp.py")
    rows = []
    with frozen(coord):
        outcomes = [200, 401, 403, 500, abrp.aiohttp.ClientConnectionError("synthetic disconnect")]
        for outcome in outcomes:
            http = Http(outcome)
            api = abrp.AbrpApi(http, "synthetic-key", "synthetic-token")
            data = fixture()
            try:
                result = {"return": await api.async_send(data["status"], data["charging"])}
            except abrp.AbrpError as error:
                result = {"exception": type(error).__name__, "message": str(error)}
            rows.append({"case": str(outcome), "result": result, "requests": http.requests})
        for name in ("gps_only", "no_usable_data", "fallback_clock"):
            http = Http()
            data = fixture()
            if name == "gps_only":
                data["charging"] = None
            elif name == "no_usable_data":
                data = {"status": None, "charging": None}
            else:
                data["status"].statusTime = None
            result = await abrp.AbrpApi(http, "synthetic-key", "synthetic-token").async_send(data["status"], data["charging"])
            rows.append({"case": name, "result": result, "requests": http.requests})
        # Exercise the coordinator's production global freshness/error boundary
        # as well as AbrpApi's direct return/exception contract.
        c = coord.SAICMGDataUpdateCoordinator.__new__(coord.SAICMGDataUpdateCoordinator)
        c.client, c.hass, c.vin = NS(), NS(), "SYNTHETIC-VIN"
        c.config_entry = NS(options={coord.CONF_ABRP_USER_TOKEN: "synthetic-token",
                                     coord.CONF_ABRP_API_KEY: "synthetic-key"})
        for outcome in outcomes:
            http = Http(outcome)
            with patch.object(coord, "async_get_clientsession", return_value=http):
                cached = await c._maybe_send_abrp(fixture(), fresh_status=False)
                cached_calls = len(http.requests)
                fresh = await c._maybe_send_abrp(fixture(), fresh_status=True)
            rows.append({"case": f"coordinator_{outcome}", "cached_return": cached,
                         "cached_calls": cached_calls, "fresh_return": fresh, "requests": http.requests})
    return rows


async def probe_polling():
    harness, coord = main_harness()
    rows = []

    class Client:
        def __init__(self):
            self.data = fixture()
            self.calls = []

        async def get_vehicle_info(self):
            self.calls.append(["info"])
            return [NS(vin="SYNTHETIC-VIN", brandName="MG", modelName="Global EV")]

        async def get_vehicle_status(self, vin):
            self.calls.append(["status", vin])
            return copy.deepcopy(self.data["status"])

        async def get_charging_info(self, vin):
            self.calls.append(["charging", vin])
            return copy.deepcopy(self.data["charging"])

    sleeps, events = [], []

    async def timer_sleep(delay):
        sleeps.append(delay)

    hass = NS(bus=NS(async_fire=lambda name, data: events.append([name, copy.deepcopy(data)])))
    with frozen(coord, sys.modules["mg_saic.logic"], sys.modules["mg_saic.trip_stats"]), patch("asyncio.sleep", timer_sleep):
        for vehicle_type in ("BEV", "ICE"):
            client = Client()
            entry = NS(entry_id="entry", data={"vin": "SYNTHETIC-VIN", "vehicle_type": vehicle_type}, options={})
            c = coord.SAICMGDataUpdateCoordinator(hass, client, entry)
            # Existing HA import stubs don't implement superclass lifecycle.
            c.hass, c.data, c.last_update_success = hass, {}, True
            c.update_interval = timedelta(hours=1)
            c.trip_stats = coord.TripStatsManager(hass, "entry", "SYNTHETIC-VIN")
            c.async_update_listeners = MagicMock()  # HA entity event boundary
            c._schedule_trip_save = MagicMock()  # HA debounced storage boundary
            c._schedule_refresh = MagicMock()  # HA timer boundary
            steps = ("fresh", "cached", "charging_dropout", "recovered") if vehicle_type == "BEV" else ("fresh",)
            for step in steps:
                client.data = fixture(stamp=NOW.timestamp() - (20 if step == "recovered" else 30))
                if step == "charging_dropout":
                    client.data["charging"] = None
                start_calls, start_sleeps, start_events = len(client.calls), len(sleeps), len(events)
                listeners, saves, schedules = c.async_update_listeners.call_count, c._schedule_trip_save.call_count, c._schedule_refresh.call_count
                c.data = await c._run_update_cycle()
                rows.append({
                    "vehicle_type": vehicle_type, "step": step,
                    "endpoint_calls": client.calls[start_calls:], "retry_delays_s": sleeps[start_sleeps:],
                    "events": events[start_events:], "listeners": c.async_update_listeners.call_count - listeners,
                    "save_schedules": c._schedule_trip_save.call_count - saves,
                    "refresh_schedules": c._schedule_refresh.call_count - schedules,
                    "status_present": c.data.get("status") is not None, "charging_present": c.data.get("charging") is not None,
                    "charging_retained": c.last_charging_data is not None,
                    "charging": c.is_charging, "dc_charging": c.is_dc_charging, "powered": c.is_powered_on,
                    "interval_s": c.update_interval.total_seconds(), "freshness": c.data_freshness,
                    "charging_freshness": c.charging_data_freshness,
                    "charging_attributes": c.charging_freshness.attributes(NOW),
                    "last_vehicle_activity": c.last_vehicle_activity, "last_status_time": c._last_status_time,
                    "refresh_running": c.refresh_in_progress, "refresh_step": c.refresh_step,
                    "trip_store": await save_manager(c.trip_stats), "guard_store": c.counter_reset_guard.to_dict(),
                })
    return rows


def differences(base, candidate, path="$", limit=40):
    found = []

    def compare(a, b, at):
        if len(found) >= limit:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                if key not in a or key not in b:
                    found.append({"path": f"{at}.{key}", "base": a.get(key, {"missing": True}),
                                  "candidate": b.get(key, {"missing": True})})
                else:
                    compare(a[key], b[key], f"{at}.{key}")
                if len(found) >= limit:
                    break
        elif isinstance(a, list) and isinstance(b, list):
            if len(a) != len(b):
                found.append({"path": f"{at}.length", "base": len(a), "candidate": len(b)})
            for index, (left, right) in enumerate(zip(a, b)):
                compare(left, right, f"{at}[{index}]")
        elif type(a) is not type(b) or a != b:
            found.append({"path": at, "base": a, "candidate": b})

    compare(base, candidate, path)
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--python", default=sys.executable, help="one interpreter/environment used for both checkouts")
    parser.add_argument("--_probe", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--scenario", choices=SCOPES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args._probe:
        sys.path.insert(0, str(args._probe.resolve() / "tests"))
        logging.disable(logging.CRITICAL)
        probe = globals()[f"probe_{args.scenario}"]
        with redirect_stdout(io.StringIO()):
            result = probe()
            if asyncio.iscoroutine(result):
                result = asyncio.run(result)
        print(json.dumps(normalized(result), sort_keys=True, allow_nan=False))
        return 0
    if not args.base or not args.candidate:
        parser.error("--base and --candidate are required")
    paths = {"base": args.base.resolve(), "candidate": args.candidate.resolve()}
    for name, path in paths.items():
        if not (path / "tests" / "test_setup_and_config_flow.py").is_file():
            parser.error(f"{name} lacks the existing HA test harness: {path}")
    # Keep the venv executable path: resolving its symlink would report (and if
    # used to launch, select) the system environment instead of the test venv.
    interpreter = str(Path(shutil.which(args.python) or args.python).absolute())
    report = {"equal": True, "python": interpreter, "clock": NOW.isoformat(),
              "checkouts": {key: str(path) for key, path in paths.items()}, "scenarios": {},
              "limits": "Representative synthetic global scenarios; HA import stubs and I/O fakes; no live car, HA runtime, timeout timing, command control, or India behavior validation."}
    errors = False
    for scenario, scope in SCOPES.items():
        values = {}
        row = {"scope": scope}
        for name, path in paths.items():
            command = [interpreter, "-I", "-B", str(Path(__file__).resolve()), "--_probe", str(path), "--scenario", scenario]
            try:
                result = subprocess.run(command, cwd=path, capture_output=True, text=True, timeout=40)
                if result.returncode:
                    raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:] or f"exit {result.returncode}")
                values[name] = json.loads(result.stdout)
            except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
                row[f"{name}_error"] = str(error)
                errors = True
        if len(values) == 2:
            delta = differences(values["base"], values["candidate"])
            row["equal"] = not delta
            row["observed_bytes"] = {key: len(json.dumps(value, sort_keys=True)) for key, value in values.items()}
            row["sha256"] = {key: hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest() for key, value in values.items()}
            if delta:
                row["differences_first_40"] = delta
                report["equal"] = False
        else:
            row["equal"] = None
            report["equal"] = False
        report["scenarios"][scenario] = row
    print(json.dumps(report, indent=2, sort_keys=True))
    return 2 if errors else 0 if report["equal"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
