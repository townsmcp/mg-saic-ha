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


class PollLandsInAPauseTests(unittest.TestCase):
    """@HarryFlatter, 3 Oct: "Would it make sense to total the durations until
    you get Charging Complete or Unplugged?" His Zappi shows "Waiting for EV"
    during the stops, so it is the car pausing.

    In his log no poll happened to land in one of the two half-minute pauses.
    Had one done so, the car would have been "not charging", the session
    would have closed there, and a new one would have opened at the next
    poll: Last Charge would have shown only what came after the pause --
    energy included. A charge that is stopped but not finished, with the
    cable still in, is now kept open.
    """

    def _note(self, manager, at, charging, soc, start, end, *, plugged=True, paused=False):
        return manager.note_charge_state(
            charging, _snap(at, soc, start, end),
            capacity_kwh=CAPACITY, now_iso=at.isoformat(),
            is_plugged_in=plugged, charge_paused=paused,
        )

    def _until_the_pause(self):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        self._note(manager, _at(1, 23, 4, 25), False, 62.7, PREVIOUS_END - 1700, PREVIOUS_END)
        self._note(manager, _at(1, 23, 49, 0), True, 68.7, STRETCH_1, PREVIOUS_END)
        # A poll at 00:38:00 UTC, inside the first pause (00:37:44 -> 00:38:22):
        # not charging, cable in, not finished.
        charge, changed = self._note(
            manager, _at(2, 0, 38, 0), False, 90.1, STRETCH_1, STRETCH_1_END, paused=True
        )
        self.assertIsNone(charge)
        self.assertTrue(changed)
        return manager

    def test_the_charge_stays_open_and_is_reported_whole(self):
        manager = self._until_the_pause()
        self.assertIsNotNone(manager.open_charge)
        self._note(manager, _at(2, 0, 50, 30), True, 93.1, STRETCH_2, STRETCH_1_END)
        self._note(manager, _at(2, 1, 21, 17), True, 99.8, STRETCH_3, STRETCH_2_END)
        charge, _ = self._note(manager, _at(2, 1, 50, 49), False, 100.0, STRETCH_3, CHARGE_END)
        self.assertAlmostEqual(charge["energy_added_kWh"], 8.654, places=3)
        self.assertEqual(charge["soc_start_pct"], 62.7)
        self.assertEqual(charge["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(charge["interruptions"], 2)
        self.assertEqual(charge["duration_s"], 6290 - 74)

    def test_still_stopped_a_few_minutes_later_is_still_one_charge(self):
        manager = self._until_the_pause()
        charge, changed = self._note(
            manager, _at(2, 0, 43, 0), False, 90.1, STRETCH_1, STRETCH_1_END, paused=True
        )
        self.assertIsNone(charge)
        self.assertFalse(changed)
        self.assertIsNotNone(manager.open_charge)

    def test_a_long_stop_ends_the_charge_where_it_stopped(self):
        # Never resumed (the charger's schedule ended, say). After 20 minutes
        # the charge is closed against the reading taken when it stopped.
        manager = self._until_the_pause()
        charge, _ = self._note(
            manager, _at(2, 1, 8, 0), False, 90.0, STRETCH_1, STRETCH_1_END, paused=True
        )
        self.assertIsNotNone(charge)
        self.assertEqual(charge["soc_end_pct"], 90.1)
        self.assertEqual(charge["end_ts"], _at(2, 0, 38, 0).isoformat())
        self.assertEqual(charge["charge_end_ts"], _at(2, 0, 37, 44).isoformat())
        self.assertIsNone(manager.open_charge)
        # The newest reading is what a following charge would start from.
        self.assertEqual(manager.pre_charge_snapshot.ts, _at(2, 1, 8, 0).isoformat())

    def test_unplugged_and_driven_before_the_next_poll(self):
        # Stopped, then unplugged and driven: the next reading is lower. The
        # charge ends at the reading taken when it stopped, not after the drive.
        manager = self._until_the_pause()
        charge, _ = self._note(
            manager, _at(2, 0, 50, 0), False, 84.0, STRETCH_1, STRETCH_1_END, plugged=False
        )
        self.assertEqual(charge["soc_end_pct"], 90.1)
        self.assertIsNone(manager.pre_charge_snapshot)

    def test_resumed_and_finished_before_the_next_poll(self):
        manager = self._until_the_pause()
        charge, _ = self._note(manager, _at(2, 1, 50, 49), False, 100.0, STRETCH_3, CHARGE_END)
        self.assertEqual(charge["soc_end_pct"], 100.0)
        self.assertEqual(charge["charge_end_ts"], _at(2, 1, 21, 33).isoformat())

    def test_finished_or_unplugged_still_ends_the_charge_at_once(self):
        # charge_paused is False for Charging Finished and Unplugged.
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        self._note(manager, _at(1, 23, 49, 0), True, 68.7, STRETCH_1, PREVIOUS_END)
        charge, _ = self._note(manager, _at(2, 1, 50, 49), False, 100.0, STRETCH_1, CHARGE_END)
        self.assertIsNotNone(charge)

    def test_plugged_in_and_waiting_before_any_charge_opens_nothing(self):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        charge, changed = self._note(
            manager, _at(1, 23, 4, 25), False, 62.7, PREVIOUS_END - 1700, PREVIOUS_END, paused=True
        )
        self.assertIsNone(charge)
        self.assertFalse(changed)
        self.assertIsNone(manager.open_charge)
        self.assertIsNone(manager.charge_paused_snapshot)
        self.assertIsNotNone(manager.pre_charge_snapshot)


class PausedStatusCodesTests(unittest.TestCase):
    """Which of the car's charging statuses count as paused."""

    def test_codes(self):
        import sys

        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        const = sys.modules["mg_saic.const"]
        paused = const.CHARGE_PAUSED_STATUS_CODES
        self.assertEqual(paused, {5, 6, 7, 8, 9})
        self.assertFalse(paused & const.CHARGE_SESSION_STATUS_CODES)
        for ends_it in (0, 2, 4, 13):  # Unplugged, Finished, Fault, V2X discharging
            self.assertNotIn(ends_it, paused)


class CoordinatorTellsTheManagerTests(unittest.TestCase):
    """coordinator._update_charge_state works out "paused" from the car's
    status and the cable."""

    def _call(self, status, gun):
        import sys
        from types import SimpleNamespace

        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        cls = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
        c = cls.__new__(cls)
        c.vin = "VIN"
        c.trip_stats = MagicMock()
        c.trip_stats.note_charge_state.return_value = (None, False)
        c._charge_snapshot = MagicMock(return_value="snapshot")
        c.resolve_battery_capacity_for = MagicMock(return_value=(CAPACITY, "profile"))
        c._schedule_trip_save = MagicMock()
        charging = SimpleNamespace(
            chrgMgmtData=SimpleNamespace(bmsChrgSts=status),
            rvsChargeStatus=SimpleNamespace(chargingGunState=gun),
        )
        cls._update_charge_state(c, SimpleNamespace(), charging)
        call = c.trip_stats.note_charge_state.call_args
        return call.args[0], call.kwargs["is_plugged_in"], call.kwargs["charge_paused"]

    def test_charging(self):
        self.assertEqual(self._call(1, 1), (True, True, False))

    def test_stopped_or_waiting_with_the_cable_in_is_paused(self):
        for status in (7, 8, 9):
            with self.subTest(status=status):
                self.assertEqual(self._call(status, 1), (False, True, True))

    def test_finished_is_not_paused(self):
        # Harry's last poll: bmsChrgSts 2, cable still in.
        self.assertEqual(self._call(2, 1), (False, True, False))

    def test_cable_out_is_not_paused(self):
        self.assertEqual(self._call(8, 0), (False, False, False))
        self.assertEqual(self._call(0, 0), (False, False, False))


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
