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


if __name__ == "__main__":
    unittest.main()
