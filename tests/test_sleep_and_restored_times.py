"""Overnight deep sleep, restored times after a restart, and window activity.

From @HarryFlatter's logs (#262, 30 Sept):

1. Car asleep overnight: once flagged unreachable, every hourly scheduled poll
   still made RETRY_LIMIT status attempts (~4 minutes of requests that could
   not succeed) plus a 20 s charging attempt. Now: one attempt per scheduled
   poll while flagged unreachable, charging skipped. User and event-driven
   refreshes keep full retries (that's how a car woken with the key is caught).
2. Last Powered On / Off / Vehicle Activity were "restored" from an entity ID
   the sensors never have, so every restart replaced them with "24 hours ago".
   Now they're saved in the integration's own storage and restored from it.
   The first reading after a restart is also no longer counted as activity.
3. Opening a window counts as vehicle activity.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
CODE4 = Exception(
    "return code: 4, message: The remote control instruction failed, please try "
    "again later., event_id: 1118530795"
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _bare():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c._code4_this_cycle = False
    c._consecutive_unreachable_polls = 0
    c._last_command_unreachable = False
    c._scheduled_refresh = False
    c._gave_up_car_asleep = False
    return c


# ── 1. One attempt per scheduled poll while the car is asleep ────────────────


class SleepingCarRetryTests(unittest.TestCase):
    def _fetch(self, c, error=CODE4):
        calls = []

        async def failing():
            calls.append(1)
            raise error

        with patch.object(COORD_MOD.asyncio, "sleep", AsyncMock()):
            result = _run(c._fetch_with_retries(failing, lambda _d: False, "vehicle status"))
        return result, len(calls)

    def test_scheduled_poll_of_a_sleeping_car_tries_once(self):
        c = _bare()
        c._scheduled_refresh = True
        c._last_command_unreachable = True  # flagged after 2 failed polls
        result, attempts = self._fetch(c)
        self.assertIsNone(result)
        self.assertEqual(attempts, 1)
        self.assertTrue(c._gave_up_car_asleep)
        self.assertTrue(c._code4_this_cycle, "still counted for the debounce")

    def test_not_yet_flagged_keeps_full_retries(self):
        # The first failing polls must still retry: a code 4 is often a
        # one-off hiccup, and the flag needs them to count up (#238).
        c = _bare()
        c._scheduled_refresh = True
        _, attempts = self._fetch(c)
        self.assertEqual(attempts, COORD_MOD.RETRY_LIMIT)
        self.assertFalse(c._gave_up_car_asleep)

    def test_user_or_event_refresh_keeps_full_retries(self):
        # Harry's 07:54 refresh: the car was flagged unreachable, failed a few
        # times while waking up from the key unlock, then answered.
        c = _bare()
        c._last_command_unreachable = True
        _, attempts = self._fetch(c)
        self.assertEqual(attempts, COORD_MOD.RETRY_LIMIT)
        self.assertFalse(c._gave_up_car_asleep)

    def test_other_errors_keep_full_retries(self):
        c = _bare()
        c._scheduled_refresh = True
        c._last_command_unreachable = True
        _, attempts = self._fetch(c, error=Exception("return code: 6, message: x"))
        self.assertEqual(attempts, COORD_MOD.RETRY_LIMIT)

    def test_success_on_the_single_attempt_is_returned(self):
        c = _bare()
        c._scheduled_refresh = True
        c._last_command_unreachable = True

        async def ok():
            return "status"

        self.assertEqual(
            _run(c._fetch_with_retries(ok, lambda _d: False, "vehicle status")),
            "status",
        )

    def test_scheduled_flag_only_during_the_timer_refresh(self):
        c = _bare()
        seen = []

        async def refresh():
            seen.append(c._scheduled_refresh)

        c.async_refresh = refresh
        _run(c._handle_refresh_interval(None))
        self.assertEqual(seen, [True])
        self.assertFalse(c._scheduled_refresh)

    def test_scheduled_flag_cleared_even_if_the_refresh_fails(self):
        c = _bare()

        async def refresh():
            raise RuntimeError("boom")

        c.async_refresh = refresh
        with self.assertRaises(RuntimeError):
            _run(c._handle_refresh_interval(None))
        self.assertFalse(c._scheduled_refresh)


class SleepingCarChargingSkippedTests(unittest.TestCase):
    """The real _run_update_cycle with scripted fetches."""

    def _coord(self, status):
        c = _bare()
        c._api_lock = None
        c.config_entry = NS(data={"vin": "TESTVIN"})
        c.client = NS(get_vehicle_info=None, get_vehicle_status=None, get_charging_info=None)
        c.vehicle_type = "PHEV"
        c.is_initial_setup = False
        c.has_battery_heating = False
        c.has_sunroof = c.has_heated_seats = c.has_rear_heated_seats = False
        c.has_steering_wheel_heat = c.has_window_control = False
        c.is_charging = c.is_powered_on = False
        c.last_powered_on_time = c.last_powered_off_time = None
        c.last_vehicle_activity = None
        c.update_interval = timedelta(hours=1)
        c._last_status_time = None
        c._last_poll_result = None
        c.charging_freshness = COORD_MOD.ChargingFreshnessTracker()
        c._charging_outcome_recorded = False
        c._update_state = MagicMock()
        c._adjust_update_interval = MagicMock()
        c._update_reachability_after_poll = MagicMock()
        c._is_status_timestamp_valid = MagicMock(return_value=True)
        c._maybe_send_abrp = AsyncMock()
        self.charging_calls = []

        async def scripted(_fetch, _is_generic, name):
            if name == "vehicle info":
                return [NS(vin="TESTVIN")]
            if name == "vehicle status":
                return status(c)
            self.charging_calls.append(1)
            return NS(chrgMgmtData=NS())

        c._fetch_with_retries = scripted
        return c

    def test_charging_not_requested_when_the_sleeping_car_did_not_answer(self):
        def asleep(c):
            c._gave_up_car_asleep = True
            return None

        c = self._coord(asleep)
        c.charging_freshness.record_success(datetime.now(timezone.utc) - timedelta(hours=7))
        _run(c._run_update_cycle())
        self.assertEqual(self.charging_calls, [])
        self.assertEqual(c.charging_data_freshness, "stale")
        self.assertEqual(c.charging_freshness.last_error, "Car not answering (asleep)")
        self.assertTrue(c._charging_outcome_recorded)

    def test_charging_still_requested_normally(self):
        c = self._coord(lambda c: NS(statusTime=1790011000))
        _run(c._run_update_cycle())
        self.assertEqual(self.charging_calls, [1])


# ── 2. Times survive a restart ───────────────────────────────────────────────


ON = "2026-09-29T08:47:00+00:00"
OFF = "2026-09-29T09:10:00+00:00"
ACTIVE = "2026-09-29T09:11:00+00:00"


def _restoring(stored):
    c = _bare()
    c.trip_stats = NS(activity_times=stored) if stored is not ... else None
    c.config_entry = NS(entry_id="entry-1")
    c.hass = NS(states=NS(get=lambda _eid: None))
    return c


class RestoredTimesTests(unittest.TestCase):
    def test_saved_times_come_back_exactly(self):
        c = _restoring(
            {"last_powered_on": ON, "last_powered_off": OFF, "last_vehicle_activity": ACTIVE}
        )
        c._restore_activity_times()
        self.assertEqual(c.last_powered_on_time, datetime(2026, 9, 29, 8, 47, tzinfo=timezone.utc))
        self.assertEqual(c.last_powered_off_time, datetime(2026, 9, 29, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(c.last_vehicle_activity, datetime(2026, 9, 29, 9, 11, tzinfo=timezone.utc))
        # Nothing new to save straight after restoring.
        self.assertEqual(c._saved_activity_times["last_powered_off"], OFF)

    def test_nothing_saved_falls_back_to_24_hours_ago(self):
        c = _restoring(None)
        before = datetime.now(timezone.utc) - timedelta(hours=24)
        c._restore_activity_times()
        self.assertGreaterEqual(c.last_powered_off_time, before)
        self.assertLess(c.last_powered_off_time, datetime.now(timezone.utc) - timedelta(hours=23))

    def test_partly_saved_only_fills_the_gaps(self):
        c = _restoring({"last_powered_on": ON})
        c._restore_activity_times()
        self.assertEqual(c.last_powered_on_time.isoformat(), ON)
        self.assertLess(c.last_powered_off_time, datetime.now(timezone.utc) - timedelta(hours=23))

    def test_bad_saved_value_is_ignored(self):
        c = _restoring({"last_powered_on": "not a time"})
        c._restore_activity_times()
        self.assertLess(c.last_powered_on_time, datetime.now(timezone.utc) - timedelta(hours=23))

    def test_naive_saved_value_is_read_as_utc(self):
        c = _restoring({"last_powered_on": "2026-09-29T08:47:00"})
        c._restore_activity_times()
        self.assertEqual(c.last_powered_on_time, datetime(2026, 9, 29, 8, 47, tzinfo=timezone.utc))

    def test_no_trip_store_does_not_break_startup(self):
        c = _restoring(...)
        c._restore_activity_times()
        self.assertIsNotNone(c.last_vehicle_activity)


class SavingTimesTests(unittest.TestCase):
    def _coord(self):
        c = _restoring({"last_powered_on": ON, "last_powered_off": OFF, "last_vehicle_activity": ACTIVE})
        c._restore_activity_times()
        c._schedule_trip_save = MagicMock()
        return c

    def test_unchanged_times_are_not_saved_again(self):
        c = self._coord()
        c._save_activity_times_if_changed()
        c._schedule_trip_save.assert_not_called()

    def test_a_new_time_is_saved(self):
        c = self._coord()
        c.last_powered_off_time = datetime(2026, 9, 30, 7, 11, 25, tzinfo=timezone.utc)
        c._save_activity_times_if_changed()
        c._schedule_trip_save.assert_called_once()
        self.assertEqual(
            c.trip_stats.activity_times["last_powered_off"], "2026-09-30T07:11:25+00:00"
        )
        c._save_activity_times_if_changed()  # and only once
        c._schedule_trip_save.assert_called_once()


# ── 2b/3. Activity: first reading is a baseline; windows count ──────────────


def _status(**overrides):
    values = dict(
        lockStatus=1, driverDoor=0, passengerDoor=0, rearLeftDoor=0, rearRightDoor=0,
        bootStatus=0, bonnetStatus=0, remoteClimateStatus=0, rmtHtdRrWndSt=0,
        engineStatus=0, driverWindow=0, passengerWindow=0, rearLeftWindow=0,
        rearRightWindow=0, powerMode=0,
    )
    values.update(overrides)
    return NS(**values)


def _activity_coord():
    c = _bare()
    c.is_charging = False
    c.enable_shutdown_refresh_sequence = True
    c._shutdown_refresh_task = None
    c._start_shutdown_refresh_sequence = MagicMock()
    return c


class ActivityTests(unittest.TestCase):
    def test_first_reading_after_a_restart_is_not_activity(self):
        # Harry's "Vehicle Active 29 Sept 19:15" was just HA restarting.
        c = _activity_coord()
        self.assertFalse(c._detect_activity(_status(), NS(bmsChrgSts=0)))

    def test_a_real_change_after_the_baseline_still_counts(self):
        c = _activity_coord()
        c._detect_activity(_status())
        self.assertTrue(c._detect_activity(_status(lockStatus=0)))

    def test_opening_a_window_is_activity(self):
        c = _activity_coord()
        c._detect_activity(_status())
        self.assertTrue(c._detect_activity(_status(driverWindow=1)))

    def test_a_window_that_always_reads_open_never_counts(self):
        # e.g. the MG3 Hybrid's phantom passenger window (stuck at 1).
        c = _activity_coord()
        c._detect_activity(_status(passengerWindow=1))
        self.assertFalse(c._detect_activity(_status(passengerWindow=1)))

    def test_power_mode_and_charging_baselines(self):
        c = _activity_coord()
        c._detect_activity(_status(powerMode=0), NS(bmsChrgSts=0))
        self.assertTrue(c._detect_activity(_status(powerMode=2), NS(bmsChrgSts=0)))
        self.assertTrue(c._detect_activity(_status(powerMode=2), NS(bmsChrgSts=1)))

    def test_lock_trigger_unaffected(self):
        c = _activity_coord()
        c._detect_activity(_status(lockStatus=0))
        c._detect_activity(_status(lockStatus=1))
        c._start_shutdown_refresh_sequence.assert_called_once()


if __name__ == "__main__":
    unittest.main()
