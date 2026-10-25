"""A charge closed long after it ended, once the charging data came back.

@HarryFlatter's HS PHEV, 13-15 Oct 2026 (times BST):

    13 Oct 23:25  plugged in, 6.1%
    14 Oct 00:36  charging starts (the car's record); 00:37 live poll
    14 Oct 04:03  charge ends (the car's record)
    14 Oct 04:08  live poll: status says 100%, not charging; the charging
                  request fails (return code 4) -- and keeps failing
    14 Oct        driven
    15 Oct 01:11  the charging data comes back: 91.8%, not charging

The charge could only be closed at 01:11 on the 15th, and was recorded as
6.1% to 91.8% (85.7%, 19.9 kWh) instead of 6.1% to 100% (93.9%, about
21.8 kWh). The times were right: they come from the car's record.
"""

import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charge_stats import CSnap, ts as TRIP
from test_charge_reload import _loaded, _saved

COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
LOGIC = sys.modules["mg_saic.logic"]

UTC = timezone.utc
CAPACITY = 23.2


def _at(day, h, m, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def _epoch(day, h, m, s=0):
    return _at(day, h, m, s).timestamp()


def _snap(at, soc, odo, start=None, end=None, range_km=None):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=odo, range_km=range_km,
                 record_start=start, record_end=end)


# Times UTC (BST - 1). The record before is the previous charge's.
PREV_START, PREV_END = _epoch(12, 19, 0), _epoch(12, 21, 0)
REC_START, REC_END = _epoch(13, 23, 36, 49), _epoch(14, 3, 3, 29)

PLUGGED = _snap(_at(13, 22, 25, 19), 6.1, 6510.0, PREV_START, PREV_END, 6.0)
CHARGING = _snap(_at(13, 23, 37, 15), 6.3, 6510.0, REC_START, PREV_END, 6.0)
STOPPED = _snap(_at(14, 3, 8, 31), 100.0, 6510.0, range_km=118.0)
RETURNED = _snap(_at(15, 0, 11, 9), 91.8, 6531.0, REC_START, REC_END, 110.0)


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


def _note(manager, charging, snap, *, plugged=True):
    return manager.note_charge_state(
        charging, snap, capacity_kwh=CAPACITY, now_iso=snap.ts,
        is_plugged_in=plugged,
    )


def _charge_to_0408(manager):
    _note(manager, False, PLUGGED)
    _note(manager, True, CHARGING)
    return manager.note_charge_stopped(STOPPED)


class HarrysChargeTests(unittest.TestCase):
    def test_closed_at_100_percent(self):
        manager = _manager()
        self.assertTrue(_charge_to_0408(manager))
        charge, _ = _note(manager, False, RETURNED, plugged=False)
        self.assertEqual(charge["soc_start_pct"], 6.1)
        self.assertEqual(charge["soc_end_pct"], 100.0)
        self.assertEqual(charge["soc_added_pct"], 93.9)
        self.assertEqual(charge["energy_added_kWh"], 21.785)
        self.assertEqual(charge["range_end_km"], 118.0)

    def test_times_still_from_the_cars_record(self):
        manager = _manager()
        _charge_to_0408(manager)
        charge, _ = _note(manager, False, RETURNED, plugged=False)
        self.assertEqual(charge["duration_source"], "car")
        self.assertEqual(charge["duration_s"], 12400)
        self.assertEqual(charge["charge_end_ts"], _at(14, 3, 3, 29).isoformat())
        # HA's own window ends at the 04:08 reading, not the next night.
        self.assertEqual(charge["end_ts"], STOPPED.ts)

    def test_before_the_fix(self):
        manager = _manager()
        _note(manager, False, PLUGGED)
        _note(manager, True, CHARGING)
        charge, _ = _note(manager, False, RETURNED, plugged=False)
        self.assertEqual(charge["soc_end_pct"], 91.8)

    def test_survives_a_restart(self):
        manager = _manager()
        _charge_to_0408(manager)
        reloaded = _loaded(_saved(manager))
        self.assertEqual(reloaded.charge_stopped_snapshot, STOPPED)
        charge, _ = _note(reloaded, False, RETURNED, plugged=False)
        self.assertEqual(charge["soc_end_pct"], 100.0)


class WhenToUseItTests(unittest.TestCase):
    def test_not_moved_on_the_later_reading_stands(self):
        # Charging data back the same morning, car not driven.
        manager = _manager()
        _charge_to_0408(manager)
        same = _snap(_at(14, 8, 0), 100.0, 6510.0, REC_START, REC_END, 118.0)
        charge, _ = _note(manager, False, same)
        self.assertEqual(charge["end_ts"], same.ts)
        self.assertEqual(charge["soc_end_pct"], 100.0)

    def test_a_later_charges_record_is_not_used(self):
        # Charged again during the day: the record is that charge's.
        manager = _manager()
        _charge_to_0408(manager)
        later = _snap(_at(15, 0, 11, 9), 91.8, 6531.0,
                      _epoch(14, 12, 0), _epoch(14, 13, 0), 110.0)
        charge, _ = _note(manager, False, later, plugged=False)
        self.assertEqual(charge["soc_end_pct"], 100.0)
        self.assertEqual(charge["duration_source"], "polls")
        self.assertNotIn("charge_end_ts", charge)

    def test_charging_again_forgets_the_stop(self):
        manager = _manager()
        _charge_to_0408(manager)
        _note(manager, True, _snap(_at(14, 3, 30), 99.0, 6510.0, REC_START, PREV_END))
        self.assertIsNone(manager.charge_stopped_snapshot)

    def test_only_the_first_stop_is_kept(self):
        manager = _manager()
        _charge_to_0408(manager)
        self.assertFalse(manager.note_charge_stopped(_snap(_at(14, 5, 0), 99.0, 6510.0)))
        self.assertIs(manager.charge_stopped_snapshot, STOPPED)

    def test_nothing_without_an_open_charge(self):
        self.assertFalse(_manager().note_charge_stopped(STOPPED))

    def test_cleared_when_the_charge_closes(self):
        manager = _manager()
        _charge_to_0408(manager)
        _note(manager, False, RETURNED, plugged=False)
        self.assertIsNone(manager.charge_stopped_snapshot)


class CoordinatorTests(unittest.TestCase):
    def _coordinator(self, *, trusted=True, open_charge=True):
        c = COORD_CLS.__new__(COORD_CLS)
        c.vin = "TESTVIN"
        c.vehicle_type = "PHEV"
        c._charging_flag_seen = trusted
        c.trip_stats = MagicMock()
        c.trip_stats.open_charge = CHARGING if open_charge else None
        c.trip_stats.note_charge_stopped = MagicMock(return_value=True)
        c._schedule_trip_save = MagicMock()
        return c

    @staticmethod
    def _basic(flag=0):
        # The 04:08 status: 100%, not charging.
        return NS(extendedData1=100, extendedData2=flag, mileage=65100)

    def test_the_0408_reading_is_noted(self):
        c = self._coordinator()
        c._update_charge_state(self._basic(), None)
        snap = c.trip_stats.note_charge_stopped.call_args.args[0]
        self.assertEqual(snap.soc_pct, 100.0)
        c._schedule_trip_save.assert_called_once()

    def test_not_while_the_status_says_charging(self):
        c = self._coordinator()
        c._update_charge_state(self._basic(flag=1), None)
        c.trip_stats.note_charge_stopped.assert_not_called()

    def test_not_before_the_flag_is_trusted(self):
        c = self._coordinator(trusted=False)
        c._update_charge_state(self._basic(), None)
        c.trip_stats.note_charge_stopped.assert_not_called()

    def test_not_without_an_open_charge(self):
        c = self._coordinator(open_charge=False)
        c._update_charge_state(self._basic(), None)
        c.trip_stats.note_charge_stopped.assert_not_called()

    def test_helper(self):
        self.assertTrue(LOGIC.basic_status_says_not_charging(self._basic(), flag_trusted=True))
        self.assertFalse(LOGIC.basic_status_says_not_charging(self._basic(), flag_trusted=False))
        self.assertFalse(LOGIC.basic_status_says_not_charging(None, flag_trusted=True))


if __name__ == "__main__":
    unittest.main()
