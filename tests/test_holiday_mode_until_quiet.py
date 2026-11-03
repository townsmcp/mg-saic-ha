"""Holiday Mode: Until, Fully Quiet, and no post-shutdown checks (#460).

@HarryFlatter, 2 Nov 2026: Holiday Mode on while his HS PHEV was at the dealer
for a T-Box reset, expecting it to leave the car alone. Each time the dealer
started and stopped the car, the post-shutdown checks ran (live polls at 1, 3,
7, 15 and 25 minutes), and while it was on it was polled as normal. He asked
for a truly quiet mode, and a way to set when it ends, both within Holiday
Mode.
"""

import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charging_controls_dropout import MODS

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
LOGIC = sys.modules["mg_saic.logic"]
CONST = sys.modules["mg_saic.const"]
CF = sys.modules["mg_saic.config_flow"]
SWITCH = MODS["switch"]
UNTIL = CONST.CONF_HOLIDAY_MODE_UNTIL
QUIET = CONST.CONF_HOLIDAY_FULLY_QUIET


def _datetime_platform():
    class _Entity:
        def async_write_ha_state(self):
            pass

    mod = types.ModuleType("homeassistant.components.datetime")
    mod.DateTimeEntity = _Entity
    sys.modules["homeassistant.components.datetime"] = mod
    exc = sys.modules.get("homeassistant.exceptions")
    if exc is None or not hasattr(exc, "HomeAssistantError"):
        exc = exc or types.ModuleType("homeassistant.exceptions")
        exc.HomeAssistantError = type("HomeAssistantError", (Exception,), {})
        sys.modules["homeassistant.exceptions"] = exc
    return test_setup_and_config_flow._load(
        "mg_saic.datetime", test_setup_and_config_flow.PKG_DIR / "datetime.py", force=True
    )


DATETIME = _datetime_platform()


def _coordinator(options=None, *, holiday=True, fully_quiet=False):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.holiday_mode = holiday
    c.holiday_fully_quiet = fully_quiet
    c.holiday_mode_until = None
    c._holiday_until_unsub = None
    c.is_charging = False
    c.is_powered_on = False
    c.quiet_hours = False
    c.live_polling = True
    c._shutdown_refresh_task = None
    c.config_entry = NS(options=dict(options or {}))

    def update_entry(entry, options):
        entry.options = options

    c.hass = MagicMock()
    c.hass.config_entries.async_update_entry.side_effect = update_entry
    c.async_request_refresh = AsyncMock()
    return c


class ParseTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(
            LOGIC.parse_holiday_until("2026-11-07T18:00:00+00:00"),
            datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc),
        )
        # A naive time is taken as UTC; another zone is converted.
        self.assertEqual(
            LOGIC.parse_holiday_until("2026-11-07T19:00:00+01:00"),
            datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc),
        )
        for raw in (None, "", "nonsense", 42):
            self.assertIsNone(LOGIC.parse_holiday_until(raw), raw)


class FullyQuietTests(unittest.TestCase):
    def test_only_inside_holiday_mode(self):
        self.assertTrue(_coordinator(fully_quiet=True).holiday_fully_quiet_active)
        self.assertFalse(_coordinator(holiday=False, fully_quiet=True).holiday_fully_quiet_active)
        self.assertFalse(_coordinator(fully_quiet=False).holiday_fully_quiet_active)

    def test_scheduled_update_never_reaches_the_car(self):
        # Even with the car switched on and charging: someone else has it.
        c = _coordinator(fully_quiet=True)
        c.is_powered_on = True
        c.is_charging = True
        c._scheduled_refresh = True
        c.data = {"status": "last"}
        c._run_update_cycle = AsyncMock()
        c._adjust_update_interval = MagicMock()
        c._consecutive_update_failures = 0
        self.assertEqual(asyncio.run(c._async_update_data()), {"status": "last"})
        c._run_update_cycle.assert_not_awaited()
        c._adjust_update_interval.assert_called_once()

    def test_update_vehicle_data_still_does(self):
        c = _coordinator(fully_quiet=True)
        c._scheduled_refresh = False
        c.quiet_hours = False
        c._run_update_cycle = AsyncMock(return_value={"status": "new"})
        c._consecutive_update_failures = 0
        self.assertEqual(asyncio.run(c._async_update_data()), {"status": "new"})

    def test_saic_messages_dont_trigger_a_poll(self):
        c = _coordinator(fully_quiet=True)
        asyncio.run(c.async_trigger_refresh("engine start"))
        c.async_request_refresh.assert_not_awaited()
        c = _coordinator(fully_quiet=False)
        asyncio.run(c.async_trigger_refresh("engine start"))
        c.async_request_refresh.assert_awaited_once()

    def test_interval_is_the_holiday_one(self):
        c = _coordinator(fully_quiet=True)
        c._action_interval_active = False
        c.holiday_update_interval = timedelta(hours=12)
        c.default_update_interval = timedelta(minutes=30)
        c.last_powered_off_time = c.last_vehicle_activity = None
        c._schedule_refresh = MagicMock()
        c.is_powered_on = True
        c._adjust_update_interval()
        self.assertEqual(c.update_interval, timedelta(hours=12))
        c._schedule_refresh.assert_called_once()

    def test_persisted(self):
        c = _coordinator(fully_quiet=False)
        asyncio.run(c.async_set_holiday_fully_quiet(True))
        self.assertTrue(c.holiday_fully_quiet)
        self.assertIs(c.config_entry.options[QUIET], True)


class ShutdownChecksTests(unittest.TestCase):
    def _run(self, c):
        async def sleep(_delay):
            pass

        with patch.object(COORD_MOD.asyncio, "sleep", sleep):
            asyncio.run(c._run_shutdown_refresh_sequence())
        return c.async_request_refresh.await_count

    def test_not_started_in_holiday_mode(self):
        c = _coordinator()
        c.config_entry.async_create_background_task = MagicMock()
        c._start_shutdown_refresh_sequence()
        c.config_entry.async_create_background_task.assert_not_called()

    def test_stopped_if_holiday_mode_turns_on(self):
        self.assertEqual(self._run(_coordinator()), 0)

    def test_as_before_without_holiday_mode(self):
        self.assertEqual(
            self._run(_coordinator(holiday=False)),
            len(COORD_MOD.POST_SHUTDOWN_REFRESH_SEQUENCE),
        )


class UntilTests(unittest.TestCase):
    def test_set_in_the_future(self):
        c = _coordinator()
        until = datetime.now(timezone.utc) + timedelta(days=3)
        with patch.object(COORD_MOD, "async_track_point_in_utc_time") as track:
            asyncio.run(c.async_set_holiday_until(until))
        self.assertEqual(c.holiday_mode_until, until)
        self.assertEqual(c.config_entry.options[UNTIL], until.isoformat())
        self.assertEqual(track.call_args.args[2], until)

    def test_past_refused(self):
        c = _coordinator()
        with self.assertRaises(ValueError):
            asyncio.run(c.async_set_holiday_until(datetime.now(timezone.utc) - timedelta(minutes=1)))
        self.assertNotIn(UNTIL, c.config_entry.options)

    def test_reached_turns_holiday_mode_off_and_clears_it(self):
        c = _coordinator({"holiday_mode": True, UNTIL: "2026-11-07T18:00:00+00:00"})
        c.holiday_mode_until = LOGIC.parse_holiday_until("2026-11-07T18:00:00+00:00")
        asyncio.run(c._holiday_until_reached())
        self.assertFalse(c.holiday_mode)
        self.assertIsNone(c.holiday_mode_until)
        self.assertIs(c.config_entry.options["holiday_mode"], False)
        self.assertNotIn(UNTIL, c.config_entry.options)

    def test_turning_holiday_mode_off_clears_it(self):
        c = _coordinator({"holiday_mode": True, UNTIL: "2026-11-07T18:00:00+00:00"})
        c.holiday_mode_until = LOGIC.parse_holiday_until("2026-11-07T18:00:00+00:00")
        asyncio.run(c.async_set_holiday_mode(False))
        self.assertIsNone(c.holiday_mode_until)
        self.assertNotIn(UNTIL, c.config_entry.options)

    def test_turning_holiday_mode_on_keeps_it(self):
        c = _coordinator({UNTIL: "2026-11-07T18:00:00+00:00"}, holiday=False)
        c.holiday_mode_until = LOGIC.parse_holiday_until("2026-11-07T18:00:00+00:00")
        asyncio.run(c.async_set_holiday_mode(True))
        self.assertIsNotNone(c.holiday_mode_until)
        self.assertIn(UNTIL, c.config_entry.options)

    def test_passed_while_home_assistant_was_down(self):
        c = _coordinator()
        c.holiday_mode_until = datetime.now(timezone.utc) - timedelta(hours=1)
        c._arm_holiday_until()
        c.hass.async_create_task.assert_called_once()
        c.hass.async_create_task.call_args.args[0].close()

    def test_options_keep_them_on_a_configure_save(self):
        self.assertIn(UNTIL, CF.RUNTIME_OPTION_KEYS)
        self.assertIn(QUIET, CF.RUNTIME_OPTION_KEYS)


class EntityTests(unittest.TestCase):
    def _coord(self):
        c = _coordinator(fully_quiet=True)
        c.vin_info = NS(vin="VIN1", brandName="MG", modelName="HS")
        return c

    def test_until_entity(self):
        c = self._coord()
        entity = DATETIME.SAICMGHolidayModeUntil(c, NS(entry_id="e1"), c.vin_info, "VIN1")
        self.assertEqual(entity._attr_name, "MG HS Holiday Mode Until")
        self.assertEqual(entity._attr_unique_id, "e1_VIN1_holiday_mode_until")
        self.assertIsNone(entity.native_value)
        c.async_set_holiday_until = AsyncMock(side_effect=ValueError("must be in the future"))
        # The test stubs' HomeAssistantError isn't an exception class.
        ha_error = type("HomeAssistantError", (Exception,), {})
        with patch.object(DATETIME, "HomeAssistantError", ha_error):
            with self.assertRaises(ha_error) as caught:
                asyncio.run(entity.async_set_value(datetime.now(timezone.utc)))
        self.assertIn("must be in the future", str(caught.exception))

    def test_fully_quiet_switch(self):
        c = self._coord()
        switch = SWITCH.SAICMGHolidayFullyQuietSwitch(c, NS(entry_id="e1"), c.vin_info, "VIN1")
        self.assertEqual(switch._attr_name, "MG HS Holiday Mode Fully Quiet")
        self.assertEqual(switch._attr_unique_id, "e1_VIN1_holiday_mode_fully_quiet")
        self.assertTrue(switch.is_on)
        self.assertEqual(switch.extra_state_attributes, {"active": True})

    def test_holiday_mode_attributes(self):
        c = self._coord()
        c.holiday_mode_until = datetime(2026, 11, 7, 18, 0, tzinfo=timezone.utc)
        switch = SWITCH.SAICMGHolidayModeSwitch(c, NS(entry_id="e1"), c.vin_info, "VIN1")
        self.assertEqual(
            switch.extra_state_attributes,
            {"until": "2026-11-07T18:00:00+00:00", "fully_quiet": True},
        )

    def test_datetime_platform_is_set_up(self):
        self.assertIn("datetime", CONST.PLATFORMS)


if __name__ == "__main__":
    unittest.main()
