"""Pauses nobody saw are counted, not shown as 0 s (#460).

@HarryFlatter, 2 Nov 2026: his zappi kept pausing and restarting the charge
overnight (a myenergi comms fault), with Quiet Charging on, so there was one
live poll at the start and one at the end. Last Charge showed 1 interruption
and 0 s paused however many there had been. The car's record showed the
charge had restarted after the last poll that saw it charging, but not when
it had stopped, so the length of the pause wasn't known: 0 s looked like a
measurement. Such pauses are now counted in ``unmeasured_pauses``; the
duration includes them.
"""

import unittest
from unittest.mock import MagicMock

from test_charge_restarts import (
    HARRYS_POLLS, STRETCH_1, STRETCH_3, CHARGE_END, _at, _replay, _snap,
)
from test_charge_stats import ts as TRIP


class SessionTests(unittest.TestCase):
    def test_quiet_charging_start_and_end_only(self):
        # Poll at the start, poll after the end: the restarts in between
        # left only the last stretch's start in the record.
        polls = [HARRYS_POLLS[0], HARRYS_POLLS[1], HARRYS_POLLS[5]]
        charge, _ = _replay(polls)
        self.assertEqual(charge["interruptions"], 1)
        self.assertEqual(charge["paused_s"], 0)
        self.assertEqual(charge["unmeasured_pauses"], 1)
        # Start to end, the unseen pauses included.
        self.assertEqual(charge["duration_s"], CHARGE_END - STRETCH_1)

    def test_every_pause_seen(self):
        charge, _ = _replay(HARRYS_POLLS)
        self.assertEqual(charge["interruptions"], 2)
        self.assertEqual(charge["paused_s"], 74)
        self.assertNotIn("unmeasured_pauses", charge)

    def test_one_seen_one_not(self):
        polls = HARRYS_POLLS[:4] + [HARRYS_POLLS[5]]
        charge, _ = _replay(polls)
        self.assertEqual(charge["interruptions"], 2)
        self.assertEqual(charge["paused_s"], 38)
        self.assertEqual(charge["unmeasured_pauses"], 1)

    def test_no_restarts(self):
        polls = [HARRYS_POLLS[0], HARRYS_POLLS[1], HARRYS_POLLS[5]]
        polls[2] = (polls[2][0], False, 100.0, STRETCH_1, CHARGE_END)
        charge, _ = _replay(polls)
        self.assertNotIn("interruptions", charge)
        self.assertNotIn("unmeasured_pauses", charge)


class TrackerTests(unittest.TestCase):
    def test_a_restart_without_the_stretch_end(self):
        tracker, _ = TRIP.track_charge_record(None, _snap(_at(1, 23, 49), 68.7, STRETCH_1, None))
        # Next poll: a later start, and an end that isn't between the two.
        tracker, changed = TRIP.track_charge_record(
            tracker, _snap(_at(2, 1, 21, 17), 99.8, STRETCH_3, None)
        )
        self.assertTrue(changed)
        self.assertEqual(tracker["interruptions"], 1)
        self.assertEqual(tracker["unmeasured_pauses"], 1)
        self.assertEqual(tracker["paused_s"], 0.0)


class SensorAttributeTests(unittest.TestCase):
    def test_shown_on_last_charge_duration_and_session(self):
        import sys
        from types import SimpleNamespace as NS

        import test_setup_and_config_flow  # noqa: F401
        from test_erac_fallback import SENSOR

        polls = [HARRYS_POLLS[0], HARRYS_POLLS[1], HARRYS_POLLS[5]]
        charge, manager = _replay(polls)
        manager.last_charge = charge
        coordinator = NS(
            vin_info=NS(vin="VIN1", brandName="MG", modelName="HS"),
            trip_stats=manager,
        )
        entry = NS(entry_id="entry1")
        self.assertIn("unmeasured_pauses", SENSOR.SAICMGLastChargeDurationSensor._CHARGE_ATTR_KEYS)
        session = SENSOR.SAICMGChargeSessionDurationSensor(coordinator, entry)
        attrs = session.extra_state_attributes
        self.assertEqual(attrs["interruptions"], 1)
        self.assertEqual(attrs["unmeasured_pauses"], 1)


if __name__ == "__main__":
    unittest.main()
