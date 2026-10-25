"""A charge that started and finished between two readings is still recorded.

@HarryFlatter's HS PHEV, 10-11 Oct 2026 (times BST), quiet hours 00:00-06:00:

    23:40  live poll: plugged in, waiting (Connecting), 71.3 %
    00:30  zappi starts the charge; nothing polls the car
    02:24  charge complete (the car's own record: last stretch 02:13-02:17)
    06:09  live poll at the end of quiet hours: Charging Finished, 100 %

No reading saw the car charging, so no charge was recorded and Last Charge
still showed the one from 8 Oct.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from test_charge_stats import CSnap, ts as TRIP

UTC = timezone.utc
CAPACITY = 23.2

SOLAR_START = 1791640497   # 10 Oct 13:54:57 UTC, a short top-up that afternoon
SOLAR_END = 1791640698     # 13:58:18
LAST_START = 1791681187    # 11 Oct 01:13:07 UTC, the charge's last stretch
LAST_END = 1791681432      # 01:17:12

BEFORE_AT = datetime(2026, 10, 10, 22, 40, 36, tzinfo=UTC)
AFTER_AT = datetime(2026, 10, 11, 5, 9, 47, tzinfo=UTC)


def _snap(at, soc, start, end, odometer=6381.0):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=odometer, range_km=None,
                 record_start=start, record_end=end)


BEFORE = _snap(BEFORE_AT, 71.3, SOLAR_START, SOLAR_END)
AFTER = _snap(AFTER_AT, 100.0, LAST_START, LAST_END)


async def _done():
    return None


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


def _note(manager, snap, *, paused=False, plugged=True):
    return manager.note_charge_state(
        False, snap, capacity_kwh=CAPACITY, now_iso=snap.ts,
        is_plugged_in=plugged, charge_paused=paused,
    )


class HarrysNightTests(unittest.TestCase):
    def setUp(self):
        self.manager = _manager()
        # 23:40 Connecting counts as paused-not-finished; nothing to record.
        charge, _ = _note(self.manager, BEFORE, paused=True)
        self.assertIsNone(charge)
        self.charge, self.changed = _note(self.manager, AFTER)

    def test_the_charge_is_recorded(self):
        self.assertIsNotNone(self.charge)
        self.assertTrue(self.changed)
        self.assertIs(self.manager.last_charge, self.charge)
        self.assertTrue(self.charge["reconstructed"])

    def test_energy_from_the_battery_rise(self):
        self.assertEqual(self.charge["soc_start_pct"], 71.3)
        self.assertEqual(self.charge["soc_end_pct"], 100.0)
        self.assertEqual(self.charge["soc_added_pct"], 28.7)
        self.assertEqual(self.charge["energy_added_kWh"], round(0.287 * CAPACITY, 3))
        self.assertEqual(self.charge["method"], "soc")

    def test_the_end_comes_from_the_cars_record(self):
        self.assertEqual(
            self.charge["charge_end_ts"],
            datetime(2026, 10, 11, 1, 17, 12, tzinfo=UTC).isoformat(),
        )

    def test_no_duration_is_claimed(self):
        # The car's record covers only the last 4 minutes of the charge.
        self.assertNotIn("duration_s", self.charge)
        self.assertNotIn("charge_start_ts", self.charge)
        self.assertNotIn("average_power_kW", self.charge)
        self.assertEqual(self.charge["duration_source"], "not_seen")
        self.assertIsNone(self.manager.charge_session())

    def test_not_recorded_twice(self):
        later = _snap(datetime(2026, 10, 11, 6, 9, 47, tzinfo=UTC), 100.0,
                      LAST_START, LAST_END)
        charge, _ = _note(self.manager, later)
        self.assertIsNone(charge)
        self.assertIs(self.manager.last_charge, self.charge)


class NotAChargeTests(unittest.TestCase):
    def _pair(self, after, *, paused=False, plugged=True, before=BEFORE):
        manager = _manager()
        _note(manager, before, paused=True)
        charge, _ = _note(manager, after, paused=paused, plugged=plugged)
        return charge, manager

    def test_the_cars_record_did_not_move(self):
        # Plugged in all night, the battery's own rebound: no charge.
        after = _snap(AFTER_AT, 72.8, SOLAR_START, SOLAR_END)
        self.assertIsNone(self._pair(after)[0])

    def test_driven_in_between(self):
        after = _snap(AFTER_AT, 100.0, LAST_START, LAST_END, odometer=6400.0)
        self.assertIsNone(self._pair(after)[0])

    def test_a_finished_stretch_counts_even_if_the_car_says_waiting(self):
        # An HS reads Connecting after a charge that stopped short of full.
        charge, _ = self._pair(AFTER, paused=True)
        self.assertIsNotNone(charge)

    def test_record_ending_outside_the_two_readings(self):
        after = _snap(AFTER_AT, 100.0, SOLAR_START - 7200, SOLAR_START - 3600)
        self.assertIsNone(self._pair(after)[0])

    def test_too_small_a_rise(self):
        after = _snap(AFTER_AT, 72.0, LAST_START, LAST_END)
        self.assertIsNone(self._pair(after)[0])

    def test_no_reading_before(self):
        manager = _manager()
        charge, _ = _note(manager, AFTER)
        self.assertIsNone(charge)

    def test_readings_too_far_apart(self):
        before = _snap(datetime(2026, 10, 8, 22, 0, tzinfo=UTC), 71.3,
                       SOLAR_START - 90000, SOLAR_END - 90000)
        self.assertIsNone(self._pair(AFTER, before=before)[0])


class OtherCasesTests(unittest.TestCase):
    def test_plugged_in_after_a_reading_that_saw_it_unplugged(self):
        # Home at 23:00, plugged in during quiet hours: the reading before
        # had no cable in.
        manager = _manager()
        _note(manager, BEFORE, plugged=False)
        self.assertIsNone(manager.pre_charge_snapshot)
        charge, _ = _note(manager, AFTER)
        self.assertIsNotNone(charge)
        self.assertEqual(charge["soc_added_pct"], 28.7)

    def test_a_charge_seen_live_is_not_recorded_again(self):
        manager = _manager()
        _note(manager, BEFORE, paused=True)
        mid = _snap(datetime(2026, 10, 10, 23, 45, tzinfo=UTC), 80.0,
                    LAST_START - 3000, SOLAR_END)
        manager.note_charge_state(True, mid, capacity_kwh=CAPACITY,
                                  now_iso=mid.ts, is_plugged_in=True)
        done = _snap(datetime(2026, 10, 11, 2, 0, tzinfo=UTC), 100.0,
                     LAST_START, LAST_END)
        first, _ = _note(manager, done, plugged=False)
        self.assertIsNotNone(first)
        self.assertNotIn("reconstructed", first)
        again, _ = _note(manager, AFTER, plugged=False)
        self.assertIsNone(again)
        self.assertIs(manager.last_charge, first)

    def test_the_idle_reading_survives_a_restart(self):
        manager = _manager()
        _note(manager, BEFORE, paused=True)
        saved = {}
        manager._store = MagicMock()
        manager._store.async_save = lambda data: saved.update(data) or _done()
        import asyncio
        asyncio.new_event_loop().run_until_complete(manager.async_save())
        self.assertEqual(saved["idle_charge_reading"]["soc_pct"], 71.3)

    def test_unplugged_before_the_morning_reading_still_counts(self):
        manager = _manager()
        _note(manager, BEFORE, paused=True)
        charge, _ = _note(manager, AFTER, plugged=False)
        self.assertIsNotNone(charge)
        self.assertIsNone(manager.pre_charge_snapshot)

    def test_a_car_with_no_charge_record_needs_a_bigger_rise(self):
        before = _snap(BEFORE_AT, 71.3, None, None)
        small = _snap(AFTER_AT, 72.5, None, None)
        big = _snap(AFTER_AT, 80.0, None, None)
        self.assertIsNone(TRIP.reconstruct_unseen_charge(before, small, capacity_kwh=CAPACITY))
        charge = TRIP.reconstruct_unseen_charge(before, big, capacity_kwh=CAPACITY)
        self.assertEqual(charge["soc_added_pct"], 8.7)
        self.assertNotIn("charge_end_ts", charge)

    def test_a_charge_seen_live_is_unchanged(self):
        # The normal path still opens on the first charging reading.
        manager = _manager()
        _note(manager, BEFORE, paused=True)
        mid = _snap(datetime(2026, 10, 10, 23, 45, tzinfo=UTC), 80.0,
                    LAST_START - 3000, SOLAR_END)
        charge, _ = manager.note_charge_state(
            True, mid, capacity_kwh=CAPACITY, now_iso=mid.ts, is_plugged_in=True,
        )
        self.assertIsNone(charge)
        self.assertIsNotNone(manager.open_charge)
        charge, _ = _note(manager, AFTER)
        self.assertIsNotNone(charge)
        self.assertNotIn("reconstructed", charge)


if __name__ == "__main__":
    unittest.main()
