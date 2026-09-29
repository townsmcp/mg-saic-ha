"""Rear heated seats: read from the car, and shown as On/Off sensors.

The car's /vehicle/status response carries secondRowLeftSeatHeatLevel and
secondRowRightSeatHeatLevel (confirmed from an MGS6 response, 2026-09-29).
mg-saic-client up to 0.9.4 had no fields for them, so they were dropped and
the rear seat switches could only ever show Off. 0.9.5 adds them.
"""

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_india_soc import BACKENDS, INDIA, LOGIC, SENSOR

REPO_ROOT = Path(__file__).resolve().parent.parent
SETUP = sys.modules["mg_saic.setup_module"]

# The MGS6's real status response (trimmed to the basicVehicleStatus keys that
# matter here, plus GPS so the nested types are exercised), 2026-09-29 21:45.
MGS6_STATUS_2026_09_29 = {
    "basicVehicleStatus": {
        "frontLeftSeatHeatLevel": 0,
        "frontRightSeatHeatLevel": 0,
        "secondRowLeftSeatHeatLevel": 3,
        "secondRowRightSeatHeatLevel": 0,
        "steeringHeatLevel": 0,
        "lockStatus": 1,
        "remoteClimateStatus": 0,
        "mileage": -128,
        "rearRightOSTyrePressure": -128,
        "elecRangeDspMode": -128,
        "fuelRangeElec": 3430,
    },
    "extendedVehicleStatus": {"alertDataSum": [0, 0, 0]},
    "gpsPosition": {
        "timeStamp": 1790714719,
        "gpsStatus": 3,
        "wayPoint": {
            "satellites": 16,
            "heading": 21,
            "position": {"altitude": 42, "latitude": 51117513, "longitude": 891696},
            "hdop": 5,
            "speed": 0,
        },
    },
    "statusTime": 1790714718,
}

MANIFEST = json.loads(
    (REPO_ROOT / "custom_components" / "mg_saic" / "manifest.json").read_text()
)
PINNED_CLIENT = next(
    r for r in MANIFEST["requirements"] if r.startswith("mg-saic-client")
)

# Runs in a clean interpreter against the REAL installed library (the shared
# test harness stubs it in this process): deserialise the captured MGS6
# response exactly as the library does.
_REAL_LIBRARY_CHECK = """
import dacite, json, sys
from importlib.metadata import version
from saic_ismart_client_ng.api.vehicle import VehicleStatusResp
status = dacite.from_dict(VehicleStatusResp, json.loads(sys.argv[1]))
b = status.basicVehicleStatus
print(json.dumps({
    "version": version("mg-saic-client"),
    "rear": [getattr(b, "secondRowLeftSeatHeatLevel", "missing"),
             getattr(b, "secondRowRightSeatHeatLevel", "missing")],
}))
"""


def _real_library():
    result = subprocess.run(
        [sys.executable, "-c", _REAL_LIBRARY_CHECK, json.dumps(MGS6_STATUS_2026_09_29)],
        capture_output=True, text=True, timeout=60,
    )
    return json.loads(result.stdout) if result.returncode == 0 else None


class PinnedLibraryTests(unittest.TestCase):
    def test_manifest_pins_a_client_that_reads_rear_seats(self):
        # 0.9.5 is the first mg-saic-client with the rear seat fields.
        pinned = tuple(int(p) for p in PINNED_CLIENT.split("==")[1].split("."))
        self.assertGreaterEqual(pinned, (0, 9, 5))

    def test_installed_library_keeps_the_rear_seat_levels(self):
        out = _real_library()
        if out is None:
            self.skipTest("mg-saic-client not installed")
        if f"mg-saic-client=={out['version']}" != PINNED_CLIENT:
            self.skipTest(f"installed {out['version']}, manifest pins {PINNED_CLIENT}")
        self.assertEqual(out["rear"], [3, 0])


def _setup_sensors(*, heated_seats, rear_heated_seats, backend, status):
    vin_info = SimpleNamespace(
        vin="VIN1", brandName="MG", modelName="MGS6 EV", modelYear="2025",
        series="MIS3E S", colorName=None,
    )
    coordinator = SimpleNamespace(
        data={"info": [vin_info], "status": status, "charging": None},
        vin_info=vin_info,
        vehicle_type="BEV",
        client=backend,
        last_update_success=True,
        has_heated_seats=heated_seats,
        has_rear_heated_seats=rear_heated_seats,
        has_battery_heating=False,
        has_steering_wheel_heat=False,
        supports_charging_current_limit=False,
        supports_target_soc=False,
    )
    resolution = LOGIC.resolve_battery_capacity(None, None, None, factor=0.1)
    coordinator.battery_capacity_override = None
    coordinator._profile_battery_capacity_kwh = None
    coordinator.known_battery_capacity_kwh = resolution[0]
    coordinator.battery_capacity_resolution = resolution
    coordinator.effective_battery_capacity_kwh = resolution[0]
    coordinator.backend_supports = lambda feature: BACKENDS.backend_supports(
        backend, feature
    )
    entry = SimpleNamespace(entry_id="entry-1", data={})
    hass = SimpleNamespace(data={"mg_saic": {"entry-1_coordinator": coordinator}})
    entities = []
    asyncio.run(
        SENSOR.async_setup_entry(
            hass, entry, lambda added, update_before_add: entities.extend(added)
        )
    )
    return {
        e._name: e for e in entities if isinstance(e, SENSOR.SAICMGHeatedSeatLevelSensor)
    }


def _status(rear_left=None, rear_right=None, front_left=0, front_right=0):
    basic = SimpleNamespace(
        frontLeftSeatHeatLevel=front_left, frontRightSeatHeatLevel=front_right
    )
    if rear_left is not None:
        basic.secondRowLeftSeatHeatLevel = rear_left
    if rear_right is not None:
        basic.secondRowRightSeatHeatLevel = rear_right
    return SimpleNamespace(basicVehicleStatus=basic)


GLOBAL = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
REAR_NAMES = {"Rear Left Heated Seat Status", "Rear Right Heated Seat Status"}
FRONT_NAMES = {"Front Left Heated Seat Level", "Front Right Heated Seat Level"}


class RearSeatSensorTests(unittest.TestCase):
    def test_rear_option_on_creates_rear_sensors(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(0, 0),
        )
        self.assertEqual(set(sensors), FRONT_NAMES | REAR_NAMES)

    def test_rear_option_off_creates_front_sensors_only(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=False, backend=GLOBAL,
            status=_status(0, 0),
        )
        self.assertEqual(set(sensors), FRONT_NAMES)

    def test_heated_seats_off_creates_no_seat_sensors(self):
        # Same nesting as the switches: rear sits under "Has Heated Seats".
        sensors = _setup_sensors(
            heated_seats=False, rear_heated_seats=True, backend=GLOBAL,
            status=_status(0, 0),
        )
        self.assertEqual(sensors, {})

    def test_india_backend_gets_no_rear_sensors(self):
        india = INDIA.IndiaBackend("user", "password", vin="VIN1")
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=india,
            status=_status(0, 0),
        )
        self.assertEqual(set(sensors), FRONT_NAMES)

    def test_rear_seats_read_on_or_off(self):
        # The rear seats have no levels (app and car offer on/off only); the
        # app's "on" arrives as 3. Whatever turned them on -- HA, the app or
        # the car's button -- the car reports it and the sensors show it.
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(rear_left=3, rear_right=0),
        )
        self.assertEqual(sensors["Rear Left Heated Seat Status"].native_value, "On")
        self.assertEqual(sensors["Rear Right Heated Seat Status"].native_value, "Off")

    def test_any_non_zero_rear_value_is_on(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(rear_left=1, rear_right=2),
        )
        self.assertEqual(sensors["Rear Left Heated Seat Status"].native_value, "On")
        self.assertEqual(sensors["Rear Right Heated Seat Status"].native_value, "On")

    def test_front_seats_keep_their_levels(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(rear_left=0, rear_right=0, front_left=2, front_right=3),
        )
        self.assertEqual(sensors["Front Left Heated Seat Level"].native_value, "Medium")
        self.assertEqual(sensors["Front Right Heated Seat Level"].native_value, "High")

    def test_unique_ids_are_distinct_and_stable(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(0, 0),
        )
        self.assertEqual(
            sensors["Rear Left Heated Seat Status"].unique_id,
            "entry-1_VIN1_secondRowLeftSeatHeatLevel_seat_heat_level",
        )
        self.assertEqual(len({s.unique_id for s in sensors.values()}), 4)

    def test_missing_field_is_not_reported_as_off(self):
        # A car (or an older library) that doesn't send the field: the sensor
        # must say nothing rather than claim the seat is off.
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(),
        )
        self.assertIsNone(sensors["Rear Left Heated Seat Status"].native_value)


class CapabilityReloadTests(unittest.TestCase):
    """Ticking "Has Rear Heated Seats" should create the entities straight away."""

    def _run_listener(self, *, before, after):
        coordinator = SimpleNamespace(**before)

        async def update_options(_options):
            for key, value in after.items():
                setattr(coordinator, key, value)

        coordinator.async_update_options = update_options
        reload = MagicMock(return_value="reload-coro")
        created = []
        hass = SimpleNamespace(
            data={SETUP.DOMAIN: {"e1_coordinator": coordinator}},
            config_entries=SimpleNamespace(async_reload=reload),
            async_create_task=created.append,
        )
        entry = SimpleNamespace(entry_id="e1", title="MG", options={})
        asyncio.run(SETUP.update_listener(hass, entry))
        return reload, created

    def test_capability_change_reloads(self):
        reload, created = self._run_listener(
            before={"has_heated_seats": True, "has_rear_heated_seats": False},
            after={"has_rear_heated_seats": True},
        )
        reload.assert_called_once_with("e1")
        self.assertEqual(created, ["reload-coro"])

    def test_other_option_changes_do_not_reload(self):
        reload, created = self._run_listener(
            before={"has_heated_seats": True, "has_rear_heated_seats": True},
            after={"update_interval": 30},
        )
        reload.assert_not_called()
        self.assertEqual(created, [])


if __name__ == "__main__":
    unittest.main()
