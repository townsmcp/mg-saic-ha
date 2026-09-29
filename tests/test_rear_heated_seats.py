"""Rear heated seat levels: read from the car, and shown as sensors.

The car's /vehicle/status response carries secondRowLeftSeatHeatLevel and
secondRowRightSeatHeatLevel (confirmed from an MGS6 response, 2026-09-29), but
the SAIC client library's BasicVehicleStatus dataclass has no fields for them,
so dacite dropped them and the rear seat switches could only ever show Off.
status_schema.py fetches the same status into a subclass that keeps them.
"""

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_india_soc import BACKENDS, INDIA, LOGIC, SENSOR

REPO_ROOT = Path(__file__).resolve().parent.parent
STATUS_SCHEMA = sys.modules["mg_saic.status_schema"]
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

HAVE_SAIC_LIB = (
    subprocess.run(
        [sys.executable, "-c", "import saic_ismart_client_ng"],
        capture_output=True,
        timeout=60,
    ).returncode
    == 0
)

# Runs in a clean interpreter against the REAL pinned library (the shared
# test harness stubs it in this process). Loads status_schema.py with a tiny
# stand-in for .const, then:
#   1. records the request the library's own get_vehicle_status makes and the
#      one fetch_vehicle_status makes -- they must be identical;
#   2. deserialises the captured MGS6 response the way the library does
#      (dacite.from_dict) into the extended type.
_REAL_LIBRARY_CHECK = """
import asyncio, dacite, importlib.util, json, logging, sys, types
from pathlib import Path
root = Path(sys.argv[1])
pkg = types.ModuleType("mgs"); pkg.__path__ = [str(root / "custom_components/mg_saic")]
sys.modules["mgs"] = pkg
const = types.ModuleType("mgs.const"); const.LOGGER = logging.getLogger("t")
sys.modules["mgs.const"] = const
spec = importlib.util.spec_from_file_location(
    "mgs.status_schema", root / "custom_components/mg_saic/status_schema.py")
mod = importlib.util.module_from_spec(spec); sys.modules["mgs.status_schema"] = mod
spec.loader.exec_module(mod)

from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration
api = SaicApi(SaicApiConfiguration(username="offline@example.com", password="x"))
calls = []
async def capture(method, path, **kw):  # replaces the network call
    calls.append({"method": method, "path": path, "params": kw.get("params"),
                  "out_type": kw["out_type"].__name__})
    return dacite.from_dict(kw["out_type"], json.loads(sys.argv[2]))
api.execute_api_call_with_event_id = capture
library = asyncio.run(api.get_vehicle_status("OFFLINE0000000000"))
ours = asyncio.run(mod.fetch_vehicle_status(api, "OFFLINE0000000000"))
b = ours.basicVehicleStatus
print(json.dumps({
    "calls": calls,
    "library_has_rear": hasattr(library.basicVehicleStatus, "secondRowLeftSeatHeatLevel"),
    "rear": [b.secondRowLeftSeatHeatLevel, b.secondRowRightSeatHeatLevel],
    "front": [b.frontLeftSeatHeatLevel, b.frontRightSeatHeatLevel],
    "lock": b.lockStatus,
    "lat": ours.gpsPosition.wayPoint.position.latitude,
    "status_time": ours.statusTime,
    "is_library_type": isinstance(ours, type(library)),
    "is_parked": b.is_parked,
}))
"""


@unittest.skipUnless(HAVE_SAIC_LIB, "mg-saic-client not installed")
class RealLibraryTests(unittest.TestCase):
    """Against the real pinned SAIC client (skipped if it isn't installed)."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                _REAL_LIBRARY_CHECK,
                str(REPO_ROOT),
                json.dumps(MGS6_STATUS_2026_09_29),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_the_library_drops_the_rear_seat_levels(self):
        # The bug this fixes: the library's own type has nowhere to put them.
        self.assertFalse(self.out["library_has_rear"])

    def test_same_request_as_the_library(self):
        library_call, our_call = self.out["calls"]
        self.assertEqual(library_call["method"], our_call["method"])
        self.assertEqual(library_call["path"], our_call["path"])
        self.assertEqual(library_call["params"], our_call["params"])
        self.assertEqual(our_call["path"], "/vehicle/status")

    def test_rear_seat_levels_are_kept(self):
        self.assertEqual(self.out["rear"], [3, 0])

    def test_everything_the_library_reads_is_unchanged(self):
        self.assertEqual(self.out["front"], [0, 0])
        self.assertEqual(self.out["lock"], 1)
        self.assertEqual(self.out["lat"], 51117513)
        self.assertEqual(self.out["status_time"], 1790714718)
        self.assertTrue(self.out["is_library_type"])
        self.assertTrue(self.out["is_parked"])


class FallbackTests(unittest.TestCase):
    """If the library's internals can't be used, keep working without rear levels."""

    def test_falls_back_to_the_library_status_call(self):
        # A client without the request method we rely on -- as a future
        # library that renamed it would be. Whether the real library or the
        # test stub is loaded, the status must still come back.
        expected = SimpleNamespace(basicVehicleStatus=SimpleNamespace(lockStatus=1))
        saic_api = SimpleNamespace(get_vehicle_status=AsyncMock(return_value=expected))

        result = asyncio.run(STATUS_SCHEMA.fetch_vehicle_status(saic_api, "VIN1"))

        self.assertIs(result, expected)
        saic_api.get_vehicle_status.assert_awaited_once_with("VIN1")


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
REAR_NAMES = {"Rear Left Heated Seat Level", "Rear Right Heated Seat Level"}
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

    def test_levels_read_from_the_car(self):
        # Whatever turned them on (HA, the app, the car's own buttons), the
        # car reports the level and the sensors show it.
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(rear_left=3, rear_right=0),
        )
        self.assertEqual(sensors["Rear Left Heated Seat Level"].native_value, "High")
        self.assertEqual(sensors["Rear Right Heated Seat Level"].native_value, "Off")

    def test_unique_ids_are_distinct_and_stable(self):
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(0, 0),
        )
        self.assertEqual(
            sensors["Rear Left Heated Seat Level"].unique_id,
            "entry-1_VIN1_secondRowLeftSeatHeatLevel_seat_heat_level",
        )
        self.assertEqual(len({s.unique_id for s in sensors.values()}), 4)

    def test_missing_field_is_not_reported_as_off(self):
        # If the library fallback is in use the field is absent: the sensor
        # must say nothing rather than claim the seat is off.
        sensors = _setup_sensors(
            heated_seats=True, rear_heated_seats=True, backend=GLOBAL,
            status=_status(),
        )
        self.assertIsNone(sensors["Rear Left Heated Seat Level"].native_value)


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
