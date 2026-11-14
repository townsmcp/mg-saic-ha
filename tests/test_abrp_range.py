"""ABRP gets the range the Electric Range sensor shows (#478, @tabannis).

MG IM5 (P12L), debug log 9 Nov 2026, battery at 59%: the charging data's
rvsChargeStatus.fuelRangeElec was 3050 (305 km, the DFRE estimate the car's
dashboard and Electric Range show) and the status's basicVehicleStatus
fuelRangeElec 4200 (420 km, WLTP-based). ABRP was sent the higher, 420 km.
"""

import unittest
from types import SimpleNamespace as NS

import test_setup_and_config_flow as harness

ABRP = harness._load("mg_saic.abrp", harness.PKG_DIR / "abrp.py")


def _range(status_raw, charging_raw=None, imcu=None, charging=True):
    basic = NS(fuelRangeElec=status_raw)
    info = None
    if charging:
        info = NS(
            rvsChargeStatus=NS(fuelRangeElec=charging_raw),
            chrgMgmtData=NS(imcuVehElecRng=imcu),
        )
    api = ABRP.AbrpApi.__new__(ABRP.AbrpApi)
    return api._electric_range(basic, info).get("est_battery_range")


class Issue478Tests(unittest.TestCase):
    def test_the_log(self):
        self.assertEqual(_range(4200, 3050), 305.0)

    def test_was_the_higher_one_before(self):
        # Guard against going back to max(): the WLTP figure must not win.
        self.assertNotEqual(_range(4200, 3050), 420.0)


class FallbackTests(unittest.TestCase):
    def test_no_charging_data_uses_the_status(self):
        self.assertEqual(_range(4200, charging=False), 420.0)

    def test_charging_figure_missing_uses_the_status(self):
        self.assertEqual(_range(4200, -128), 420.0)
        self.assertEqual(_range(4200, 0), 420.0)

    def test_hs_phev_gets_a_range_now(self):
        # fuelRangeElec is -128 in both places on an HS PHEV (AS33P), so
        # nothing used to be sent. imcuVehElecRng is what Electric Range shows.
        self.assertEqual(_range(-128, -128, imcu=111), 111.0)

    def test_nothing_usable(self):
        self.assertIsNone(_range(-128, -128))
        self.assertIsNone(_range(None, charging=False))

    def test_same_figure_either_place_unchanged(self):
        self.assertEqual(_range(3050, 3050), 305.0)


if __name__ == "__main__":
    unittest.main()
