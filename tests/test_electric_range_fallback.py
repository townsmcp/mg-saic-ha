"""Electric Range without charging data, and library debug logging (#398).

A Cyberster (EC32) whose charging data never arrived showed Electric Range as
Unknown, although its main status carried a real, live fuelRangeElec
(3180 -> 3070, i.e. 318 -> 307 km, across a drive). Cars whose fuelRangeElec
is flagged unreliable (it's sometimes the -128 sentinel) read Electric Range
only from the charging data's imcuVehElecRng. They now fall back to a real
status fuelRangeElec when there's no charging data.

Also: the manifest lists the SAIC libraries as the integration's loggers, so
Home Assistant's "Enable debug logging" includes SAIC's actual replies.
"""

import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from test_india_soc import SENSOR

REPO_ROOT = Path(__file__).resolve().parent.parent


def _sensor(*, status_range=None, imcu=None, reliable=False, charging=True):
    vin_info = NS(vin="VIN1", brandName="MG", modelName="Cyberster")
    basic = NS(fuelRangeElec=status_range)
    data = {"status": NS(basicVehicleStatus=basic), "charging": None}
    if charging:
        data["charging"] = NS(
            chrgMgmtData=NS(imcuVehElecRng=imcu, bmsEstdElecRng=450),
            rvsChargeStatus=NS(fuelRangeElec=0),
        )
    coordinator = NS(
        data=data,
        vin_info=vin_info,
        last_update_success=True,
        reliable_fuel_range_elec=reliable,
    )
    return SENSOR.SAICMGElectricRangeSensor(
        coordinator,
        NS(entry_id="entry-1"),
        "Electric Range",
        "fuelRangeElec",
        "basicVehicleStatus",
        "rvsChargeStatus",
        None,
        "km",
        "mdi:car-electric",
        "measurement",
        0.1,
        "status",
    )


class UnreliableProfileFallbackTests(unittest.TestCase):
    """Profiles with reliable_fuel_range_elec=False: EC32, AS33P."""

    def test_charging_data_missing_uses_status_range(self):
        # #398 at 13:11: charging data None, status fuelRangeElec=3180.
        sensor = _sensor(status_range=3180, charging=False)
        self.assertAlmostEqual(sensor.native_value, 318.0)

    def test_it_tracks_driving(self):
        # ...and 3070 after the drive at 18:46.
        sensor = _sensor(status_range=3070, charging=False)
        self.assertAlmostEqual(sensor.native_value, 307.0)

    def test_charging_data_still_preferred_when_present(self):
        sensor = _sensor(status_range=3180, imcu=320)
        self.assertEqual(sensor.native_value, 320.0)

    def test_charging_data_without_a_live_range_falls_back(self):
        sensor = _sensor(status_range=3180, imcu=-128)
        self.assertAlmostEqual(sensor.native_value, 318.0)

    def test_sentinel_status_value_is_never_used(self):
        # The HS PHEV always sends -128 there: nothing changes for it.
        sensor = _sensor(status_range=-128, charging=False)
        self.assertIsNone(sensor.native_value)

    def test_zero_status_value_is_never_used(self):
        sensor = _sensor(status_range=0, charging=False)
        self.assertIsNone(sensor.native_value)

    def test_last_real_value_is_held_when_both_sources_fail(self):
        sensor = _sensor(status_range=3180, charging=False)
        self.assertAlmostEqual(sensor.native_value, 318.0)
        sensor.coordinator.data["status"].basicVehicleStatus.fuelRangeElec = -128
        self.assertAlmostEqual(sensor.native_value, 318.0)


class ReliableProfileUnchangedTests(unittest.TestCase):
    def test_status_range_used_as_before(self):
        sensor = _sensor(status_range=3180, imcu=999, reliable=True, charging=False)
        self.assertAlmostEqual(sensor.native_value, 318.0)


class ManifestLoggersTests(unittest.TestCase):
    """HA's "Enable debug logging" button covers these loggers too."""

    def test_saic_libraries_are_listed(self):
        manifest = json.loads(
            (REPO_ROOT / "custom_components/mg_saic/manifest.json").read_text()
        )
        self.assertIn("saic_ismart_client_ng", manifest.get("loggers", []))
        self.assertIn("mg_ismart_india_client", manifest.get("loggers", []))

    def test_manifest_key_order_is_valid_for_hassfest(self):
        # hassfest: domain, name, then the rest alphabetically.
        keys = list(
            json.loads(
                (REPO_ROOT / "custom_components/mg_saic/manifest.json").read_text()
            )
        )
        self.assertEqual(keys[:2], ["domain", "name"])
        self.assertEqual(keys[2:], sorted(keys[2:]))


if __name__ == "__main__":
    unittest.main()
