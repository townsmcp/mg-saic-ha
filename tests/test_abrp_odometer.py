"""ABRP gets the odometer the Mileage sensor shows (#456, @joaommarques).

MG4 Electric (EH32MY25 S), 9 Nov 2026: basicVehicleStatus.mileage is a 16-bit
field and had stuck at 65535 (6553.5 km). The Mileage sensor fell back to the
charging data's odometer (9,304 km), but ABRP was sent the stuck figure, so
it showed 6,554 km for days.
"""

import unittest
from types import SimpleNamespace as NS

import test_setup_and_config_flow as harness

ABRP = harness._load("mg_saic.abrp", harness.PKG_DIR / "abrp.py")


def _odometer(status_raw, charging_raw=None, charging=True):
    basic = NS(mileage=status_raw)
    info = NS(rvsChargeStatus=NS(mileage=charging_raw)) if charging else None
    return ABRP.AbrpApi._odometer(basic, info).get("odometer")


class Issue456Tests(unittest.TestCase):
    def test_the_report(self):
        self.assertEqual(_odometer(65535, 93040), 9304.0)

    def test_saturated_with_no_charging_data_sends_nothing(self):
        # Better no odometer than a stuck one.
        self.assertIsNone(_odometer(65535, charging=False))


class UnchangedTests(unittest.TestCase):
    def test_below_the_cap_uses_the_status(self):
        self.assertEqual(_odometer(64470, 64470), 6447.0)

    def test_status_missing_uses_the_charging_data(self):
        self.assertEqual(_odometer(-128, 64470), 6447.0)
        self.assertEqual(_odometer(None, 64470), 6447.0)

    def test_nothing_usable(self):
        self.assertIsNone(_odometer(-128, -128))
        self.assertIsNone(_odometer(0, charging=False))


if __name__ == "__main__":
    unittest.main()
