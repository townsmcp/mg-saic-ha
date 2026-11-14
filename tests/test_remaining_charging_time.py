"""Remaining Charging Time when the car has no time of its own.

@HarryFlatter's HS PHEV, 3 Nov 2026, Quiet Charging with his zappi as the
charger entity:

    00:32:29  zappi says charging; one live poll
    00:32:37  the car: charging (bmsChrgSts 1) since 00:32:29, 23.1 %,
              chrgngRmnngTime 1023 with chrgngRmnngTimeV 1 ("no value yet")
              Quiet Charging: "should finish charging in 2:55:16"

Remaining Charging Time showed 1023 minutes all night, and it shows 1023
after every finished charge too. Now: our estimate while charging, 0 when not.
"""

import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
QH = sys.modules["mg_saic.quiet_hours"]
ENTRY = NS(entry_id="entry1")


def _chrg(status=1, remaining=1023, valid=1, **extra):
    values = dict(
        bmsChrgSts=status,
        chrgngRmnngTime=remaining,
        chrgngRmnngTimeV=valid,
        bmsPackSOCDsp=231,
        bmsOnBdChrgTrgtSOCDspCmd=0,
        bmsPackCrnt=19696,  # 15.2 A into the pack...
        bmsPackVol=1607,  # ...at 401.75 V: 6.1 kW
    )
    values.update(extra)
    return NS(chrgMgmtData=NS(**values), rvsChargeStatus=NS())


def _coordinator(charging):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.vin_info = NS(vin="VIN1", brandName="MG", modelName="HS")
    c.last_update_success = True
    c.charging_capacity_correction = None
    c.data = {"status": None, "charging": charging}
    c.resolve_battery_capacity_for = lambda _cd: (23.2, "profile")
    return c


def _sensor(c):
    return SENSOR.SAICMGChargingSensor(
        c, ENTRY, "Remaining Charging Time", "chrgngRmnngTime",
        None, None, None, None, 1.0, "chrgMgmtData", "charging",
    )


class CarFigureTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(QH.car_remaining_minutes(188, 0), 188.0)
        self.assertEqual(QH.car_remaining_minutes(188, None), 188.0)
        self.assertEqual(QH.car_remaining_minutes(0, 0), 0.0)

    def test_no_value(self):
        for minutes, flag in ((1023, 1), (1023, 0), (300, 1), (-128, 0), (None, 0), (True, 0)):
            self.assertIsNone(QH.car_remaining_minutes(minutes, flag), (minutes, flag))


class SensorTests(unittest.TestCase):
    def test_harrys_0032_poll(self):
        c = _coordinator(_chrg())
        s = _sensor(c)
        # 76.9 % of 23.2 kWh at 6.1 kW: 2 h 55 min, as Quiet Charging said.
        self.assertEqual(s.native_value, 175)
        self.assertEqual(s.extra_state_attributes, {"source": "estimated"})

    def test_the_cars_own_figure_wins(self):
        s = _sensor(_coordinator(_chrg(remaining=188, valid=0)))
        self.assertEqual(s.native_value, 188)
        self.assertEqual(s.extra_state_attributes, {"source": "car"})

    def test_after_the_charge(self):
        # Finished, plugged in, stopped or waiting for its slot: 0, not 1023.
        for status in (2, 7, 8, 9):
            s = _sensor(_coordinator(_chrg(status=status)))
            self.assertEqual(s.native_value, 0, status)
            self.assertEqual(s.extra_state_attributes, {"source": "not_charging"})

    def test_unplugged_is_still_0(self):
        s = _sensor(_coordinator(_chrg(status=0)))
        self.assertEqual(s.native_value, 0)

    def test_charging_but_no_way_to_tell(self):
        # No power reading yet: unknown, not the last charge's figure.
        c = _coordinator(_chrg(remaining=188, valid=0))
        s = _sensor(c)
        self.assertEqual(s.native_value, 188)
        c.data["charging"] = _chrg(bmsPackCrnt=20000)
        self.assertIsNone(s.native_value)
        self.assertEqual(s.extra_state_attributes, {"source": "unknown"})

    def test_drop_out_mid_charge_holds(self):
        c = _coordinator(_chrg())
        s = _sensor(c)
        self.assertEqual(s.native_value, 175)
        c.data["charging"] = None
        self.assertEqual(s.native_value, 175)


class EstimateTests(unittest.TestCase):
    def test_matches_quiet_charging(self):
        c = _coordinator(_chrg())
        estimate = c.estimated_remaining_charge()
        self.assertAlmostEqual(estimate / timedelta(minutes=1), 175.3, delta=0.2)

    def test_nothing_to_go_on(self):
        self.assertIsNone(_coordinator(None).estimated_remaining_charge())


if __name__ == "__main__":
    unittest.main()
