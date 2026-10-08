"""Charge session duration / average power from the car's own record (#262).

HA only sees a charge start or end when it polls, so each edge can land up to
a whole interval late. @HarryFlatter's HS PHEV, 25 Sep, 30-minute interval:

  HA's session:           23:03:25 -> 00:34:11 UTC   (1 h 30 min 46 s, 0.63 kW)
  car's record (SAIC):    23:30    -> 23:58    UTC   (28 min, ~2 kW)
  energy added:           0.951 kWh (SOC method; counter method 0.967)

rvsChargeStatus startTime/endTime carry the car's record. It's used for
duration and average power only when it's demonstrably this session's.
start_ts/end_ts stay HA's observed window, which the trip-overlap check uses.
"""

import unittest
from datetime import datetime, timezone

from test_charge_stats import CSnap, ts as TRIP

UTC = timezone.utc
T = lambda h, m, s=0, d=24: datetime(2026, 9, d, h, m, s, tzinfo=UTC)
E = lambda dt: dt.timestamp()

PREVIOUS_RECORD = (E(T(13, 40, 41)), E(T(13, 47, 6)))       # 24 Sep, the plug-in
THIS_RECORD = (E(T(23, 30)), E(T(23, 58)))                  # 24 Sep 23:30 -> 23:58


def _snap(at, record, soc):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=6118.0, range_km=None,
                 record_start=record[0], record_end=record[1])


def _session(start_record, end_record, *, end_at=T(0, 34, 11, d=25)):
    start = _snap(T(23, 3, 25), start_record, 95.9)
    end = _snap(end_at, end_record, 100.0)
    return TRIP.compute_charge_session(start, end, capacity_kwh=23.2)


class ChargeSessionTimingTests(unittest.TestCase):
    def test_harrys_charge_uses_the_cars_record(self):
        s = _session(PREVIOUS_RECORD, THIS_RECORD)
        self.assertEqual(s["duration_s"], 28 * 60)
        self.assertEqual(s["duration_source"], "car")
        self.assertEqual(s["charge_start_ts"], T(23, 30).isoformat())
        self.assertEqual(s["charge_end_ts"], T(23, 58).isoformat())
        self.assertAlmostEqual(s["average_power_kW"], s["energy_added_kWh"] / (28 / 60), places=2)
        # HA's observed window is untouched -- the trip-overlap check uses it.
        self.assertEqual(s["start_ts"], T(23, 3, 25).isoformat())
        self.assertEqual(s["end_ts"], T(0, 34, 11, d=25).isoformat())

    def test_the_previous_charges_record_is_never_used(self):
        """Record unchanged since the session began: it's the last charge's."""
        s = _session(PREVIOUS_RECORD, PREVIOUS_RECORD)
        self.assertEqual(s["duration_source"], "polls")
        self.assertEqual(s["duration_s"], 90 * 60 + 46)  # 23:03:25 -> 00:34:11

    def test_a_record_ending_outside_the_session_is_not_used(self):
        stale = (E(T(9, 0)), E(T(9, 30)))  # changed, but ended before HA saw charging
        self.assertEqual(_session(PREVIOUS_RECORD, stale)["duration_source"], "polls")

    def test_a_little_clock_skew_past_the_end_is_allowed(self):
        skewed = (E(T(23, 30)), E(T(0, 40, d=25)))  # 6 min after HA saw it end
        self.assertEqual(_session(PREVIOUS_RECORD, skewed)["duration_source"], "car")

    def test_nonsense_records_fall_back_to_polls(self):
        for record in ((None, None), (E(T(23, 58)), E(T(23, 30))),
                       (E(T(23, 30, d=21)), E(T(23, 58)))):  # > 48 h long
            with self.subTest(record=record):
                self.assertEqual(_session(PREVIOUS_RECORD, record)["duration_source"], "polls")

    def test_snapshots_saved_before_this_change_still_load(self):
        old = {"ts": T(23, 3, 25).isoformat(), "soc_pct": 95.9}
        snap = CSnap.from_dict(old)
        self.assertIsNone(snap.record_start)
        self.assertIsNone(snap.record_end)

    def test_record_times_round_trip_through_storage(self):
        snap = _snap(T(23, 3, 25), THIS_RECORD, 95.9)
        self.assertEqual(CSnap.from_dict(snap.to_dict()), snap)


if __name__ == "__main__":
    unittest.main()
