"""Follow the charger all day, not just during Quiet Charging (#488).

@HarryFlatter's HS PHEV on Intelligent Octopus Go, 22 Nov 2026: outside a
charge the integration polled hourly, and a dispatch charge from 20:31 to
20:55 fell between polls at 19:46 and 21:01. His zappi was picked as the
charger entity, but it was only followed during Quiet Charging (23:00 to
06:00). With "Charger status: follow all day" ticked, it's followed at any
time: one live poll when it starts charging, one once it has stopped.
"""

import asyncio
import sys
import unittest
from datetime import time
from unittest.mock import MagicMock, patch

import test_quiet_charging_charger as base

COORD_MOD = base.COORD_MOD
QH = base.QH
CF = base.CF
_event = base._event


def _daytime(*, all_day=True, quiet_hours=True, **kwargs):
    """A coordinator at 20:31, outside 23:00-06:00 quiet hours."""
    c = base._coordinator(**kwargs)
    c.quiet_hours = quiet_hours
    c.quiet_hours_start = time(23, 0)
    c.quiet_hours_end = time(6, 0)
    c.live_polling = True  # outside the window
    c.charger_all_day = all_day
    return c


def _run(coro):
    return asyncio.run(coro)


class StartTests(unittest.TestCase):
    def test_harrys_2031_dispatch_gets_a_poll(self):
        c = _daytime()
        _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_awaited_once()

    def test_without_the_tick_nothing_changes(self):
        c = _daytime(all_day=False)
        _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_works_without_quiet_charging(self):
        c = _daytime(quiet_hours=False)
        _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_awaited_once()

    def test_not_while_the_car_is_on(self):
        c = _daytime()
        c.is_powered_on = True
        _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_not_when_already_known_charging(self):
        c = _daytime(charging=True)
        _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_not_in_fully_quiet_holiday_mode(self):
        c = _daytime()
        with patch.object(
            type(c), "holiday_fully_quiet_active", new_callable=lambda: property(lambda s: True)
        ):
            _run(c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_still_works_in_quiet_hours(self):
        c = _daytime()
        c.live_polling = False
        _run(c._handle_charger_change(_event("Waiting for EV", "Charging")))
        c.async_request_refresh.assert_awaited_once()


class StopTests(unittest.TestCase):
    def setUp(self):
        self.later = MagicMock(return_value=MagicMock())
        self.patch = patch.object(COORD_MOD, "async_call_later", self.later)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_harrys_2055_stop_gets_a_poll_two_minutes_later(self):
        c = _daytime(charger_state="EV Connected", charging=True)
        _run(c._handle_charger_change(_event("Charging", "EV Connected")))
        c.async_request_refresh.assert_not_awaited()
        self.assertEqual(self.later.call_args.args[1], 120)
        _run(c._handle_charger_stopped())
        c.async_request_refresh.assert_awaited_once()

    def test_without_the_tick_no_stop_poll(self):
        c = _daytime(all_day=False, charger_state="EV Connected", charging=True)
        _run(c._handle_charger_change(_event("Charging", "EV Connected")))
        self.later.assert_not_called()


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.track = MagicMock(return_value=MagicMock())
        self.patch = patch.object(COORD_MOD, "async_track_state_change_event", self.track)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_followed_with_only_the_all_day_tick(self):
        c = _daytime(quiet_hours=False)
        c._watch_charger()
        self.track.assert_called_once()

    def test_not_followed_with_neither(self):
        c = _daytime(all_day=False, quiet_hours=False)
        c._watch_charger()
        self.track.assert_not_called()

    def test_not_followed_with_no_charger_picked(self):
        c = _daytime(entity=None)
        c._watch_charger()
        self.track.assert_not_called()

    def test_charger_state_reads_with_only_the_all_day_tick(self):
        c = _daytime(quiet_hours=False)
        self.assertTrue(c._charger_charging())


class OptionTests(unittest.TestCase):
    def test_option_key(self):
        self.assertEqual(QH.CONF_CHARGER_ALL_DAY, "charger_entity_all_day")

    def test_translations(self):
        import json
        from pathlib import Path

        folder = Path(base.__file__).resolve().parents[1] / "custom_components" / "mg_saic" / "translations"
        for path in sorted(folder.glob("*.json")):
            step = json.loads(path.read_text())["options"]["step"]["init"]
            self.assertIn("charger_entity_all_day", step["data"], path.name)
            self.assertIn("charger_entity_all_day", step["data_description"], path.name)


if __name__ == "__main__":
    unittest.main()
