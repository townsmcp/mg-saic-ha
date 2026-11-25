"""Quiet Charging all day: equal start and end times (#269, @Kappa71).

MG4 Urban, Quiet Charging set to 00:00 - 00:00 so the integration would never
wake the car (each wake closes the HV contactors and cycles the grille
shutter). It still woke the car every midnight: equal times meant "no window",
yet both timers fired at 00:00 -- the end one turned Quiet Charging off with a
catch-up live poll, the start one turned it straight back on. In the other
order it would have been left off, polling as normal all day.

From 2.0.0-beta08 equal times mean all day: no timers, Quiet Charging on, and
the switch still turns it off and on by hand.
"""

import asyncio
import unittest
from datetime import datetime, time
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import test_quiet_hours as base

COORD_MOD = base.COORD_MOD
QH = base.QH
NOW = base.NOW
MIDNIGHT = time(0, 0)


def _start(local_time, *, restore=False, options=None, start=MIDNIGHT, end=MIDNIGHT):
    c = base._coordinator()
    c.quiet_hours = True
    c.quiet_hours_start = start
    c.quiet_hours_end = end
    c.hass = MagicMock()
    c.config_entry = NS(options=options or {})
    tracker = MagicMock(return_value=MagicMock())
    with patch.object(COORD_MOD, "async_track_time_change", tracker), patch.object(
        COORD_MOD, "local_now", lambda: datetime.combine(NOW.date(), local_time, tzinfo=NOW.tzinfo)
    ):
        c._start_quiet_hours(restore=restore)
    return c, tracker


class HelperTests(unittest.TestCase):
    def test_equal_times_are_all_day(self):
        self.assertTrue(QH.quiet_all_day(MIDNIGHT, MIDNIGHT))
        self.assertTrue(QH.quiet_all_day(time(22, 0), time(22, 0)))
        self.assertFalse(QH.quiet_all_day(time(22, 0), time(7, 0)))

    def test_every_time_of_day_is_inside(self):
        for hour in range(24):
            self.assertTrue(QH.in_quiet_window(time(hour, 30), MIDNIGHT, MIDNIGHT), hour)

    def test_nothing_ever_flips_it(self):
        now = datetime(2026, 11, 25, 11, 0, tzinfo=NOW.tzinfo)
        self.assertIsNone(QH.last_scheduled_flip(now, MIDNIGHT, MIDNIGHT))

    def test_after_a_restart_it_stays_as_left(self):
        now = datetime(2026, 11, 25, 11, 0, tzinfo=NOW.tzinfo)
        switched_off_by_hand = datetime(2026, 11, 24, 18, 0, tzinfo=NOW.tzinfo)
        self.assertTrue(
            QH.live_polling_after_restart(True, switched_off_by_hand, now, MIDNIGHT, MIDNIGHT)
        )
        self.assertFalse(
            QH.live_polling_after_restart(False, switched_off_by_hand, now, MIDNIGHT, MIDNIGHT)
        )

    def test_after_a_restart_with_nothing_saved_it_is_quiet(self):
        now = datetime(2026, 11, 25, 11, 0, tzinfo=NOW.tzinfo)
        self.assertFalse(QH.live_polling_after_restart(None, None, now, MIDNIGHT, MIDNIGHT))


class CoordinatorTests(unittest.TestCase):
    def test_no_timers_so_no_midnight_wake(self):
        c, tracker = _start(time(12, 0))
        tracker.assert_not_called()
        self.assertEqual(c._quiet_unsubs, [])

    def test_quiet_from_the_moment_the_times_are_set(self):
        c, _ = _start(time(12, 0))
        self.assertFalse(c.live_polling)
        self.assertTrue(c.quiet_active)

    def test_any_equal_time_not_just_midnight(self):
        c, tracker = _start(time(3, 0), start=time(22, 0), end=time(22, 0))
        tracker.assert_not_called()
        self.assertFalse(c.live_polling)

    def test_switched_off_by_hand_survives_a_restart(self):
        c, _ = _start(
            time(12, 0),
            restore=True,
            options={
                "quiet_hours_live_polling": True,
                "quiet_hours_live_polling_at": "2026-10-08T22:30:00+00:00",
            },
        )
        self.assertTrue(c.live_polling)

    def test_a_normal_window_still_has_its_two_timers(self):
        _, tracker = _start(time(12, 0), start=time(22, 0), end=time(7, 0))
        self.assertEqual(tracker.call_count, 2)


class SwitchTests(unittest.TestCase):
    def _attrs(self, start, end):
        from test_charging_controls_dropout import MODS  # loads the platforms

        switch_mod = MODS["switch"]
        c = NS(quiet_charger_entity=None, quiet_finish_check_at=None, quiet_last_stored=None)
        c.quiet_hours_start, c.quiet_hours_end = start, end
        c.quiet_charger_entity = None
        c._quiet_finish_check_at = None
        sw = switch_mod.SAICMGQuietChargingSwitch.__new__(switch_mod.SAICMGQuietChargingSwitch)
        sw.coordinator = c
        return sw.extra_state_attributes

    def test_all_day_is_shown(self):
        self.assertTrue(self._attrs(MIDNIGHT, MIDNIGHT)["all_day"])

    def test_not_shown_for_a_window(self):
        self.assertNotIn("all_day", self._attrs(time(22, 0), time(7, 0)))


if __name__ == "__main__":
    unittest.main()
