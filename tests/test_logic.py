"""Unit tests for pure integration logic."""

from datetime import timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "mg_saic"
    / "logic.py"
)
SPEC = importlib.util.spec_from_file_location("mg_saic_logic", MODULE_PATH)
LOGIC = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LOGIC)


class NormalizeSunroofActionTests(unittest.TestCase):
    def test_accepts_boolean_true(self):
        self.assertEqual(LOGIC.normalize_sunroof_action(True), (True, "open"))

    def test_accepts_boolean_false(self):
        self.assertEqual(LOGIC.normalize_sunroof_action(False), (False, "close"))

    def test_accepts_string(self):
        self.assertEqual(LOGIC.normalize_sunroof_action("open"), (True, "open"))

    def test_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            LOGIC.normalize_sunroof_action("tilt")


class BuildVehicleOptionsTests(unittest.TestCase):
    def test_uses_model_name_and_masks_vin_in_label(self):
        vin = "LSJW74096NZ123456"
        vehicle = SimpleNamespace(vin=vin, modelName="New ZS", series="ZS")

        options = LOGIC.build_vehicle_options([vehicle])

        self.assertEqual(options, {vin: "New ZS (…23456)"})

    def test_uses_series_when_model_name_is_missing(self):
        vin = "LSJW74096NZ654321"
        vehicle = SimpleNamespace(vin=vin, modelName="", series="E230")

        options = LOGIC.build_vehicle_options([vehicle])

        self.assertEqual(options, {vin: "E230 (…54321)"})

    def test_falls_back_to_vin_without_model_metadata(self):
        vin = "LSJW74096NZ111111"
        vehicle = SimpleNamespace(vin=vin, modelName="", series="")

        options = LOGIC.build_vehicle_options([vehicle])

        self.assertEqual(options, {vin: vin})

    def test_accepts_plain_vin_strings(self):
        vin = "LSJW74096NZ222222"

        options = LOGIC.build_vehicle_options([vin])

        self.assertEqual(options, {vin: vin})


class SelectUpdateIntervalTests(unittest.TestCase):
    def setUp(self):
        self.default_interval = timedelta(minutes=30)
        self.powered_interval = timedelta(minutes=1)
        self.charging_interval = timedelta(minutes=5)
        self.grace_interval = timedelta(minutes=10)
        self.after_shutdown_interval = timedelta(minutes=20)

    def select_interval(self, **kwargs):
        return LOGIC.select_update_interval(
            default_update_interval=self.default_interval,
            powered_update_interval=self.powered_interval,
            charging_update_interval=self.charging_interval,
            grace_period_update_interval=self.grace_interval,
            after_shutdown_update_interval=self.after_shutdown_interval,
            **kwargs,
        )

    def test_prefers_powered_interval(self):
        interval = self.select_interval(
            is_powered_on=True,
            is_charging=False,
            idle_duration=timedelta(hours=1),
            activity_duration=timedelta(hours=1),
        )
        self.assertEqual(interval, self.powered_interval)

    def test_prefers_charging_interval(self):
        interval = self.select_interval(
            is_powered_on=False,
            is_charging=True,
            idle_duration=timedelta(hours=1),
            activity_duration=timedelta(hours=1),
        )
        self.assertEqual(interval, self.charging_interval)

    def test_uses_grace_period_interval_for_recent_activity(self):
        interval = self.select_interval(
            is_powered_on=False,
            is_charging=False,
            idle_duration=timedelta(hours=1),
            activity_duration=timedelta(minutes=5),
        )
        self.assertEqual(interval, self.grace_interval)

    def test_uses_after_shutdown_interval_before_default(self):
        interval = self.select_interval(
            is_powered_on=False,
            is_charging=False,
            idle_duration=timedelta(minutes=15),
            activity_duration=timedelta(hours=1),
        )
        self.assertEqual(interval, self.after_shutdown_interval)

    def test_preserves_user_default_interval_when_idle(self):
        interval = self.select_interval(
            is_powered_on=False,
            is_charging=False,
            idle_duration=timedelta(hours=1),
            activity_duration=timedelta(hours=1),
        )
        self.assertEqual(interval, self.default_interval)


class ApplyEnergyCorrectionTests(unittest.TestCase):
    """The ~3x energy inflation correction (#262, #310).

    Regression cover for a fix that was silently doing nothing: the correction
    existed and was the right number, but sat in a code path
    powerUsageSinceLastCharge never took, so the sensor kept reporting the raw
    figure. Pinning the field list here means a future refactor that drops a
    field fails loudly.
    """

    def test_corrects_power_usage_since_last_charge(self):
        # Harry's HS PHEV: 20.20 kWh reported, ~6.73 kWh real (#262).
        self.assertAlmostEqual(
            LOGIC.apply_energy_correction("powerUsageSinceLastCharge", 20.20, 1 / 3),
            6.733,
            places=3,
        )

    def test_corrects_last_charge_ending_power(self):
        self.assertAlmostEqual(
            LOGIC.apply_energy_correction("lastChargeEndingPower", 72.5, 1 / 3),
            24.167,
            places=3,
        )

    def test_leaves_uncorrected_fields_alone(self):
        # Distance is never inflated, only the energy fields.
        self.assertEqual(
            LOGIC.apply_energy_correction("mileageSinceLastCharge", 38.6, 1 / 3), 38.6
        )

    def test_no_correction_configured_is_a_passthrough(self):
        self.assertEqual(
            LOGIC.apply_energy_correction("powerUsageSinceLastCharge", 10.0, None), 10.0
        )

    def test_missing_value_stays_none(self):
        self.assertIsNone(
            LOGIC.apply_energy_correction("powerUsageSinceLastCharge", None, 1 / 3)
        )

    def test_zero_is_corrected_not_treated_as_missing(self):
        self.assertEqual(
            LOGIC.apply_energy_correction("powerUsageSinceLastCharge", 0.0, 1 / 3), 0.0
        )


class OdometerKmTests(unittest.TestCase):
    """Odometer source preference and fallback order (#262).

    The charging-data fallback used to read chrgMgmtData, which carries no
    mileage field at all, so it could never fire — and nothing noticed because
    the logic wasn't reachable from a test. It is now.
    """

    FACTOR = 0.1
    SATURATION = 65535

    def _odo(self, basic=None, charging=None):
        return LOGIC.odometer_km(
            basic, charging, factor=self.FACTOR, saturation=self.SATURATION
        )

    def test_prefers_basic_vehicle_status(self):
        basic = SimpleNamespace(mileage=123456)
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(mileage=999))
        self.assertAlmostEqual(self._odo(basic, charging), 12345.6)

    def test_falls_back_to_rvs_charge_status(self):
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(mileage=123456))
        self.assertAlmostEqual(self._odo(None, charging), 12345.6)

    def test_chrg_mgmt_data_carries_no_odometer(self):
        # The original bug: chrgMgmtData has no mileage field, so a fallback
        # pointed at it yields nothing.
        charging = SimpleNamespace(chrgMgmtData=SimpleNamespace(bmsPackSOCDsp=661))
        self.assertIsNone(self._odo(None, charging))

    def test_rejects_zero_and_negative(self):
        self.assertIsNone(self._odo(SimpleNamespace(mileage=0)))
        self.assertIsNone(self._odo(SimpleNamespace(mileage=-128)))

    def test_rejects_uint16_saturation(self):
        self.assertIsNone(self._odo(SimpleNamespace(mileage=self.SATURATION)))

    def test_saturated_basic_status_falls_through_to_charging(self):
        basic = SimpleNamespace(mileage=self.SATURATION)
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(mileage=123456))
        self.assertAlmostEqual(self._odo(basic, charging), 12345.6)

    def test_no_data_at_all(self):
        self.assertIsNone(self._odo(None, None))


class ElectricRangeKmTests(unittest.TestCase):
    """Electric range extraction for the charge-session range delta (#262)."""

    FACTOR = 0.1

    def _range(self, basic=None, charging=None):
        return LOGIC.electric_range_km(basic, charging, factor=self.FACTOR)

    def test_prefers_charging_block(self):
        basic = SimpleNamespace(fuelRangeElec=500)
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(fuelRangeElec=750))
        self.assertEqual(self._range(basic, charging), 75.0)

    def test_falls_back_to_basic_status(self):
        self.assertEqual(self._range(SimpleNamespace(fuelRangeElec=530)), 53.0)

    def test_rejects_the_parked_sentinel(self):
        basic = SimpleNamespace(fuelRangeElec=-128)
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(fuelRangeElec=-128))
        self.assertIsNone(self._range(basic, charging))

    def test_sentinel_in_charging_block_falls_through(self):
        basic = SimpleNamespace(fuelRangeElec=530)
        charging = SimpleNamespace(rvsChargeStatus=SimpleNamespace(fuelRangeElec=-128))
        self.assertEqual(self._range(basic, charging), 53.0)

    def test_zero_range_is_a_real_value(self):
        # A flat pack genuinely has no range left; that is not missing data.
        self.assertEqual(self._range(SimpleNamespace(fuelRangeElec=0)), 0.0)

    def test_falls_back_to_imcu_vehicle_range(self):
        """Models flagged reliable_fuel_range_elec: False never give a usable
        fuelRangeElec — @HarryFlatter's PHEV reported imcuVehElecRng=83 with
        the estimate fields at 0. Without this, everything derived from range
        silently produces nothing on those cars."""
        charging = SimpleNamespace(
            rvsChargeStatus=SimpleNamespace(fuelRangeElec=-128),
            chrgMgmtData=SimpleNamespace(imcuVehElecRng=83),
        )
        self.assertEqual(self._range(SimpleNamespace(fuelRangeElec=-128), charging), 83.0)

    def test_imcu_range_is_whole_km_not_tenths(self):
        # imcuVehElecRng 257 == fuelRangeElec 2570 on a car reporting both, so
        # the decimal correction must not be applied to it.
        charging = SimpleNamespace(
            rvsChargeStatus=SimpleNamespace(fuelRangeElec=None),
            chrgMgmtData=SimpleNamespace(imcuVehElecRng=257),
        )
        self.assertEqual(self._range(None, charging), 257.0)

    def test_fuel_range_elec_still_wins_when_usable(self):
        charging = SimpleNamespace(
            rvsChargeStatus=SimpleNamespace(fuelRangeElec=2570),
            chrgMgmtData=SimpleNamespace(imcuVehElecRng=999),
        )
        self.assertEqual(self._range(None, charging), 257.0)

    def test_zero_imcu_range_is_not_used(self):
        # 0 here means "not reported", unlike fuelRangeElec where a flat pack
        # genuinely has no range.
        charging = SimpleNamespace(
            rvsChargeStatus=SimpleNamespace(fuelRangeElec=-128),
            chrgMgmtData=SimpleNamespace(imcuVehElecRng=0),
        )
        self.assertIsNone(self._range(None, charging))

    def test_nothing_available(self):
        self.assertIsNone(self._range(None, None))
class ResolveBatteryCapacityTests(unittest.TestCase):
    """Capacity precedence and the API-tier guards (#262, #302).

    The precedence was always documented as override > profile > API, and the
    Total Battery Capacity sensor implemented all three — but the attribute
    the energy maths read only ever saw the first two, so unprofiled cars got
    a populated capacity sensor next to blank derived sensors.
    """

    FACTOR = 0.1

    def _resolve(self, override=None, profile=None, api_raw=None, derived=None):
        return LOGIC.resolve_battery_capacity(
            override, profile, api_raw, factor=self.FACTOR, derived_kwh=derived
        )

    def test_user_override_wins_over_everything(self):
        self.assertEqual(
            self._resolve(override=23.2, profile=64.0, api_raw=725),
            (23.2, "user_override"),
        )

    def test_profile_wins_over_api(self):
        self.assertEqual(
            self._resolve(profile=23.2, api_raw=725), (23.2, "profile")
        )

    def test_falls_back_to_api_when_unprofiled(self):
        # The gap this closes: an unprofiled car reporting a real capacity.
        self.assertEqual(self._resolve(api_raw=383), (38.3, "api"))

    def test_rejects_the_placeholder(self):
        # 725 -> 72.5 kWh is a documented placeholder, not a pack size, and is
        # plausible enough that a range check alone would let it through.
        self.assertEqual(self._resolve(api_raw=725), (None, None))

    def test_placeholder_still_overridden_by_profile_and_user(self):
        self.assertEqual(self._resolve(profile=23.2, api_raw=725)[1], "profile")
        self.assertEqual(
            self._resolve(override=24.7, api_raw=725)[1], "user_override"
        )

    def test_rejects_implausible_magnitudes(self):
        self.assertEqual(self._resolve(api_raw=1)[0], None)      # 0.1 kWh
        self.assertEqual(self._resolve(api_raw=50000)[0], None)  # 5000 kWh

    def test_nothing_available_yields_no_source(self):
        self.assertEqual(self._resolve(), (None, None))

    def test_source_is_reported_not_inferred(self):
        # An API-derived value must not be labelled "profile".
        self.assertEqual(self._resolve(api_raw=383)[1], "api")

    def test_derived_fills_the_gap_when_the_api_reports_nothing(self):
        # The India frame has no totalBatteryCapacity field at all, so this is
        # the tier that gives those cars a capacity.
        self.assertEqual(self._resolve(derived=37.1), (37.1, "derived"))

    def test_derived_sits_below_every_other_tier(self):
        self.assertEqual(self._resolve(override=40.0, derived=37.1)[1], "user_override")
        self.assertEqual(self._resolve(profile=38.0, derived=37.1)[1], "profile")
        self.assertEqual(self._resolve(api_raw=383, derived=37.1), (38.3, "api"))

    def test_rejected_api_value_falls_through_to_derived(self):
        # The placeholder and out-of-band values are refused, not preferred to
        # a figure the car's own energy reading supports.
        self.assertEqual(self._resolve(api_raw=725, derived=37.1), (37.1, "derived"))
        self.assertEqual(self._resolve(api_raw=50000, derived=37.1), (37.1, "derived"))

    def test_derived_is_range_checked_like_the_api_tier(self):
        self.assertEqual(self._resolve(derived=0.4), (None, None))
        self.assertEqual(self._resolve(derived=5000.0), (None, None))


class DeriveBatteryCapacityTests(unittest.TestCase):
    """energy / SOC as a capacity of last resort (#302).

    A profile keyed on a series code is mis-keyed whenever one code covers two
    pack sizes, and every owner of the smaller variant then gets a capacity
    that is confidently wrong. This tier cannot be: the car reports its own
    energy, so a bigger pack answers with a bigger number at the same SOC.
    """

    def _d(self, energy, soc):
        return LOGIC.derive_battery_capacity_kwh(energy, soc)

    def test_same_pack_from_readings_across_the_soc_range(self):
        # The same car at three widely separated states of charge has to
        # resolve to one pack size, within the reading's own precision.
        self.assertAlmostEqual(self._d(33.1, 89.0), 37.2, places=1)
        self.assertAlmostEqual(self._d(23.4, 63.0), 37.1, places=1)
        self.assertAlmostEqual(self._d(16.6, 45.0), 36.9, places=1)

    def test_withheld_below_the_soc_floor(self):
        # Quantisation dominates as SOC falls, and the BMS estimate is least
        # trustworthy there — a blank beats a confident wrong pack size.
        self.assertIsNone(self._d(6.1, 12.0))
        self.assertIsNone(self._d(0.4, 1.0))

    def test_soc_floor_is_inclusive(self):
        self.assertAlmostEqual(self._d(9.3, 25.0), 37.2, places=1)

    def test_missing_inputs_yield_nothing(self):
        self.assertIsNone(self._d(None, 63.0))
        self.assertIsNone(self._d(23.4, None))

    def test_zero_energy_is_not_a_capacity(self):
        # A frame that has not populated the field yet reads 0, and 0/SOC
        # would otherwise resolve to a 0 kWh pack.
        self.assertIsNone(self._d(0.0, 63.0))
        self.assertIsNone(self._d(-1.0, 63.0))


class ProjectRangeAtTargetTests(unittest.TestCase):
    """Range projection used when the car won't estimate one itself (#262)."""

    def _p(self, rng, soc, target):
        return LOGIC.project_range_at_target(rng, soc, target)

    def test_matches_the_cars_own_estimate(self):
        # Real capture: 257 km at 51.7% SOC with an 80% target. The car
        # reported 410 km; we project 397.7 — within 3%.
        self.assertAlmostEqual(self._p(257, 51.7, 80), 397.7, places=1)

    def test_phev_with_no_target_projects_to_full(self):
        # A PHEV has no target SOC, so the charge runs to 100%.
        self.assertAlmostEqual(self._p(30.0, 50.0, 100), 60.0, places=1)

    def test_needs_no_battery_capacity(self):
        """Pure ratio work — a capacity override cannot skew it. Same inputs
        must give the same answer whatever pack the car has."""
        self.assertEqual(self._p(100, 50, 100), self._p(100, 50, 100))

    def test_rejects_low_soc_where_noise_dominates(self):
        self.assertIsNone(self._p(20, 5.0, 100))

    def test_accepts_soc_at_the_threshold(self):
        self.assertIsNotNone(self._p(40, LOGIC.MIN_SOC_PCT_FOR_RANGE_PROJECTION, 100))

    def test_target_below_current_soc_is_not_projected(self):
        # Already past the target — there is nothing to project forward to.
        self.assertIsNone(self._p(300, 90.0, 80))

    def test_zero_soc_does_not_divide_by_zero(self):
        self.assertIsNone(self._p(100, 0.0, 100))

    def test_missing_inputs(self):
        self.assertIsNone(self._p(None, 50, 80))
        self.assertIsNone(self._p(100, None, 80))
        self.assertIsNone(self._p(100, 50, None))

    def test_rejects_nonsense_targets(self):
        self.assertIsNone(self._p(100, 50, 0))
        self.assertIsNone(self._p(100, 50, 150))

    def test_zero_range_is_not_projected(self):
        self.assertIsNone(self._p(0, 50, 100))

    def test_target_soc_codes_map_to_percentages(self):
        self.assertEqual(LOGIC.TARGET_SOC_PERCENT_BY_CODE[5], 80)
        self.assertEqual(LOGIC.TARGET_SOC_PERCENT_BY_CODE[7], 100)


if __name__ == "__main__":
    unittest.main()
