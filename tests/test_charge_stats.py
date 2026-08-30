# File: tests/test_charge_stats.py
"""Unit tests for the charge-session maths in trip_stats (#262).

Covers the Last Charge Energy feature: how much energy a charge put *into*
the battery, which the API has no field for (there is no
``lastChargeStartingPower`` to subtract from ``lastChargeEndingPower``), so
it has to be measured across the session.

Imports only the pure functions and the manager's session bookkeeping, both
of which are free of Home Assistant deps — matching python-tests.yaml CI.
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


ts = _load("mg_saic_charge_stats_under_test", PKG_DIR / "trip_stats.py")
CSnap = ts.ChargeSnapshot


def csnap(soc=None, pack=None, odo=None, t="2026-08-29T22:00:00+00:00"):
    return CSnap(ts=t, soc_pct=soc, pack_energy_kwh=pack, odometer_km=odo)


class TestComputeChargeSession(unittest.TestCase):
    def test_soc_based_energy_added(self):
        start = csnap(soc=36.9, t="2026-08-28T18:30:00+00:00")
        end = csnap(soc=80.0, t="2026-08-28T23:38:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=74.3)
        self.assertIsNotNone(charge)
        self.assertEqual(charge["soc_added_pct"], 43.1)
        # 43.1 % of 74.3 kWh
        self.assertAlmostEqual(charge["energy_added_kWh"], 32.023, places=3)
        self.assertEqual(charge["method"], "soc")
        self.assertEqual(charge["duration_s"], 18480)

    def test_counter_figure_exposed_alongside_soc(self):
        start = csnap(soc=50.0, pack=30.0)
        end = csnap(soc=80.0, pack=52.0, t="2026-08-29T23:00:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=74.3)
        self.assertAlmostEqual(charge["energy_added_kWh_soc"], 22.29, places=2)
        self.assertAlmostEqual(charge["energy_added_kWh_counter"], 22.0, places=2)
        # SOC is the headline figure; the counter rides along for comparison.
        self.assertEqual(charge["method"], "soc")
        self.assertEqual(charge["energy_added_kWh"], charge["energy_added_kWh_soc"])

    def test_counter_used_when_soc_unavailable(self):
        start = csnap(pack=10.0)
        end = csnap(pack=18.5, t="2026-08-29T23:00:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=24.7)
        self.assertEqual(charge["method"], "counter")
        self.assertAlmostEqual(charge["energy_added_kWh"], 8.5, places=2)

    def test_stale_ending_power_is_rejected(self):
        """If the car hasn't refreshed lastChargeEndingPower yet the pack-energy
        delta is <= 0 — that must not be reported as a charge."""
        start = csnap(soc=50.0, pack=40.0)
        end = csnap(soc=80.0, pack=40.0, t="2026-08-29T23:00:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=74.3)
        self.assertNotIn("energy_added_kWh_counter", charge)
        self.assertEqual(charge["method"], "soc")

    def test_counter_larger_than_pack_is_rejected(self):
        start = csnap(pack=10.0)
        end = csnap(pack=200.0, t="2026-08-29T23:00:00+00:00")
        self.assertIsNone(
            ts.compute_charge_session(start, end, capacity_kwh=24.7)
        )

    def test_trivial_soc_rise_is_not_a_charge(self):
        """The pack rebounds a fraction of a percent after a drive — that is
        not a charge and must not produce an energy figure."""
        start = csnap(soc=66.0)
        end = csnap(soc=66.2, t="2026-08-29T23:00:00+00:00")
        self.assertIsNone(
            ts.compute_charge_session(start, end, capacity_kwh=74.3)
        )

    def test_no_capacity_falls_back_to_counter(self):
        start = csnap(soc=40.0, pack=12.0)
        end = csnap(soc=90.0, pack=24.0, t="2026-08-29T23:00:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=None)
        self.assertEqual(charge["method"], "counter")
        self.assertAlmostEqual(charge["energy_added_kWh"], 12.0, places=2)

    def test_average_power(self):
        start = csnap(soc=50.0, t="2026-08-29T20:00:00+00:00")
        end = csnap(soc=60.0, t="2026-08-29T22:00:00+00:00")
        charge = ts.compute_charge_session(start, end, capacity_kwh=74.0)
        # 7.4 kWh over 2 h
        self.assertAlmostEqual(charge["average_power_kW"], 3.7, places=2)


class _Manager(ts.TripStatsManager):
    """Manager with persistence/event plumbing bypassed for the pure logic."""

    def __init__(self):
        self.open_charge = None
        self.last_charge = None


class TestNoteChargeState(unittest.TestCase):
    def setUp(self):
        self.m = _Manager()

    def _note(self, charging, snapshot, now="2026-08-29T23:00:00+00:00"):
        return self.m.note_charge_state(
            charging, snapshot, capacity_kwh=74.3, now_iso=now
        )

    def test_full_session_open_and_close(self):
        charge, changed = self._note(
            True, csnap(soc=40.0, t="2026-08-29T20:00:00+00:00")
        )
        self.assertIsNone(charge)
        self.assertTrue(changed)
        self.assertIsNotNone(self.m.open_charge)

        charge, changed = self._note(
            False, csnap(soc=80.0, t="2026-08-29T23:00:00+00:00")
        )
        self.assertIsNotNone(charge)
        self.assertTrue(changed)
        self.assertIsNone(self.m.open_charge)
        self.assertEqual(self.m.last_charge, charge)
        self.assertAlmostEqual(charge["energy_added_kWh"], 29.72, places=2)

    def test_start_snapshot_is_not_overwritten_mid_charge(self):
        self._note(True, csnap(soc=40.0, t="2026-08-29T20:00:00+00:00"))
        charge, changed = self._note(
            True, csnap(soc=60.0, t="2026-08-29T21:30:00+00:00")
        )
        self.assertIsNone(charge)
        self.assertFalse(changed)
        self.assertEqual(self.m.open_charge.soc_pct, 40.0)

    def test_missing_snapshot_is_ignored(self):
        """A poll with no usable reading must not open or close anything —
        this is the charging-endpoint dropout guard."""
        self._note(True, csnap(soc=40.0, t="2026-08-29T20:00:00+00:00"))
        charge, changed = self._note(False, None)
        self.assertIsNone(charge)
        self.assertFalse(changed)
        self.assertIsNotNone(self.m.open_charge)

    def test_not_charging_with_no_open_session_is_a_no_op(self):
        charge, changed = self._note(False, csnap(soc=66.0))
        self.assertIsNone(charge)
        self.assertFalse(changed)

    def test_stale_open_session_is_abandoned(self):
        self._note(True, csnap(soc=40.0, t="2026-08-25T20:00:00+00:00"))
        charge, changed = self._note(
            False, csnap(soc=80.0, t="2026-08-29T23:00:00+00:00")
        )
        self.assertIsNone(charge)
        self.assertTrue(changed)
        self.assertIsNone(self.m.open_charge)
        self.assertIsNone(self.m.last_charge)

    def test_plug_in_that_delivered_nothing_records_no_charge(self):
        self._note(True, csnap(soc=66.0, t="2026-08-29T20:00:00+00:00"))
        charge, changed = self._note(
            False, csnap(soc=66.1, t="2026-08-29T21:00:00+00:00")
        )
        self.assertIsNone(charge)
        self.assertTrue(changed)
        self.assertIsNone(self.m.last_charge)

    def test_snapshot_roundtrips_through_storage(self):
        snap = csnap(soc=40.0, pack=30.0, odo=12345.6)
        restored = CSnap.from_dict(snap.to_dict())
        self.assertEqual(restored, snap)
        self.assertIsNone(CSnap.from_dict(None))


if __name__ == "__main__":
    unittest.main()
