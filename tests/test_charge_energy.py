# File: tests/test_charge_energy.py
"""Charge-session energy and since-charge correction tests (#262).

Covers the three defects reported by HarryFlatter (MG HS PHEV, series AS33P)
plus the new Last Charge Energy Added feature:

1. powerUsageSinceLastCharge never had the per-model energy correction applied
   — the correction lived in a branch that field never reaches, so HS PHEV
   owners still saw a ~3x figure after #310.
2. Efficiency Since Charge (SOC) sat on Unknown forever, because the odometer
   fallback searched chrgMgmtData for a `mileage` field that only exists on
   rvsChargeStatus.
3. Nothing in the API reports energy put INTO the battery, so a charge session
   has to be measured as it happens.

Like the rest of the suite these import only the pure modules, so they run in
plain CPython with no Home Assistant stubs.
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


ts = _load("mg_saic_trip_stats_charge_test", PKG_DIR / "trip_stats.py")
logic = _load("mg_saic_logic_charge_test", PKG_DIR / "logic.py")

compute_charge_session = ts.compute_charge_session
MAX_OPEN_CHARGE_MINUTES = ts.MAX_OPEN_CHARGE_MINUTES

# Mirrors const.DATA_DECIMAL_CORRECTION / MILEAGE_UINT16_SATURATION, which live
# in const.py (which imports Home Assistant, so it can't be loaded here).
FACTOR = 0.1
SATURATION = 65535


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class TestComputeChargeSession(unittest.TestCase):
    """The pure SOC-rise -> energy maths."""

    def test_soc_rise_gives_energy_added(self):
        result = compute_charge_session(
            20.0, 80.0, 74.3,
            start_ts="2026-08-29T00:00:00+00:00",
            end_ts="2026-08-29T02:00:00+00:00",
        )
        self.assertIsNotNone(result)
        # 60% of 74.3 kWh
        self.assertAlmostEqual(result["energy_added_kWh"], 44.58, places=2)
        self.assertEqual(result["soc_added_pct"], 60.0)
        self.assertEqual(result["duration_min"], 120.0)
        self.assertAlmostEqual(result["average_power_kW"], 22.29, places=2)

    def test_phev_capacity_gives_plausible_figure(self):
        """Harry's HS PHEV: 23.2 kWh usable, charged 40% -> 100%."""
        result = compute_charge_session(40.0, 100.0, 23.2)
        self.assertAlmostEqual(result["energy_added_kWh"], 13.92, places=2)

    def test_no_soc_rise_is_none(self):
        self.assertIsNone(compute_charge_session(80.0, 80.0, 74.3))
        self.assertIsNone(compute_charge_session(80.0, 72.0, 74.3))

    def test_missing_inputs_are_none(self):
        self.assertIsNone(compute_charge_session(None, 80.0, 74.3))
        self.assertIsNone(compute_charge_session(20.0, None, 74.3))
        self.assertIsNone(compute_charge_session(20.0, 80.0, None))
        self.assertIsNone(compute_charge_session(20.0, 80.0, 0))

    def test_counter_cross_check_when_all_values_present(self):
        """Pack held (ending_prev - used) at the start, ending_new at the end."""
        result = compute_charge_session(
            50.0, 90.0, 20.0,
            ending_power_kwh=19.0,          # this charge ended with 19 kWh in
            start_ending_power_kwh=18.0,    # previous charge ended at 18 kWh
            start_power_usage_kwh=9.0,      # 9 kWh used since -> 9 kWh at start
        )
        self.assertEqual(result["energy_added_kWh_counter"], 10.0)
        self.assertEqual(result["ending_pack_energy_kWh"], 19.0)

    def test_counter_cross_check_omitted_when_counters_absent(self):
        """Cars that never populate the counters (e.g. some MGS5s)."""
        result = compute_charge_session(50.0, 90.0, 20.0, ending_power_kwh=19.0)
        self.assertNotIn("energy_added_kWh_counter", result)
        # The SOC figure is still reported.
        self.assertEqual(result["energy_added_kWh"], 8.0)

    def test_negative_counter_result_is_dropped(self):
        """A spurious counter reset must not produce a nonsense figure."""
        result = compute_charge_session(
            50.0, 90.0, 20.0,
            ending_power_kwh=5.0,
            start_ending_power_kwh=18.0,
            start_power_usage_kwh=0.0,
        )
        self.assertNotIn("energy_added_kWh_counter", result)
        self.assertEqual(result["energy_added_kWh"], 8.0)


class _Manager(ts.TripStatsManager):
    """TripStatsManager without HA — only the pure session logic is exercised."""

    def __init__(self):
        super().__init__(hass=None, entry_id="e", vin="V")


class TestNoteChargeState(unittest.TestCase):
    def test_session_opens_and_closes(self):
        m = _Manager()
        self.assertTrue(
            m.note_charge_state(
                True, 30.0, "2026-08-29T00:00:00+00:00", capacity_kwh=74.3
            )
        )
        self.assertIsNotNone(m.charge_open_snapshot)
        self.assertIsNone(m.last_charge)

        self.assertTrue(
            m.note_charge_state(
                False, 80.0, "2026-08-29T03:00:00+00:00", capacity_kwh=74.3
            )
        )
        self.assertIsNone(m.charge_open_snapshot)
        self.assertEqual(m.last_charge["soc_added_pct"], 50.0)
        self.assertAlmostEqual(m.last_charge["energy_added_kWh"], 37.15, places=2)

    def test_repeated_charging_polls_do_not_move_the_start(self):
        m = _Manager()
        m.note_charge_state(True, 30.0, "t0", capacity_kwh=74.3)
        # SOC climbing mid-charge must not drag the start baseline up with it.
        self.assertFalse(m.note_charge_state(True, 45.0, "t1", capacity_kwh=74.3))
        self.assertFalse(m.note_charge_state(True, 60.0, "t2", capacity_kwh=74.3))
        self.assertEqual(m.charge_open_snapshot["soc_pct"], 30.0)

    def test_lower_soc_mid_session_corrects_the_start(self):
        m = _Manager()
        m.note_charge_state(True, 32.0, "t0", capacity_kwh=74.3)
        self.assertTrue(m.note_charge_state(True, 30.0, "t1", capacity_kwh=74.3))
        self.assertEqual(m.charge_open_snapshot["soc_pct"], 30.0)

    def test_not_charging_with_no_open_session_is_a_no_op(self):
        m = _Manager()
        self.assertFalse(m.note_charge_state(False, 80.0, "t0", capacity_kwh=74.3))
        self.assertIsNone(m.last_charge)

    def test_stale_session_is_abandoned_not_recorded(self):
        """Open longer than the cap means the close was missed — discard it."""
        m = _Manager()
        m.note_charge_state(True, 30.0, "2026-08-01T00:00:00+00:00", capacity_kwh=74.3)
        self.assertTrue(
            m.note_charge_state(
                False, 90.0, "2026-08-04T12:00:00+00:00", capacity_kwh=74.3
            )
        )
        self.assertIsNone(m.charge_open_snapshot)
        self.assertIsNone(m.last_charge)

    def test_session_just_inside_the_cap_is_kept(self):
        m = _Manager()
        m.note_charge_state(True, 30.0, "2026-08-01T00:00:00+00:00", capacity_kwh=74.3)
        m.note_charge_state(False, 90.0, "2026-08-02T00:00:00+00:00", capacity_kwh=74.3)
        self.assertIsNotNone(m.last_charge)

    def test_previous_result_survives_a_session_that_yields_nothing(self):
        m = _Manager()
        m.note_charge_state(True, 30.0, "2026-08-29T00:00:00+00:00", capacity_kwh=74.3)
        m.note_charge_state(False, 80.0, "2026-08-29T03:00:00+00:00", capacity_kwh=74.3)
        good = dict(m.last_charge)
        # Cable in, nothing actually delivered.
        m.note_charge_state(True, 80.0, "2026-08-29T04:00:00+00:00", capacity_kwh=74.3)
        m.note_charge_state(False, 80.0, "2026-08-29T04:30:00+00:00", capacity_kwh=74.3)
        self.assertEqual(m.last_charge, good)

    def test_charging_without_soc_does_not_open_a_session(self):
        m = _Manager()
        self.assertFalse(m.note_charge_state(True, None, "t0", capacity_kwh=74.3))
        self.assertIsNone(m.charge_open_snapshot)


class TestEnergyCorrection(unittest.TestCase):
    """Regression tests for the correction that #310 intended but never applied."""

    def test_energy_fields_are_in_the_correction_set(self):
        self.assertIn("powerUsageSinceLastCharge", logic.ENERGY_CORRECTION_FIELDS)
        self.assertIn("lastChargeEndingPower", logic.ENERGY_CORRECTION_FIELDS)

    def test_correction_applied_to_power_usage(self):
        # Harry's reported 20.20 kWh raw -> the real ~6.73 kWh battery draw.
        self.assertAlmostEqual(
            logic.apply_energy_correction("powerUsageSinceLastCharge", 20.2, 1 / 3),
            6.733,
            places=3,
        )

    def test_distance_fields_are_never_corrected(self):
        self.assertEqual(
            logic.apply_energy_correction("mileageSinceLastCharge", 24.0, 1 / 3), 24.0
        )

    def test_models_without_a_correction_are_untouched(self):
        self.assertEqual(
            logic.apply_energy_correction("powerUsageSinceLastCharge", 20.2, None), 20.2
        )

    def test_missing_value_stays_none(self):
        self.assertIsNone(
            logic.apply_energy_correction("powerUsageSinceLastCharge", None, 1 / 3)
        )


class TestOdometerExtraction(unittest.TestCase):
    """The fallback that could never fire, and why Efficiency (SOC) was Unknown."""

    def _odo(self, basic, charging):
        return logic.odometer_km(
            basic, charging, factor=FACTOR, saturation=SATURATION
        )

    def test_prefers_basic_vehicle_status(self):
        basic = _Obj(mileage=25109)
        charging = _Obj(rvsChargeStatus=_Obj(mileage=1))
        self.assertAlmostEqual(self._odo(basic, charging), 2510.9, places=1)

    def test_falls_back_to_rvs_charge_status(self):
        """chrgMgmtData carries no `mileage`; rvsChargeStatus does."""
        charging = _Obj(rvsChargeStatus=_Obj(mileage=25109), chrgMgmtData=_Obj())
        self.assertAlmostEqual(self._odo(None, charging), 2510.9, places=1)

    def test_the_old_broken_path_returns_none(self):
        """Reproduces the bug: only chrgMgmtData present -> no odometer at all."""
        self.assertIsNone(self._odo(None, _Obj(chrgMgmtData=_Obj())))

    def test_rejects_zero_and_saturation(self):
        self.assertIsNone(self._odo(_Obj(mileage=0), None))
        self.assertIsNone(self._odo(_Obj(mileage=SATURATION), None))

    def test_saturated_primary_falls_through_to_charging_data(self):
        basic = _Obj(mileage=SATURATION)
        charging = _Obj(rvsChargeStatus=_Obj(mileage=25109))
        self.assertAlmostEqual(self._odo(basic, charging), 2510.9, places=1)

    def test_none_everywhere(self):
        self.assertIsNone(self._odo(None, None))


if __name__ == "__main__":
    unittest.main()
