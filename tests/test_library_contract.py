"""What the integration now relies on mg-saic-client (0.9.5+) for.

Per-seat heated seats, the heated steering wheel, the door windows and
message times used to be
built or worked around inside the integration. They now come from the
library, so these tests check both sides of the hand-over:

- the integration calls the library the right way (in this process, where
  the shared harness stubs the library);
- the real pinned library does what the integration expects (in a clean
  subprocess, skipped if the installed library isn't the pinned version).
"""

import asyncio
import enum
import json
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

API = sys.modules["mg_saic.api"]
REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED = next(
    r
    for r in json.loads(
        (REPO_ROOT / "custom_components/mg_saic/manifest.json").read_text()
    )["requirements"]
    if r.startswith("mg-saic-client")
)


class _DoorWindowsAction(enum.Enum):  # the library's values, for the stub
    CLOSE = 0
    VENTILATE = 1
    OPEN = 2


class _HeatedSeat(enum.Enum):  # the library's names, for the stub
    FRONT_LEFT = 17
    FRONT_RIGHT = 18
    REAR_LEFT = 25
    REAR_RIGHT = 26


def _client():
    client = API.SAICMGAPIClient("user@example.com", "hunter2")
    client.saic_api = MagicMock(is_logged_in=True)
    return client


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class HeatedSeatTests(unittest.TestCase):
    def setUp(self):
        climate = types.ModuleType("saic_ismart_client_ng.api.vehicle.climate")
        climate.HeatedSeat = _HeatedSeat
        self._modules = patch.dict(
            sys.modules, {"saic_ismart_client_ng.api.vehicle.climate": climate}
        )
        self._modules.start()
        self.addCleanup(self._modules.stop)

    def test_each_seat_goes_through_the_library(self):
        for seat, expected, level in (
            ("front_left", _HeatedSeat.FRONT_LEFT, 2),
            ("front_right", _HeatedSeat.FRONT_RIGHT, 1),
            ("rear_left", _HeatedSeat.REAR_LEFT, 3),
            ("rear_right", _HeatedSeat.REAR_RIGHT, 0),
        ):
            client = _client()
            client.saic_api.control_heated_seat = AsyncMock()
            _run(client.control_heated_seat("VIN1", seat, level))
            client.saic_api.control_heated_seat.assert_awaited_once_with(
                "VIN1", seat=expected, level=level
            )

    def test_unknown_seat_sends_nothing(self):
        client = _client()
        client.saic_api.control_heated_seat = AsyncMock()
        with self.assertRaises(ValueError):
            _run(client.control_heated_seat("VIN1", "middle_rear", 1))
        client.saic_api.control_heated_seat.assert_not_awaited()


class SteeringWheelTests(unittest.TestCase):
    def test_on_and_off_go_through_the_library(self):
        for enable in (True, False):
            client = _client()
            client.saic_api.control_heated_steering_wheel = AsyncMock()
            _run(client.control_steering_wheel_heat("VIN1", enable))
            client.saic_api.control_heated_steering_wheel.assert_awaited_once_with(
                "VIN1", enable=enable
            )


class DoorWindowsTests(unittest.TestCase):
    def setUp(self):
        windows = types.ModuleType("saic_ismart_client_ng.api.vehicle.windows")
        windows.DoorWindowsAction = _DoorWindowsAction
        self._modules = patch.dict(
            sys.modules, {"saic_ismart_client_ng.api.vehicle.windows": windows}
        )
        self._modules.start()
        self.addCleanup(self._modules.stop)

    def test_each_action_maps_to_the_library_value(self):
        for action, expected in (
            ("close", _DoorWindowsAction.CLOSE),
            ("ventilate", _DoorWindowsAction.VENTILATE),
            ("open", _DoorWindowsAction.OPEN),
            ("OPEN", _DoorWindowsAction.OPEN),
        ):
            client = _client()
            client.saic_api.control_door_windows = AsyncMock()
            _run(client.control_windows("VIN1", action))
            client.saic_api.control_door_windows.assert_awaited_once_with(
                "VIN1", action=expected
            )

    def test_unknown_action_sends_nothing(self):
        client = _client()
        client.saic_api.control_door_windows = AsyncMock()
        with self.assertRaises(ValueError):
            _run(client.control_windows("VIN1", "half"))
        client.saic_api.control_door_windows.assert_not_awaited()


_REAL_LIBRARY = """
import asyncio, base64, datetime, json
from importlib.metadata import version
from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.api.message.schema import MessageEntity
from saic_ismart_client_ng.api.vehicle.climate import HeatedSeat, REAR_HEATED_SEAT_ON_LEVEL
from saic_ismart_client_ng.api.vehicle.schema import BasicVehicleStatus
from saic_ismart_client_ng.api.vehicle.windows import DoorWindowsAction
from saic_ismart_client_ng.model import SaicApiConfiguration

api = SaicApi(SaicApiConfiguration(username="offline@example.com", password="x"))
sent = []
async def capture(body, vin):
    sent.append([body.rvcReqType,
                 [[p.paramId, list(base64.b64decode(p.paramValue))] for p in body.rvcParams]])
api.send_vehicle_control_command = capture
asyncio.run(api.control_heated_steering_wheel("OFFLINE0000000000", enable=True))
asyncio.run(api.control_heated_steering_wheel("OFFLINE0000000000", enable=False))
for action in ("CLOSE", "VENTILATE", "OPEN"):
    asyncio.run(api.control_door_windows(
        "OFFLINE0000000000", action=DoorWindowsAction[action]))

for seat, level in (("FRONT_LEFT", 2), ("FRONT_RIGHT", 1),
                    ("REAR_LEFT", REAR_HEATED_SEAT_ON_LEVEL), ("REAR_RIGHT", 0)):
    asyncio.run(api.control_heated_seat(
        "OFFLINE0000000000", seat=HeatedSeat[seat], level=level))

dated = MessageEntity(messageTime="2026-09-25 18:57:38", createTime=1790359058000)
undated = MessageEntity()
print(json.dumps({
    "version": version("mg-saic-client"),
    "sent": sent,
    "actions": {a.name: a.value for a in DoorWindowsAction},
    "dated": [str(dated.message_time_or_none), str(dated.create_time_utc)],
    "undated": [undated.message_time_or_none, undated.create_time_utc],
    "seat_names": [s.name for s in HeatedSeat],
    "status_fields": sorted(BasicVehicleStatus.__dataclass_fields__),
}))
"""


class RealLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [sys.executable, "-c", _REAL_LIBRARY],
            capture_output=True, text=True, timeout=60,
        )
        cls.out = json.loads(result.stdout) if result.returncode == 0 else None
        cls.err = result.stderr

    def setUp(self):
        if self.out is None:
            if "No module named 'saic_ismart_client_ng'" in self.err:
                self.skipTest("mg-saic-client not installed")
            self.fail(f"library check failed (older than 0.9.5?):\n{self.err}")
        if f"mg-saic-client=={self.out['version']}" != PINNED:
            self.skipTest(f"installed {self.out['version']}, manifest pins {PINNED}")

    def test_steering_wheel_command_matches_the_app(self):
        # Exactly what the integration used to build itself: type 8, param 24.
        self.assertEqual(self.out["sent"][0], ["8", [[24, [1]]]])
        self.assertEqual(self.out["sent"][1], ["8", [[24, [0]]]])

    def test_door_window_commands_match_the_app(self):
        for sent, value in zip(self.out["sent"][2:], (0, 1, 2)):
            self.assertEqual(
                sent,
                ["3", [[8, [0]], [9, [1]], [10, [1]], [11, [1]], [12, [1]], [13, [value]]]],
            )
        self.assertEqual(self.out["actions"], {"CLOSE": 0, "VENTILATE": 1, "OPEN": 2})

    def test_heated_seat_commands_match_the_app(self):
        # One parameter per seat, as the integration used to build itself.
        self.assertEqual(
            self.out["sent"][5:9],
            [["5", [[17, [2]]]], ["5", [[18, [1]]]], ["5", [[25, [3]]]], ["5", [[26, [0]]]]],
        )

    def test_integration_seat_keys_match_the_library(self):
        # api.control_heated_seat looks seats up by name ("rear_left" ->
        # REAR_LEFT), so the names must line up.
        self.assertEqual(
            self.out["seat_names"],
            ["FRONT_LEFT", "FRONT_RIGHT", "REAR_LEFT", "REAR_RIGHT"],
        )

    def test_status_fields_the_integration_reads(self):
        fields = set(self.out["status_fields"])
        for name in (
            "secondRowLeftSeatHeatLevel",
            "secondRowRightSeatHeatLevel",
            "rearLeftOSTyrePressure",
            "rearRightOSTyrePressure",
            "elecRangeStdA",
            "elecRangeStdB",
            "elecRangeDspMode",
        ):
            self.assertIn(name, fields)

    def test_message_times(self):
        self.assertEqual(
            self.out["dated"],
            ["2026-09-25 18:57:38", "2026-09-25 17:57:38+00:00"],
        )
        # No time means None -- not "now", which made undated messages look new.
        self.assertEqual(self.out["undated"], [None, None])


if __name__ == "__main__":
    unittest.main()
