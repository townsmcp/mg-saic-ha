# File: tests/test_trip_stats.py
"""Unit tests for the pure trip/efficiency maths in trip_stats (#301).

These import only the pure functions, which have no Home Assistant deps, so
they run in plain CPython with no stubs — matching the python-tests.yaml CI.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ts = _load("mg_saic_trip_stats_under_test", PKG_DIR / "trip_stats.py")
Snap = ts.TripSnapshot


def snap(odo, soc=None, fuel=None, t="2026-08-19T10:00:00+00:00"):
    return Snap(ts=t, odometer_km=odo, soc_pct=soc, fuel_pct=fuel)


class TestBevTrip(unittest.TestCase):
    def test_basic_electric_trip(self):
        start = snap(1000.0, soc=80.0, t="2026-08-19T10:00:00+00:00")
        end = snap(1040.0, soc=70.0, t="2026-08-19T10:40:00+00:00")
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=64.0, tank_litres=None,
            is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 40.0)
        self.assertEqual(trip["soc_used_pct"], 10.0)
        # 10% of 64 kWh = 6.4 kWh; 40 km / 6.4 = 6.25 km/kWh
        self.assertAlmostEqual(trip["energy_kWh"], 6.4, places=3)
        self.assertAlmostEqual(trip["efficiency_km_per_kWh"], 6.25, places=2)
        self.assertAlmostEqual(trip["efficiency_mi_per_kWh"], 3.88, places=1)
        self.assertAlmostEqual(trip["consumption_kWh_per_100km"], 16.0, places=1)
        # Mile-equivalent attributes
        self.assertAlmostEqual(trip["distance_mi"], 24.85, places=1)  # 40 km
        self.assertAlmostEqual(trip["consumption_kWh_per_100mi"], 25.75, places=1)
        self.assertEqual(trip["duration_s"], 40 * 60)
        self.assertFalse(trip["charged_during_park"])
        # No fuel figures for a pure-electric trip.
        self.assertIsNone(trip["fuel_used_litres"])

    def test_charged_between_flags_and_skips_energy(self):
        # SOC went UP (charged while parked before this drive's end reading).
        start = snap(1000.0, soc=50.0)
        end = snap(1010.0, soc=60.0)
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=64.0, tank_litres=None,
            is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 10.0)  # distance still valid
        self.assertTrue(trip["charged_during_park"])
        self.assertIsNone(trip["energy_kWh"])
        self.assertIsNone(trip["efficiency_km_per_kWh"])

    def test_missing_capacity_gives_distance_and_soc_only(self):
        start = snap(1000.0, soc=80.0)
        end = snap(1020.0, soc=75.0)
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=None, tank_litres=None,
            is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["soc_used_pct"], 5.0)
        self.assertIsNone(trip["energy_kWh"])


class TestIceTrip(unittest.TestCase):
    def test_fuel_trip(self):
        start = snap(500.0, fuel=90.0)
        end = snap(560.0, fuel=80.0)  # used 10% of a 50 L tank = 5 L over 60 km
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=None, tank_litres=50.0,
            is_electric=False, is_combustion=True,
        )
        self.assertEqual(trip["distance_km"], 60.0)
        self.assertEqual(trip["fuel_used_pct"], 10.0)
        self.assertAlmostEqual(trip["fuel_used_litres"], 5.0, places=2)
        # 5 L / 60 km * 100 = 8.33 L/100km
        self.assertAlmostEqual(trip["fuel_consumption_L_per_100km"], 8.33, places=1)
        # mpg for imperial users (HA can't convert L/100km automatically)
        self.assertAlmostEqual(trip["fuel_economy_mpg_uk"], 33.9, places=1)
        self.assertAlmostEqual(trip["fuel_economy_mpg_us"], 28.2, places=1)
        self.assertIsNone(trip["energy_kWh"])  # not electric

    def test_refuelled_between_flags_and_skips_fuel(self):
        start = snap(500.0, fuel=30.0)
        end = snap(520.0, fuel=95.0)
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=None, tank_litres=50.0,
            is_electric=False, is_combustion=True,
        )
        self.assertTrue(trip["refuelled_during_park"])
        self.assertIsNone(trip["fuel_used_litres"])


class TestPhevTrip(unittest.TestCase):
    def test_reports_both_electric_and_fuel(self):
        start = snap(2000.0, soc=70.0, fuel=80.0)
        end = snap(2050.0, soc=60.0, fuel=76.0)
        trip = ts.compute_completed_trip(
            start, end, capacity_kwh=20.0, tank_litres=40.0,
            is_electric=True, is_combustion=True,
        )
        self.assertEqual(trip["distance_km"], 50.0)
        self.assertAlmostEqual(trip["energy_kWh"], 2.0, places=3)  # 10% of 20
        self.assertAlmostEqual(trip["fuel_used_litres"], 1.6, places=2)  # 4% of 40


class TestInvalidTrips(unittest.TestCase):
    def test_zero_distance_is_not_a_trip(self):
        self.assertIsNone(
            ts.compute_completed_trip(
                snap(1000.0, soc=80.0), snap(1000.0, soc=79.0),
                capacity_kwh=64.0, tank_litres=None,
                is_electric=True, is_combustion=False,
            )
        )

    def test_backwards_odometer_is_not_a_trip(self):
        self.assertIsNone(
            ts.compute_completed_trip(
                snap(1000.0), snap(990.0),
                capacity_kwh=64.0, tank_litres=None,
                is_electric=True, is_combustion=False,
            )
        )

    def test_implausible_jump_rejected(self):
        # Guards odometer rollover / saturation sentinel leaking through.
        self.assertIsNone(
            ts.compute_completed_trip(
                snap(100.0), snap(100000.0),
                capacity_kwh=64.0, tank_litres=None,
                is_electric=True, is_combustion=False,
            )
        )


class TestSinceChargeEfficiency(unittest.TestCase):
    def test_basic(self):
        r = ts.compute_since_charge_efficiency(100.0, 16.0)
        self.assertAlmostEqual(r["efficiency_km_per_kWh"], 6.25, places=2)
        self.assertAlmostEqual(r["consumption_kWh_per_100km"], 16.0, places=1)
        # Mile equivalents present
        self.assertAlmostEqual(r["distance_mi"], 62.14, places=1)
        self.assertAlmostEqual(r["efficiency_mi_per_kWh"], 3.88, places=1)
        self.assertAlmostEqual(r["consumption_kWh_per_100mi"], 25.75, places=1)

    def test_zero_or_missing_returns_none(self):
        self.assertIsNone(ts.compute_since_charge_efficiency(0.0, 16.0))
        self.assertIsNone(ts.compute_since_charge_efficiency(100.0, None))
        self.assertIsNone(ts.compute_since_charge_efficiency(None, None))


class TestSnapshotRoundTrip(unittest.TestCase):
    def test_to_from_dict(self):
        s = snap(1234.5, soc=55.0, fuel=None)
        s2 = Snap.from_dict(s.to_dict())
        self.assertEqual(s2.odometer_km, 1234.5)
        self.assertEqual(s2.soc_pct, 55.0)
        self.assertIsNone(s2.fuel_pct)

    def test_from_dict_none_and_garbage(self):
        self.assertIsNone(Snap.from_dict(None))
        self.assertIsNone(Snap.from_dict({"odometer_km": "not-a-number"}))


class TestManagerLifecycle(unittest.TestCase):
    """TripStatsManager open/close state machine (sync parts, no HA Store)."""

    def _mgr(self):
        from unittest.mock import MagicMock

        return ts.TripStatsManager(MagicMock(), "entry1", "VIN123")

    def test_open_then_close_produces_last_trip(self):
        m = self._mgr()
        self.assertTrue(m.open(snap(1000.0, soc=80.0)))
        self.assertIsNotNone(m.open_snapshot)
        trip = m.close(
            snap(1040.0, soc=70.0), capacity_kwh=64.0, tank_litres=None,
            is_electric=True, is_combustion=False,
        )
        self.assertIsNotNone(trip)
        self.assertEqual(trip["distance_km"], 40.0)
        self.assertEqual(m.last_trip["distance_km"], 40.0)
        self.assertIsNone(m.open_snapshot)  # cleared after close

    def test_open_is_idempotent_keeps_first_start(self):
        m = self._mgr()
        self.assertTrue(m.open(snap(1000.0, soc=80.0)))
        # A second open while one is in progress is ignored (keeps the baseline).
        self.assertFalse(m.open(snap(1010.0, soc=78.0)))
        self.assertEqual(m.open_snapshot.odometer_km, 1000.0)

    def test_close_with_no_open_trip_returns_none(self):
        m = self._mgr()
        self.assertIsNone(
            m.close(
                snap(1040.0, soc=70.0), capacity_kwh=64.0, tank_litres=None,
                is_electric=True, is_combustion=False,
            )
        )
        self.assertIsNone(m.last_trip)

    def test_zero_distance_trip_not_stored_but_open_cleared(self):
        m = self._mgr()
        m.open(snap(1000.0, soc=80.0))
        trip = m.close(
            snap(1000.0, soc=79.0), capacity_kwh=64.0, tank_litres=None,
            is_electric=True, is_combustion=False,
        )
        self.assertIsNone(trip)      # not a real trip
        self.assertIsNone(m.last_trip)
        self.assertIsNone(m.open_snapshot)  # still cleared so the next drive opens fresh


class TestCounterBasedTrip(unittest.TestCase):
    """Distance/energy from the car's since-charge counters (the preferred path)."""

    def _snap(self, odo, since_km=None, since_kwh=None, soc=None, t="2026-08-20T06:36:00+00:00"):
        return Snap(ts=t, odometer_km=odo, soc_pct=soc,
                    since_charge_km=since_km, since_charge_kwh=since_kwh)

    def test_first_trip_since_charge_matches_counters(self):
        # Reproduces the live report: one drive since charge, baseline at 0.
        # Odometer only moved 3 km (late/fragmented open) but the counters say 11.
        start = self._snap(1008.0, since_km=8.0, since_kwh=1.0)  # opened 8 km in
        end = self._snap(1011.0, since_km=11.0, since_kwh=1.4,
                         t="2026-08-20T06:51:00+00:00")
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 0.0, "since_charge_kwh": 0.0},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        # Counter diff (11-0) wins over the 3 km odometer delta.
        self.assertEqual(trip["distance_km"], 11.0)
        self.assertEqual(trip["energy_kWh"], 1.4)
        self.assertAlmostEqual(trip["efficiency_km_per_kWh"], 7.86, places=2)
        self.assertAlmostEqual(trip["efficiency_mi_per_kWh"], 4.88, places=2)

    def test_second_trip_diffs_from_previous_close(self):
        start = self._snap(1020.0, since_km=11.0, since_kwh=1.4)
        end = self._snap(1027.0, since_km=18.0, since_kwh=2.4)
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 11.0, "since_charge_kwh": 1.4},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 7.0)
        self.assertAlmostEqual(trip["energy_kWh"], 1.0, places=3)

    def test_charge_reset_since_last_close(self):
        # Counter went backwards vs baseline => charged since last close.
        start = self._snap(1030.0, since_km=0.0, since_kwh=0.0)
        end = self._snap(1035.0, since_km=5.0, since_kwh=0.8)
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 18.0, "since_charge_kwh": 2.4},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 5.0)  # current value = the trip
        self.assertAlmostEqual(trip["energy_kWh"], 0.8, places=3)

    def test_falls_back_to_odometer_when_no_counters(self):
        start = self._snap(2000.0, soc=80.0)  # no since_charge fields
        end = self._snap(2040.0, soc=70.0)
        trip = ts.compute_completed_trip(
            start, end, baseline=None,
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 40.0)  # odometer delta
        self.assertAlmostEqual(trip["energy_kWh"], 6.4, places=3)  # SOC×capacity

    def test_counter_reset_mid_trip_falls_back_to_odometer_not_zero(self):
        # Reproduces a live incident: a real ~91 km drive, but the since-charge
        # counter spuriously reset to 0 right at the closing poll (no charge —
        # SOC fell smoothly through it), and the manager had already rebased the
        # baseline to match in the same poll. Naively, (0 - 0) = 0 looks like a
        # valid reading, not a missing one, and would silently report NO trip
        # despite ~91 km of real movement. Must fall back to the odometer.
        start = self._snap(1100.0, since_km=56.5, since_kwh=14.6, soc=57.7,
                           t="2026-08-22T11:41:31+01:00")
        end = self._snap(1191.0, since_km=0.0, since_kwh=0.0, soc=40.8,
                         t="2026-08-22T14:58:10+01:00")
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 0.0, "since_charge_kwh": 0.0},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertIsNotNone(trip)  # must NOT silently vanish
        self.assertEqual(trip["distance_km"], 91.0)       # odometer delta, not 0
        self.assertTrue(trip["counter_reset_detected"])
        # Energy also falls back (counter energy is equally untrustworthy here).
        self.assertAlmostEqual(trip["soc_used_pct"], 16.9, places=1)
        self.assertAlmostEqual(trip["energy_kWh"], 16.9 / 100 * 64.0, places=2)

    def test_small_counter_value_not_flagged_when_odometer_agrees(self):
        # A genuinely tiny trip (counter and odometer both small) must NOT trip
        # the reset-sanity-check — only a real disagreement should.
        start = self._snap(1200.0, since_km=10.0, since_kwh=1.0)
        end = self._snap(1200.3, since_km=10.3, since_kwh=1.05)
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 10.0, "since_charge_kwh": 1.0},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 0.3)
        self.assertNotIn("counter_reset_detected", trip)


class TestParallelCounterSocFigures(unittest.TestCase):
    """The *_counter / *_soc comparison attributes (#301)."""

    def _snap(self, odo, since_km=None, since_kwh=None, soc=None, t="2026-08-20T06:36:00+00:00"):
        return Snap(ts=t, odometer_km=odo, soc_pct=soc,
                    since_charge_km=since_km, since_charge_kwh=since_kwh)

    def test_counter_and_soc_sets_both_present_and_independent(self):
        # Reproduces SteveMSJ's report: the counter over-reports energy (17%
        # high here) relative to the SOC-based figure, even with no reset —
        # both should be exposed, self-consistently paired with their own
        # distance source, alongside the existing primary (counter-preferred).
        start = self._snap(1000.0, since_km=0.0, since_kwh=0.0, soc=100.0)
        end = self._snap(1341.0, since_km=341.0, since_kwh=59.1, soc=18.4,
                         t="2026-08-20T12:00:00+00:00")
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 0.0, "since_charge_kwh": 0.0},
            capacity_kwh=61.7, tank_litres=None, is_electric=True, is_combustion=False,
        )
        # Primary stays counter-preferred (unchanged behaviour).
        self.assertEqual(trip["distance_km"], 341.0)
        self.assertEqual(trip["energy_kWh"], 59.1)

        # Counter-only set: counter distance + counter energy.
        self.assertEqual(trip["distance_km_counter"], 341.0)
        self.assertEqual(trip["energy_kWh_counter"], 59.1)

        # Odometer-only distance always present.
        self.assertEqual(trip["distance_km_odometer"], 341.0)
        self.assertAlmostEqual(trip["distance_mi_odometer"], 341.0 / ts.KM_PER_MILE, places=2)

        # SOC-only set: odometer distance + SOC×capacity energy — matches
        # Steve's manual calc (81.6% x 61.7 = 50.3 kWh), independent of the
        # counter's 59.1 kWh (a ~17% discrepancy, visible by comparing the two).
        self.assertAlmostEqual(trip["energy_kWh_soc"], 50.35, places=1)
        self.assertLess(trip["energy_kWh_soc"], trip["energy_kWh_counter"])

    def test_counter_reset_still_exposes_raw_bogus_counter_value(self):
        # Even when the primary figure discards a reset counter reading, the
        # raw (bogus) counter value should still be visible in *_counter —
        # seeing "the counter said 0" is itself useful, not something to hide.
        start = self._snap(1100.0, since_km=56.5, since_kwh=14.6, soc=57.7)
        end = self._snap(1191.0, since_km=0.0, since_kwh=0.0, soc=40.8,
                         t="2026-08-20T14:58:10+00:00")
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 0.0, "since_charge_kwh": 0.0},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertTrue(trip["counter_reset_detected"])
        self.assertEqual(trip["distance_km"], 91.0)  # primary fell back to odometer
        # Raw counter figures still shown (0 - 0 = 0), distinguishable via the flag.
        self.assertEqual(trip["distance_km_counter"], 0.0)
        self.assertIsNone(trip["energy_kWh_counter"])  # 0 energy -> no valid ratio
        # SOC-based set is unaffected and gives a real figure.
        self.assertAlmostEqual(trip["energy_kWh_soc"], 16.9 / 100 * 64.0, places=2)

    def test_no_soc_data_leaves_soc_set_none(self):
        start = self._snap(1000.0, since_km=0.0, since_kwh=0.0)  # no soc
        end = self._snap(1020.0, since_km=20.0, since_kwh=3.0)
        trip = ts.compute_completed_trip(
            start, end, baseline={"since_charge_km": 0.0, "since_charge_kwh": 0.0},
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertIsNone(trip["energy_kWh_soc"])
        self.assertIsNone(trip["efficiency_km_per_kWh_soc"])


class TestSocSinceResetEfficiency(unittest.TestCase):
    """compute_soc_since_reset_efficiency — the pure SOC/odometer-only calc."""

    def test_basic_calculation(self):
        result = ts.compute_soc_since_reset_efficiency(
            baseline_soc_pct=100.0, current_soc_pct=18.4,
            baseline_odometer_km=1000.0, current_odometer_km=1341.0,
            capacity_kwh=61.7,
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["distance_km"], 341.0)
        self.assertAlmostEqual(result["energy_kWh"], 50.35, places=1)
        self.assertEqual(result["baseline_soc_pct"], 100.0)

    def test_no_baseline_returns_none(self):
        self.assertIsNone(ts.compute_soc_since_reset_efficiency(
            None, 50.0, None, 1000.0, 64.0
        ))

    def test_no_movement_returns_none(self):
        self.assertIsNone(ts.compute_soc_since_reset_efficiency(
            80.0, 80.0, 1000.0, 1000.0, 64.0
        ))

    def test_soc_rose_since_baseline_returns_none(self):
        # A further charge happened without the baseline being rebased yet —
        # shouldn't report negative/nonsensical energy.
        self.assertIsNone(ts.compute_soc_since_reset_efficiency(
            50.0, 60.0, 1000.0, 1010.0, 64.0
        ))


class TestNoteSocResetBaseline(unittest.TestCase):
    def _mgr(self):
        from unittest.mock import MagicMock
        return ts.TripStatsManager(MagicMock(), "e", "V")

    def test_seeds_on_first_call(self):
        m = self._mgr()
        self.assertTrue(m.note_soc_reset_baseline(80.0, 1000.0, "t1"))
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 80.0)

    def test_rebases_on_soc_rise_only(self):
        m = self._mgr()
        m.note_soc_reset_baseline(50.0, 1000.0, "t1")
        # SOC dropped (driving happened) -> baseline unchanged. The call still
        # reports a change, because the new low must be persisted.
        m.note_soc_reset_baseline(40.0, 1010.0, "t2")
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 50.0)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 1000.0)
        # SOC rose (a charge) -> rebase.
        self.assertTrue(m.note_soc_reset_baseline(100.0, 1010.0, "t3"))
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 100.0)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 1010.0)

    def test_rebases_on_a_charge_that_stops_below_the_previous_peak(self):
        """The bug this replaced: the old rule kept a running maximum, so a
        charge ending below a previous peak never rebased. Real numbers from
        an MGS6 — 80% baseline, 192 km driven, charged back to 79.3%, leaving
        0.7% "used" over 192 km and an efficiency two orders of magnitude out.
        """
        m = self._mgr()
        m.note_soc_reset_baseline(80.0, 4000.0, "t1")
        m.note_soc_reset_baseline(21.0, 4192.0, "t2")  # driven 192 km
        m.note_soc_reset_baseline(79.3, 4192.0, "t3")  # charged, below 80
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 79.3)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 4192.0)

    def test_post_drive_rebound_does_not_rebase(self):
        # The pack reports a fraction of a percent back after parking. That is
        # not a charge and must not move the baseline.
        m = self._mgr()
        m.note_soc_reset_baseline(80.0, 1000.0, "t1")
        m.note_soc_reset_baseline(60.0, 1100.0, "t2")
        m.note_soc_reset_baseline(60.3, 1100.0, "t3")
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 80.0)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 1000.0)

    def test_slow_trickle_charge_is_caught_cumulatively(self):
        # No single step clears the threshold, but the total gain does.
        m = self._mgr()
        m.note_soc_reset_baseline(80.0, 1000.0, "t1")
        m.note_soc_reset_baseline(60.0, 1100.0, "t2")
        for soc in (60.2, 60.4, 60.6):
            m.note_soc_reset_baseline(soc, 1100.0, "t")
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 60.6)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 1100.0)

    def test_low_water_mark_follows_the_discharge(self):
        m = self._mgr()
        m.note_soc_reset_baseline(80.0, 1000.0, "t1")
        for soc, odo in ((70.0, 1050.0), (55.0, 1120.0), (30.0, 1240.0)):
            m.note_soc_reset_baseline(soc, odo, "t")
        self.assertEqual(m.soc_reset_baseline["soc_low_pct"], 30.0)
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 80.0)
        # A charge from the bottom rebases to where the charge ended.
        m.note_soc_reset_baseline(45.0, 1240.0, "t")
        self.assertEqual(m.soc_reset_baseline["soc_pct"], 45.0)
        self.assertEqual(m.soc_reset_baseline["odometer_km"], 1240.0)

    def test_none_soc_is_a_no_op(self):
        m = self._mgr()
        self.assertFalse(m.note_soc_reset_baseline(None, 1000.0, "t1"))
        self.assertIsNone(m.soc_reset_baseline)


class TestNoteSinceCharge(unittest.TestCase):
    def _mgr(self):
        from unittest.mock import MagicMock
        return ts.TripStatsManager(MagicMock(), "e", "V")

    def test_seeds_then_rebases_on_reset(self):
        m = self._mgr()
        self.assertTrue(m.note_since_charge(0.0, 0.0))      # first poll seeds baseline
        # A close sets the baseline to the last trip's end counter (=11).
        m.since_charge_baseline = {"since_charge_km": 11.0, "since_charge_kwh": 1.4}
        self.assertFalse(m.note_since_charge(18.0, 2.4))    # climbing, no rebase
        self.assertTrue(m.note_since_charge(0.0, 0.0))      # dropped -> charge reset
        self.assertEqual(m.since_charge_baseline["since_charge_km"], 0.0)

    def test_close_rebases_baseline_to_counter(self):
        m = self._mgr()
        m.note_since_charge(0.0, 0.0)
        m.open(Snap(ts="2026-08-20T06:36:00+00:00", odometer_km=1008.0,
                    since_charge_km=8.0, since_charge_kwh=1.0))
        trip = m.close(
            Snap(ts="2026-08-20T06:51:00+00:00", odometer_km=1011.0,
                 since_charge_km=11.0, since_charge_kwh=1.4),
            capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False,
        )
        self.assertEqual(trip["distance_km"], 11.0)
        self.assertEqual(m.since_charge_baseline["since_charge_km"], 11.0)


class TestRetrospectiveTrip(unittest.TestCase):
    def _mgr(self):
        from unittest.mock import MagicMock
        return ts.TripStatsManager(MagicMock(), "e", "V")

    _kw = dict(capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False)

    def test_first_parked_reading_only_seeds_no_trip(self):
        m = self._mgr()
        trip = m.detect_missed_trip(
            Snap(ts="2026-08-21T07:00:00+00:00", odometer_km=2000.0, soc_pct=80.0),
            **self._kw)
        self.assertIsNone(trip)  # nothing to compare against yet
        self.assertIsNotNone(m.last_parked_snapshot)

    def test_odometer_jump_reconstructs_trip(self):
        m = self._mgr()
        # Parked at 06:00, then observed parked again at 10:00, 12 km further on:
        # a drive happened in the gap that was never seen live.
        m.detect_missed_trip(
            Snap(ts="2026-08-21T06:00:00+00:00", odometer_km=2000.0, soc_pct=80.0),
            **self._kw)
        trip = m.detect_missed_trip(
            Snap(ts="2026-08-21T10:00:00+00:00", odometer_km=2012.0, soc_pct=76.0),
            **self._kw)
        self.assertIsNotNone(trip)
        self.assertEqual(trip["distance_km"], 12.0)          # odometer delta
        self.assertTrue(trip["retrospective"])
        self.assertEqual(trip["timing"], "approximate")
        # Energy from SOC change (4% of 64 kWh), not a counter.
        self.assertAlmostEqual(trip["energy_kWh"], 2.56, places=2)

    def test_no_movement_advances_baseline_without_trip(self):
        m = self._mgr()
        m.detect_missed_trip(
            Snap(ts="2026-08-21T06:00:00+00:00", odometer_km=2000.0), **self._kw)
        trip = m.detect_missed_trip(
            Snap(ts="2026-08-21T06:30:00+00:00", odometer_km=2000.0), **self._kw)
        self.assertIsNone(trip)  # sub-threshold, just a parked poll

    def test_charge_in_gap_gives_distance_but_no_efficiency(self):
        m = self._mgr()
        m.detect_missed_trip(
            Snap(ts="2026-08-21T06:00:00+00:00", odometer_km=2000.0, soc_pct=40.0),
            **self._kw)
        # SOC rose (a charge happened somewhere in the gap) -> no electric figure.
        trip = m.detect_missed_trip(
            Snap(ts="2026-08-21T10:00:00+00:00", odometer_km=2015.0, soc_pct=90.0),
            **self._kw)
        self.assertEqual(trip["distance_km"], 15.0)
        self.assertTrue(trip["charged_during_park"])
        self.assertIsNone(trip["efficiency_km_per_kWh"])


class TestForceCloseStale(unittest.TestCase):
    def _mgr(self):
        from unittest.mock import MagicMock
        return ts.TripStatsManager(MagicMock(), "e", "V")

    _kw = dict(capacity_kwh=64.0, tank_litres=None, is_electric=True, is_combustion=False)

    def test_recent_open_is_not_closed(self):
        m = self._mgr()
        m.open(Snap(ts="2026-08-21T09:00:00+00:00", odometer_km=2000.0))
        # Only 1 hour later — well under the 24h backstop.
        trip = m.force_close_if_stale(
            "2026-08-21T10:00:00+00:00",
            Snap(ts="2026-08-21T10:00:00+00:00", odometer_km=2005.0), **self._kw)
        self.assertIsNone(trip)
        self.assertIsNotNone(m.open_snapshot)  # still open

    def test_stale_open_is_force_closed(self):
        m = self._mgr()
        m.open(Snap(ts="2026-08-20T06:00:00+00:00", odometer_km=2000.0, soc_pct=80.0))
        # ~28 hours later, parked, 30 km further on.
        trip = m.force_close_if_stale(
            "2026-08-21T10:00:00+00:00",
            Snap(ts="2026-08-21T10:00:00+00:00", odometer_km=2030.0, soc_pct=70.0),
            **self._kw)
        self.assertIsNotNone(trip)
        self.assertEqual(trip["distance_km"], 30.0)
        self.assertTrue(trip["retrospective"])
        self.assertIsNone(m.open_snapshot)  # unblocked

    def test_stale_open_without_reading_is_abandoned(self):
        m = self._mgr()
        m.open(Snap(ts="2026-08-20T06:00:00+00:00", odometer_km=2000.0))
        # No current reading (car unreachable) — clear the stuck trip, record none.
        trip = m.force_close_if_stale("2026-08-21T10:00:00+00:00", None, **self._kw)
        self.assertIsNone(trip)              # 0 distance -> no trip
        self.assertIsNone(m.open_snapshot)   # but the block is cleared


if __name__ == "__main__":
    unittest.main()
