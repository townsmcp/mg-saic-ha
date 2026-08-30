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


if __name__ == "__main__":
    unittest.main()
