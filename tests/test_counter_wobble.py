"""Since-charge counters that wobble down a little are not resets (#436).

@Heiko-Schneider's MG4, 14-16 Oct 2026, never charged in between: Power Usage
Since Last Charge now and then dipped by 0.1 kWh (25.0 -> 24.9) and came
back. Each dip was taken as a phantom reset (#262) and the whole previous
figure added on top: nine dips, 297.9 kWh shown for the car's 45.0, so
Efficiency Since Last Charge read 0.75 km/kWh for 223 km. Mileage never
dipped and was right throughout.

Raw values are in tenths, as SAIC sends them.
"""

import unittest

from test_erac_fallback import LOGIC
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

START = 1791369235  # his last real charge record, 7 Oct
BASE = dict(ending=724, start=START, end=START + 9000, soc=70.0, odo=153820,
            plugged=False)


def _r(km, kwh, **extra):
    return dict(BASE, km=km, kwh=kwh, **extra)


# His readings around each dip (km, kWh in tenths).
HEIKOS_DAYS = [
    (820, 171), (830, 170),            # 14 Oct 14:54
    (930, 191), (940, 190),            # 14 Oct 15:22
    (1060, 221), (1070, 220),          # 15 Oct 08:45
    (1210, 250), (1210, 249),          # 15 Oct 09:20
    (1220, 250), (1220, 249),          # 15 Oct 09:23
    (1280, 263), (1290, 262),          # 15 Oct 09:39
    (1540, 329), (1550, 328),          # 16 Oct 09:08
    (1850, 390), (1860, 389),          # 16 Oct 09:52
    (2160, 451), (2170, 450),          # 16 Oct 12:48
]


class HeikosMG4Tests(unittest.TestCase):
    def setUp(self):
        self.guard = LOGIC.SinceChargeCounterGuard()
        self.shown, self.events = [], []
        for i, (km, kwh) in enumerate(HEIKOS_DAYS):
            adjusted, event, _ = self.guard.process(f"t{i}", _r(km, kwh))
            self.shown.append(adjusted["kwh"])
            self.events.append(event)

    def test_no_reset_is_seen(self):
        self.assertEqual([e for e in self.events if e], [])
        self.assertFalse(self.guard.holding)
        self.assertEqual(self.guard.ignored_count, 0)

    def test_the_figure_ends_at_the_cars(self):
        # 45.1 held over the last dip to 45.0, not 297.9.
        self.assertEqual(self.shown[-1], 451)

    def test_it_never_goes_down(self):
        self.assertEqual(self.shown, sorted(self.shown))

    def test_and_catches_up_with_the_car(self):
        adjusted, _, _ = self.guard.process("later", _r(2200, 460))
        self.assertEqual(adjusted["kwh"], 460)

    def test_mileage_untouched(self):
        adjusted, _, _ = self.guard.process("later", _r(2200, 460))
        self.assertEqual(adjusted["km"], 2200)


class RealResetsStillWorkTests(unittest.TestCase):
    def test_a_phantom_reset_to_zero_is_still_held(self):
        guard = LOGIC.SinceChargeCounterGuard()
        guard.process("t0", _r(6100, 266))
        adjusted, event, _ = guard.process("t1", _r(0, 0, start=0, end=START + 99))
        self.assertEqual(event, "ignored")
        self.assertEqual((adjusted["km"], adjusted["kwh"]), (6100, 266))

    def test_a_real_charge_still_resets(self):
        guard = LOGIC.SinceChargeCounterGuard()
        guard.process("t0", _r(2170, 450))
        adjusted, event, _ = guard.process("t1", _r(0, 0, plugged=True))
        self.assertIsNone(event)
        self.assertEqual((adjusted["km"], adjusted["kwh"]), (0, 0))

    def test_a_small_figure_falling_to_zero_is_a_reset(self):
        guard = LOGIC.SinceChargeCounterGuard()
        guard.process("t0", _r(20, 4))
        adjusted, _, _ = guard.process("t1", _r(0, 0, plugged=True))
        self.assertEqual(adjusted["kwh"], 0)


class RepairTests(unittest.TestCase):
    def test_heikos_saved_hold_is_cleared(self):
        # What 1.3.0 saved for him: 9 dips folded into the energy offset only.
        saved = {"last": _r(2170, 450), "offset_km": 0, "offset_kwh": 2519,
                 "held_ending": None, "charge_seen": False,
                 "ignored_at": "2026-10-16T10:48:38+00:00", "ignored_count": 9,
                 "baseline_odo": 151650}
        guard = LOGIC.SinceChargeCounterGuard.from_dict(saved)
        self.assertTrue(guard.repair_one_sided_hold())
        self.assertFalse(guard.holding)
        adjusted, _, _ = guard.process("next", _r(2180, 452))
        self.assertEqual(adjusted["kwh"], 452)

    def test_a_real_phantom_hold_is_kept(self):
        saved = {"offset_km": 6100, "offset_kwh": 266}
        guard = LOGIC.SinceChargeCounterGuard.from_dict(saved)
        self.assertFalse(guard.repair_one_sided_hold())
        self.assertEqual((guard.offset_km, guard.offset_kwh), (6100, 266))

    def test_nothing_to_repair(self):
        self.assertFalse(LOGIC.SinceChargeCounterGuard.from_dict(None).repair_one_sided_hold())


if __name__ == "__main__":
    unittest.main()
