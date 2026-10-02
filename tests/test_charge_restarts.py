"""A charge that pauses and restarts is still one charge (#262).

@HarryFlatter's HS PHEV, 2 Oct 2026 (times BST; the car's own schedule is
00:30-05:30). The car restarted charging twice, and each time it restarted
its record -- startTime, and the Charging Duration counter with it:

    00:04:25  plugged in, waiting for the schedule (62.7 %)
    00:36:43  charging starts
    01:37:44  stops ... 01:38:22 restarts   (38 s)
    01:54:45  stops ... 01:55:21 restarts   (36 s)
    02:21:33  ends at 100 %
    02:50:49  HA's next poll sees it has finished

Only the last stretch was left in the record at the end, so Last Charge
showed 26 minutes at 19.82 kW. The charge took 1 h 44 min 50 s, less 74 s of
pauses, at about 5 kW.

The figures below are the car's, from the log.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from test_charge_stats import CSnap, ts as TRIP

UTC = timezone.utc
CAPACITY = 23.2

PREVIOUS_END = 1790670404      # 29 Sep 08:26:44 UTC, the charge before
STRETCH_1 = 1790897803         # 1 Oct 23:36:43 UTC
STRETCH_1_END = 1790901464     # 2 Oct 00:37:44
STRETCH_2 = 1790901502         # 00:38:22
STRETCH_2_END = 1790902485     # 00:54:45
STRETCH_3 = 1790902521         # 00:55:21
CHARGE_END = 1790904093        # 01:21:33


def _at(day, h, m, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def _snap(at, soc, start, end):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=6174.0, range_km=None,
                 record_start=start, record_end=end)


# (poll time UTC, charging?, SOC, the car's startTime, endTime)
HARRYS_POLLS = [
    (_at(1, 23, 4, 25), False, 62.7, PREVIOUS_END - 1700, PREVIOUS_END),
    (_at(1, 23, 49, 0), True, 68.7, STRETCH_1, PREVIOUS_END),
    (_at(2, 0, 19, 45), True, 83.6, STRETCH_1, PREVIOUS_END),
    (_at(2, 0, 50, 30), True, 93.1, STRETCH_2, STRETCH_1_END),
    (_at(2, 1, 21, 17), True, 99.8, STRETCH_3, STRETCH_2_END),
    (_at(2, 1, 50, 49), False, 100.0, STRETCH_3, CHARGE_END),
]


def _replay(polls, manager=None):
    manager = manager or TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
    charge = None
    for at, charging, soc, start, end in polls:
        charge, _changed = manager.note_charge_state(
            charging, _snap(at, soc, start, end),
            capacity_kwh=CAPACITY, now_iso=at.isoformat(), is_plugged_in=True,
        )
    return charge, manager


class HarrysChargeTests(unittest.TestCase):
    def setUp(self):
        self.charge, self.manager = _replay(HARRYS_POLLS)

    def test_the_charge_starts_when_the_car_started_charging(self):
        self.assertEqual(self.charge["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(self.charge["charge_end_ts"], _at(2, 1, 21, 33).isoformat())

    def test_duration_is_the_whole_charge_less_the_pauses(self):
        self.assertEqual(self.charge["interruptions"], 2)
        self.assertEqual(self.charge["paused_s"], 38 + 36)
        # 23:36:43 -> 01:21:33 is 1 h 44 min 50 s = 6290 s.
        self.assertEqual(self.charge["duration_s"], 6290 - 74)
        self.assertEqual(self.charge["duration_source"], "car")

    def test_average_power_is_believable(self):
        # It showed 19.82 kW; this car's charger can't do much over 7.
        self.assertAlmostEqual(self.charge["energy_added_kWh"], 8.654, places=3)
        self.assertAlmostEqual(self.charge["average_power_kW"], 5.01, places=2)

    def test_the_readings_window_is_unchanged(self):
        # start_ts is the last reading BEFORE charging (the energy baseline,
        # 62.7 %), not when charging began. The trip statistics rely on it.
        self.assertEqual(self.charge["start_ts"], _at(1, 23, 4, 25).isoformat())
        self.assertEqual(self.charge["end_ts"], _at(2, 1, 50, 49).isoformat())
        self.assertEqual(self.charge["soc_start_pct"], 62.7)

    def test_nothing_is_left_over_for_the_next_charge(self):
        self.assertIsNone(self.manager.open_charge)
        self.assertIsNone(self.manager.open_charge_record)


class OtherShapesTests(unittest.TestCase):
    def test_a_charge_with_no_restarts_is_as_before(self):
        polls = [
            (_at(1, 23, 4, 25), False, 62.7, PREVIOUS_END - 1700, PREVIOUS_END),
            (_at(1, 23, 49, 0), True, 68.7, STRETCH_1, PREVIOUS_END),
            (_at(2, 1, 50, 49), False, 100.0, STRETCH_1, CHARGE_END),
        ]
        charge, _ = _replay(polls)
        self.assertEqual(charge["duration_s"], CHARGE_END - STRETCH_1)
        self.assertEqual(charge["duration_source"], "car")
        self.assertNotIn("interruptions", charge)
        self.assertNotIn("paused_s", charge)

    def test_a_restart_after_the_last_charging_poll(self):
        # The final record starts later than anything seen while charging:
        # one more interruption, of unknown length.
        polls = HARRYS_POLLS[:4] + [HARRYS_POLLS[5]]
        charge, _ = _replay(polls)
        self.assertEqual(charge["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(charge["interruptions"], 2)
        self.assertEqual(charge["paused_s"], 38)
        self.assertEqual(charge["duration_s"], 6290 - 38)

    def test_a_whole_stretch_between_two_polls(self):
        # Stretch 2 began and ended between polls: its own pause can't be
        # seen, but the start, the end and one interruption still can.
        polls = HARRYS_POLLS[:3] + HARRYS_POLLS[4:]
        charge, _ = _replay(polls)
        self.assertEqual(charge["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(charge["interruptions"], 1)
        self.assertEqual(charge["paused_s"], 36)

    def test_a_stretch_missed_before_the_first_charging_poll(self):
        # The record's end had already moved on between the last idle reading
        # and the first charging one: the real start was never seen. Say so,
        # and don't work out a power from a duration that is too short.
        polls = [HARRYS_POLLS[0]] + HARRYS_POLLS[3:]
        charge, _ = _replay(polls)
        self.assertEqual(charge["duration_source"], "car_partial")
        self.assertNotIn("average_power_kW", charge)
        self.assertAlmostEqual(charge["energy_added_kWh"], 8.654, places=3)

    def test_no_reading_before_charging(self):
        # Plugged in and charging before HA first looked: the first charging
        # reading is the baseline, and the car's start is earlier than it.
        charge, _ = _replay(HARRYS_POLLS[1:])
        self.assertEqual(charge["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(charge["duration_s"], 6290 - 74)
        self.assertEqual(charge["duration_source"], "car")

    def test_a_car_that_reports_no_record_falls_back_to_polls(self):
        polls = [(at, c, soc, None, None) for at, c, soc, _s, _e in HARRYS_POLLS]
        charge, _ = _replay(polls)
        self.assertEqual(charge["duration_source"], "polls")
        self.assertNotIn("charge_start_ts", charge)


class TrackerTests(unittest.TestCase):
    def test_a_finished_record_is_not_a_stretch_in_progress(self):
        tracker, changed = TRIP.track_charge_record(
            None, _snap(_at(2, 2, 0), 100.0, STRETCH_3, CHARGE_END)
        )
        self.assertIsNone(tracker)
        self.assertFalse(changed)

    def test_the_same_stretch_again_changes_nothing(self):
        snap = _snap(_at(1, 23, 49), 68.7, STRETCH_1, PREVIOUS_END)
        tracker, _ = TRIP.track_charge_record(None, snap)
        again, changed = TRIP.track_charge_record(tracker, snap)
        self.assertFalse(changed)
        self.assertEqual(again, tracker)

    def test_a_start_that_goes_backwards_is_ignored(self):
        tracker, _ = TRIP.track_charge_record(
            None, _snap(_at(2, 0, 50), 93.1, STRETCH_2, STRETCH_1_END)
        )
        again, changed = TRIP.track_charge_record(
            tracker, _snap(_at(2, 0, 51), 93.2, STRETCH_1, PREVIOUS_END)
        )
        self.assertFalse(changed)
        self.assertEqual(again["first_start"], STRETCH_2)


class StorageTests(unittest.TestCase):
    """A restart of Home Assistant mid-charge must not lose the real start."""

    def test_the_followed_record_survives_a_restart(self):
        import asyncio

        saved = {}
        _, manager = _replay(HARRYS_POLLS[:4])

        class _Store:
            async def async_save(self, data):
                saved.update(data)

            async def async_load(self):
                return dict(saved)

        manager._store = _Store()
        asyncio.run(manager.async_save())
        self.assertEqual(saved["open_charge_record"]["first_start"], STRETCH_1)
        self.assertEqual(saved["open_charge_record"]["interruptions"], 1)

        reloaded = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        reloaded.open_charge = CSnap.from_dict(saved["open_charge"])
        reloaded.open_charge_record = saved["open_charge_record"]
        charge, _ = _replay(HARRYS_POLLS[4:], reloaded)
        self.assertEqual(charge["duration_s"], 6290 - 74)

    def test_a_session_saved_before_this_change_still_closes(self):
        # Updated mid-charge: there is an open session but nothing followed.
        _, manager = _replay(HARRYS_POLLS[:4])
        manager.open_charge_record = None
        charge, _ = _replay(HARRYS_POLLS[4:], manager)
        self.assertEqual(charge["duration_source"], "car")
        self.assertEqual(charge["charge_start_ts"], _at(2, 0, 55, 21).isoformat())


if __name__ == "__main__":
    unittest.main()
