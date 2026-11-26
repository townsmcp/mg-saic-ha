"""The car says "charging" but the battery is already full (#488).

@HarryFlatter's HS PHEV on Intelligent Octopus Go, 22 Nov 2026. The 21:01
poll came back:

    bmsChrgSts 3 (charging), bmsPackSOCDsp 1000 (100%), no target (PHEV),
    bmsPackCrnt 19995 (0.25 A: nothing going in), chrgngRmnngTime 1023
    with chrgngRmnngTimeV 1 (no time from the car),
    rvsChargeStatus startTime 20:31:22, endTime 20:55:19

The estimate needs power going in, so Remaining Charging Time went to
unknown and Charging Completion Time kept the afternoon's 15:32. A full
battery has nothing left to charge: Remaining 0, and Completion the time the
car says the charge ended.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
ENTRY = NS(entry_id="entry1")
UTC = timezone.utc

POLL = datetime(2026, 11, 22, 21, 1, 37, tzinfo=UTC)
STARTED = int(datetime(2026, 11, 22, 20, 31, 22, tzinfo=UTC).timestamp())
ENDED = int(datetime(2026, 11, 22, 20, 55, 19, tzinfo=UTC).timestamp())


def _charging(soc_tenths=1000, status=3, current=19995, start=STARTED, end=ENDED, **extra):
    values = dict(
        bmsChrgSts=status,
        chrgngRmnngTime=1023,
        chrgngRmnngTimeV=1,
        bmsPackSOCDsp=soc_tenths,
        bmsOnBdChrgTrgtSOCDspCmd=0,
        bmsPackCrnt=current,
        bmsPackVol=1657,
    )
    values.update(extra)
    return NS(
        chrgMgmtData=NS(**values),
        rvsChargeStatus=NS(startTime=start, endTime=end),
    )


def _coordinator(charging):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.vin_info = NS(vin="VIN1", brandName="MG", modelName="HS")
    c.last_update_success = True
    c.charging_capacity_correction = None
    c.data = {"status": None, "charging": charging}
    c.resolve_battery_capacity_for = lambda _cd: (23.2, "profile")
    c.charging_freshness = NS(last_success=POLL)
    return c


def _remaining(c):
    return SENSOR.SAICMGChargingSensor(
        c, ENTRY, "Remaining Charging Time", "chrgngRmnngTime",
        None, None, None, None, 1.0, "chrgMgmtData", "charging",
    )


class CoordinatorTests(unittest.TestCase):
    def test_harrys_2101_poll_is_full(self):
        c = _coordinator(_charging())
        self.assertTrue(c.charging_but_full())
        self.assertEqual(c.estimated_remaining_charge(), timedelta(0))

    def test_below_the_target_is_not(self):
        # 97.4%, as at his 15:32 poll.
        c = _coordinator(_charging(soc_tenths=974))
        self.assertFalse(c.charging_but_full())

    def test_below_the_target_with_nothing_going_in_is_still_unknown(self):
        # Not full, and no power to estimate from: unknown, as before.
        c = _coordinator(_charging(soc_tenths=974))
        self.assertIsNone(c.estimated_remaining_charge())

    def test_at_a_set_target(self):
        # An EV with an 80% target, at 80%.
        c = _coordinator(_charging(soc_tenths=800, bmsOnBdChrgTrgtSOCDspCmd=3))
        target = c._target_soc_pct(c.data["charging"])
        if target == 80.0:
            self.assertTrue(c.charging_but_full())
        else:  # the code table changed: at least don't call 80% of 100 full
            self.assertEqual(c.charging_but_full(), 80.0 >= target)

    def test_no_charging_data(self):
        self.assertFalse(_coordinator(None).charging_but_full())


class RemainingChargingTimeTests(unittest.TestCase):
    def test_harrys_2101_poll_shows_0(self):
        s = _remaining(_coordinator(_charging()))
        self.assertEqual(s.native_value, 0)
        self.assertEqual(s.extra_state_attributes, {"source": "full"})

    def test_not_full_with_nothing_going_in_is_still_unknown(self):
        s = _remaining(_coordinator(_charging(soc_tenths=974)))
        self.assertIsNone(s.native_value)
        self.assertEqual(s.extra_state_attributes, {"source": "unknown"})

    def test_the_cars_own_figure_still_wins(self):
        c = _coordinator(_charging())
        c.data["charging"].chrgMgmtData.chrgngRmnngTime = 5
        c.data["charging"].chrgMgmtData.chrgngRmnngTimeV = 0
        s = _remaining(c)
        self.assertEqual(s.native_value, 5)
        self.assertEqual(s.extra_state_attributes, {"source": "car"})

    def test_not_charging_is_unchanged(self):
        # Plugged in, not charging (Harry's 15:42-19:46 polls): 0, as before.
        s = _remaining(_coordinator(_charging(status=5)))
        self.assertEqual(s.native_value, 0)


class CompletionTimeTests(unittest.TestCase):
    def _sensor(self, charging):
        c = _coordinator(charging)
        return SENSOR.SAICMGChargingCompletionTimeSensor(c, ENTRY), c

    def test_harrys_2101_poll_shows_when_it_ended(self):
        sensor, _ = self._sensor(_charging())
        self.assertEqual(sensor.native_value, datetime(2026, 11, 22, 20, 55, tzinfo=UTC))
        self.assertEqual(
            sensor.extra_state_attributes, {"charging": True, "source": "full"}
        )

    def test_replaces_the_afternoons_finish_time(self):
        # 15:01: charging, 30 minutes left -> 15:32. 21:01: full -> 20:55.
        afternoon = _charging(
            soc_tenths=915, status=1, current=19800,
            start=int(datetime(2026, 11, 22, 15, 1, 26, tzinfo=UTC).timestamp()),
            end=int(datetime(2026, 11, 22, 13, 5, 10, tzinfo=UTC).timestamp()),
            chrgngRmnngTime=30, chrgngRmnngTimeV=0,
        )
        sensor, c = self._sensor(afternoon)
        c.charging_freshness = NS(last_success=datetime(2026, 11, 22, 15, 1, 51, tzinfo=UTC))
        self.assertEqual(sensor.native_value, datetime(2026, 11, 22, 15, 32, tzinfo=UTC))
        c.data["charging"] = _charging()
        c.charging_freshness = NS(last_success=POLL)
        self.assertEqual(sensor.native_value, datetime(2026, 11, 22, 20, 55, tzinfo=UTC))

    def test_full_with_no_end_time_for_this_charge_uses_the_reading(self):
        # The end time is from before this charge started: not this one's.
        sensor, _ = self._sensor(_charging(end=STARTED - 3600))
        self.assertEqual(sensor.native_value, datetime(2026, 11, 22, 21, 2, tzinfo=UTC))
        self.assertEqual(sensor.extra_state_attributes["source"], "full")

    def test_not_full_with_nothing_going_in_keeps_the_last(self):
        sensor, c = self._sensor(_charging(soc_tenths=974))
        self.assertIsNone(sensor.native_value)
        self.assertEqual(sensor.extra_state_attributes["source"], "unknown")


if __name__ == "__main__":
    unittest.main()
