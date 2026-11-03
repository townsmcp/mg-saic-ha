"""Estimated Range After Charging holds its plugged-in figure between charges.

Between charges the car keeps recalculating this from its range per 1% of
battery and the target SOC, so it moved with every drive and target change:

    James's MGS6, 17-18 Oct 2026 (car's own figure throughout):
      Sat 18:09  target 80% -> 100%       394 -> 508 km  (charging)
      Sun 09:44  unplugged
      Sun 14:58  target 100% -> 80%       500 -> 389 km
      Sun 17:37  after a long cold drive  377 km
    An IM6 (@mstobie) wobbled 520-591 km through a day's driving.

It now shows what it was when the car was last plugged in, with what that
figure was made of, and follows the car again when plugged back in.
"""

import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR
from test_charge_stats import ts as TRIP
from test_charge_reload import _loaded, _saved

COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
ENTRY = NS(entry_id="entry1")

UNPLUGGED = dict(bmsChrgSts=0, gun=0)
CHARGING = dict(bmsChrgSts=1, gun=1)
PLUGGED_IDLE = dict(bmsChrgSts=7, gun=1)
TARGET_100, TARGET_80 = 7, 5  # bmsOnBdChrgTrgtSOCDspCmd codes


def _coordinator(trip_stats=None):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "VIN1"
    c.vin_info = NS(vin="VIN1", brandName="MG", modelName="MGS6 EV")
    c.data = {"status": None, "charging": None}
    c.trip_stats = trip_stats or NS(range_after_charge=None)
    c._schedule_trip_save = MagicMock()
    return c


def _sensor(c):
    return SENSOR.SAICMGChargingSensor(
        c, ENTRY, "Estimated Range After Charging", "imcuChrgngEstdElecRng",
        None, None, "mdi:map-marker-distance", "measurement", 1.0,
        "chrgMgmtData", "charging",
    )


def _poll(c, *, erac, soc, rng, target, bmsChrgSts, gun):
    c.data["status"] = NS(basicVehicleStatus=NS(fuelRangeElec=rng * 10, extendedData1=soc))
    c.data["charging"] = NS(
        chrgMgmtData=NS(
            bmsChrgSts=bmsChrgSts,
            imcuChrgngEstdElecRng=erac,
            imcuChrgngEstdElecRngV=0,
            bmsPackSOCDsp=soc * 10,
            bmsOnBdChrgTrgtSOCDspCmd=target,
        ),
        rvsChargeStatus=NS(chargingGunState=gun, fuelRangeElec=rng * 10),
    )


class JamesWeekendTests(unittest.TestCase):
    def setUp(self):
        self.c = _coordinator()
        self.s = _sensor(self.c)

    def _read(self):
        return self.s.native_value, self.s.extra_state_attributes

    def test_follows_the_car_while_charging(self):
        _poll(self.c, erac=508, soc=97, rng=493, target=TARGET_100, **CHARGING)
        value, attrs = self._read()
        self.assertEqual(value, 508)
        self.assertEqual(attrs["source"], "reported")
        self.assertEqual(attrs["basis"], "plugged_in")
        self.assertEqual(attrs["target_soc_pct"], 100)
        self.assertEqual(attrs["electric_range_km"], 493)
        self.c._schedule_trip_save.assert_called()

    def test_holds_through_the_drive_and_target_change(self):
        _poll(self.c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        self._read()
        for erac, soc, rng, target in (
            (482, 99.9, 482, TARGET_100),  # unplugged
            (389, 97.9, 488, TARGET_80),   # target back to 80%
            (377, 72.4, 337, TARGET_80),   # after the long drive
        ):
            _poll(self.c, erac=erac, soc=soc, rng=rng, target=target, **UNPLUGGED)
            value, attrs = self._read()
            self.assertEqual(value, 508, erac)
            self.assertEqual(attrs["basis"], "last_charge")
            self.assertEqual(attrs["target_soc_pct"], 100)
            self.assertEqual(attrs["soc_pct"], 100)

    def test_plugging_back_in_follows_the_car_again(self):
        _poll(self.c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        self._read()
        _poll(self.c, erac=377, soc=72.4, rng=337, target=TARGET_80, **UNPLUGGED)
        self.assertEqual(self._read()[0], 508)
        _poll(self.c, erac=377, soc=72.4, rng=337, target=TARGET_80, **PLUGGED_IDLE)
        value, attrs = self._read()
        self.assertEqual(value, 377)
        self.assertEqual(attrs["basis"], "plugged_in")
        self.assertEqual(attrs["target_soc_pct"], 80)


class EdgeTests(unittest.TestCase):
    def test_first_reading_while_unplugged_is_taken_once(self):
        c = _coordinator()
        s = _sensor(c)
        _poll(c, erac=389, soc=97.9, rng=488, target=TARGET_80, **UNPLUGGED)
        self.assertEqual(s.native_value, 389)
        _poll(c, erac=377, soc=72.4, rng=337, target=TARGET_80, **UNPLUGGED)
        self.assertEqual(s.native_value, 389)
        self.assertEqual(s.extra_state_attributes["basis"], "last_charge")

    def test_kept_through_a_restart(self):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        c = _coordinator(manager)
        s = _sensor(c)
        _poll(c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        s.native_value
        reloaded = _loaded(_saved(manager))
        self.assertEqual(reloaded.range_after_charge["value_km"], 508)

        c2 = _coordinator(reloaded)
        s2 = _sensor(c2)
        _poll(c2, erac=377, soc=72.4, rng=337, target=TARGET_80, **UNPLUGGED)
        self.assertEqual(s2.native_value, 508)

    def test_no_charging_reply_keeps_what_was_shown(self):
        c = _coordinator()
        s = _sensor(c)
        _poll(c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        s.native_value
        c.data["charging"] = None
        self.assertEqual(s.native_value, 508)

    def test_unchanged_reading_is_not_saved_again(self):
        c = _coordinator()
        s = _sensor(c)
        _poll(c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        s.native_value
        s.native_value
        c._schedule_trip_save.assert_called_once()

    def test_stuck_gun_alone_is_not_plugged_in(self):
        # _cable_state needs the gun and a status other than Unplugged.
        c = _coordinator()
        s = _sensor(c)
        _poll(c, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        s.native_value
        _poll(c, erac=377, soc=72.4, rng=337, target=TARGET_80, bmsChrgSts=0, gun=1)
        self.assertEqual(s.native_value, 508)


def _india_poll(c, stamp, **kwargs):
    _poll(c, **kwargs)
    c.client = NS(_india_backend=True)
    basic = c.data["status"].basicVehicleStatus
    basic.india = basic.battery_soc_available = True
    basic.measurement_time = stamp
    frame = c.data["charging"]
    frame.india = True
    frame.chrgMgmtData.measurement_time = stamp
    frame.rvsChargeStatus.measurement_time = stamp


class IndiaRangeHoldTests(unittest.TestCase):
    def test_restart_without_charging_frame_restores_saved_range(self):
        c = _coordinator(TRIP.TripStatsManager(MagicMock(), "entry", "VIN", india=True))
        s = _sensor(c)
        _india_poll(c, 200, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        self.assertEqual(s.native_value, 508)
        snapshot = dict(c.trip_stats.range_after_charge)

        c2 = _coordinator(_loaded(_saved(c.trip_stats)))
        s2 = _sensor(c2)
        _india_poll(c2, 300, erac=377, soc=70, rng=337, target=TARGET_80, **UNPLUGGED)
        c2.data["charging"] = None
        c2.last_update_success = True
        self.assertEqual(s2.native_value, 508)
        attrs = s2.extra_state_attributes
        self.assertEqual(attrs["source"], "reported")
        self.assertEqual(attrs["measurement_time"], 200)
        self.assertEqual(attrs["india_provenance"], snapshot["india_provenance"])
        self.assertEqual(attrs["basis"], "last_charge")
        for key in ("electric_range_km", "soc_pct", "target_soc_pct", "as_of"):
            self.assertEqual(attrs[key], snapshot[key])
        self.assertEqual(c2.trip_stats.range_after_charge, snapshot)
        c2._schedule_trip_save.assert_not_called()

    def test_reported_source_persists_and_replays_do_not_save(self):
        c = _coordinator(TRIP.TripStatsManager(MagicMock(), "entry", "VIN", india=True))
        s = _sensor(c)
        _india_poll(c, 200, erac=508, soc=100, rng=508, target=TARGET_100, **PLUGGED_IDLE)
        self.assertEqual(s.native_value, 508)
        snapshot = dict(c.trip_stats.range_after_charge)
        self.assertEqual(snapshot["india_provenance"]["measurement_time"], 200)
        c._schedule_trip_save.reset_mock()
        for stamp in (200, 100):
            _india_poll(c, stamp, erac=400, soc=90, rng=400, target=TARGET_80, **PLUGGED_IDLE)
            self.assertEqual(s.native_value, 508)
            self.assertEqual(c.trip_stats.range_after_charge, snapshot)
        c._schedule_trip_save.assert_not_called()
        saved = _saved(c.trip_stats)
        c2 = _coordinator(_loaded(saved))
        s2 = _sensor(c2)
        _india_poll(c2, 300, erac=377, soc=70, rng=337, target=TARGET_80, **UNPLUGGED)
        self.assertEqual(s2.native_value, 508)
        attrs = s2.extra_state_attributes
        self.assertEqual(attrs["measurement_time"], 200)
        self.assertEqual(attrs["india_provenance"], snapshot["india_provenance"])
        self.assertEqual(attrs["basis"], "last_charge")
        self.assertEqual(attrs["soc_pct"], 100)
        _india_poll(c2, 100, erac=400, soc=90, rng=400, target=TARGET_80, **PLUGGED_IDLE)
        self.assertEqual(s2.native_value, 508)
        self.assertEqual(c2.trip_stats.range_after_charge, snapshot)

    def test_estimated_range_rejects_mixed_source_regression(self):
        c = _coordinator()
        s = _sensor(c)
        _india_poll(c, 200, erac=0, soc=50, rng=250, target=TARGET_100, **CHARGING)
        self.assertEqual(s.native_value, 500)
        snapshot = dict(c.trip_stats.range_after_charge)
        origin = snapshot["india_provenance"]
        self.assertEqual(origin["inputs"]["soc"]["measurement_time"], 200)
        c._schedule_trip_save.reset_mock()
        _india_poll(c, 300, erac=0, soc=60, rng=300, target=TARGET_80, **CHARGING)
        c.data["status"].basicVehicleStatus.fuelRangeElec = None
        c.data["charging"].rvsChargeStatus.measurement_time = 100
        self.assertEqual(s.native_value, 500)
        self.assertEqual(c.trip_stats.range_after_charge, snapshot)
        c._schedule_trip_save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
