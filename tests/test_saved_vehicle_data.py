"""The car's last status and charging data, saved across restarts.

James's MGS6, 15 Oct 2026: Home Assistant restarted at 12:43 while the car
wasn't answering (the iSmart app said "Vehicle not responding" too). With
nothing about the car in memory, the lock, climate, doors, windows, battery
level, range and mileage stayed unavailable until the car answered.
"""

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow
from test_charge_reload import _loaded, _saved
from test_charge_stats import ts as TRIP

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
SAVED = test_setup_and_config_flow._load(
    "mg_saic.saved_data", test_setup_and_config_flow.PKG_DIR / "saved_data.py"
)

try:
    import dacite  # noqa: F401 - comes with the SAIC client

    HAVE_CLIENT = True
except ImportError:
    HAVE_CLIENT = False


# Stand-ins shaped like the SAIC client's replies (the client itself is
# stubbed in these tests).
@dataclass
class Position:
    latitude: int | None = None
    longitude: int | None = None


@dataclass
class WayPoint:
    position: Position | None = None


@dataclass
class Gps:
    gpsStatus: int | None = None
    wayPoint: WayPoint | None = None


@dataclass
class Basic:
    lockStatus: int | None = None
    extendedData1: int | None = None
    mileage: int | None = None


@dataclass
class StatusResp:
    basicVehicleStatus: Basic | None = None
    gpsPosition: Gps | None = None
    statusTime: int | None = None


@dataclass
class ChrgMgmt:
    bmsChrgSts: int | None = None
    bmsPackSOCDsp: int | None = None


@dataclass
class RvsCharge:
    chargingGunState: int | None = None


@dataclass
class ChargingResp:
    chrgMgmtData: ChrgMgmt | None = None
    rvsChargeStatus: RvsCharge | None = None


def _client_replies():
    status = StatusResp(
        basicVehicleStatus=Basic(lockStatus=1, extendedData1=77, mileage=55000),
        gpsPosition=Gps(gpsStatus=2, wayPoint=WayPoint(Position(1, 2))),
        statusTime=1792048000,
    )
    charging = ChargingResp(ChrgMgmt(bmsChrgSts=0, bmsPackSOCDsp=770), RvsCharge(0))
    return status, charging


@unittest.skipUnless(HAVE_CLIENT, "needs dacite (installed with the SAIC client)")
class RoundTripTests(unittest.TestCase):
    def test_status_and_charging_come_back_the_same(self):
        status, charging = _client_replies()
        for reply in (status, charging):
            self.assertEqual(SAVED.decode_vehicle_data(SAVED.encode_vehicle_data(reply)), reply)

    def test_survives_the_store(self):
        status, charging = _client_replies()
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        manager.saved_vehicle_data = {
            "status": SAVED.encode_vehicle_data(status),
            "charging": SAVED.encode_vehicle_data(charging),
            "saved_at": "2026-10-15T07:15:52+00:00",
        }
        reloaded = _loaded(_saved(manager))
        self.assertEqual(
            SAVED.decode_vehicle_data(reloaded.saved_vehicle_data["status"]), status
        )


class BadSavesTests(unittest.TestCase):
    def test_nothing_usable(self):
        for saved in (None, {}, {"type": "x"}, {"type": "nomodule:Thing", "data": {}},
                      {"type": "json:loads", "data": {}}, "junk"):
            self.assertIsNone(SAVED.decode_vehicle_data(saved), saved)

    def test_only_dataclasses_are_saved(self):
        self.assertIsNone(SAVED.encode_vehicle_data(None))
        self.assertIsNone(SAVED.encode_vehicle_data(NS(a=1)))


def _coordinator():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.trip_stats = NS(saved_vehicle_data=None)
    c._schedule_trip_save = MagicMock()
    c.last_status_data = None
    c.last_charging_data = None
    return c


@unittest.skipUnless(HAVE_CLIENT, "needs dacite (installed with the SAIC client)")
class CoordinatorTests(unittest.TestCase):
    def test_a_good_poll_is_saved(self):
        status, charging = _client_replies()
        c = _coordinator()
        c._save_vehicle_data(status, charging)
        saved = c.trip_stats.saved_vehicle_data
        self.assertEqual(SAVED.decode_vehicle_data(saved["status"]), status)
        self.assertEqual(SAVED.decode_vehicle_data(saved["charging"]), charging)
        c._schedule_trip_save.assert_called_once()

    def test_unchanged_is_not_saved_again(self):
        status, charging = _client_replies()
        c = _coordinator()
        c._save_vehicle_data(status, charging)
        c._save_vehicle_data(status, charging)
        c._schedule_trip_save.assert_called_once()

    def test_missing_charging_keeps_the_saved_charging(self):
        status, charging = _client_replies()
        c = _coordinator()
        c._save_vehicle_data(status, charging)
        c._save_vehicle_data(status, None)
        saved = c.trip_stats.saved_vehicle_data
        self.assertEqual(SAVED.decode_vehicle_data(saved["charging"]), charging)

    def test_restart_with_the_car_not_answering(self):
        status, charging = _client_replies()
        before = _coordinator()
        before._save_vehicle_data(status, charging)

        after = _coordinator()
        after.trip_stats = NS(saved_vehicle_data=before.trip_stats.saved_vehicle_data)
        after._restore_saved_vehicle_data()
        # The controls' fallback (beta10) has them straight away.
        self.assertEqual(after.last_status_data, status)
        self.assertEqual(after.last_charging_data, charging)
        # The startup poll got nothing: the entities get the saved replies.
        data = {"status": None, "charging": None}
        after._show_saved_vehicle_data(data)
        self.assertEqual(data["status"], status)
        self.assertEqual(data["charging"], charging)

    def test_a_car_that_answers_is_shown_as_it_answered(self):
        status, charging = _client_replies()
        c = _coordinator()
        c._save_vehicle_data(status, charging)
        c._restore_saved_vehicle_data()
        live = NS(basicVehicleStatus=NS(lockStatus=0))
        data = {"status": live, "charging": None}
        c._show_saved_vehicle_data(data)
        self.assertIs(data["status"], live)
        # Charging didn't answer: its saved reply fills in.
        self.assertEqual(data["charging"], charging)

    def test_nothing_saved_nothing_shown(self):
        c = _coordinator()
        c._restore_saved_vehicle_data()
        data = {"status": None, "charging": None}
        c._show_saved_vehicle_data(data)
        self.assertIsNone(data["status"])


class WiringTests(unittest.TestCase):
    def _source(self):
        return (
            Path(COORD_MOD.__file__).read_text()
        )

    def test_only_at_startup(self):
        source = self._source()
        self.assertIn(
            "        if self.is_initial_setup:\n            self._show_saved_vehicle_data(data)",
            source,
        )

    def test_shown_after_the_state_update(self):
        # So the saved reply is never taken as a new reading (trips, charges).
        source = self._source()
        cycle = source.split("async def _run_update_cycle(self):", 1)[1]
        self.assertLess(
            cycle.index("self._update_state(data)"),
            cycle.index("self._show_saved_vehicle_data(data)"),
        )

    def test_saved_on_every_status(self):
        block = self._source().split("def _update_state(self, data):", 1)[1][:700]
        self.assertIn("self._save_vehicle_data(status_data, charging_data)", block)


if __name__ == "__main__":
    unittest.main()
