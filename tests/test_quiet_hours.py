"""Quiet hours: no live polls overnight, only reads of SAIC's stored status (#269).

On an MG HS PHEV charging on a myenergi zappi, every poll while it charges
turns the headlights and brake lights on (@HarryFlatter, #269). Every poll on
any car wakes it. SAIC's stored copy of the car's status can be read without
contacting the car, and on an MGS6 (6-7 Oct 2026) the car updates it by itself
when a charge starts, with a "charging now" flag (extendedData2).

Between two times the owner sets, while the Quiet Hours Live Polling switch is
off and the car is off, a scheduled poll reads that copy instead, and goes to
the car only:
- once when the copy shows a charge has started since the last live poll
  (Harry chose that one flash, so HA shows "Charging" overnight);
- once when the charge should have finished;
- at the end time, when Live Polling comes back on.
"""

import asyncio
import sys
import unittest
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
QH = sys.modules["mg_saic.quiet_hours"]
CACHED = sys.modules["mg_saic.cached_status"]

NOW = datetime(2026, 10, 9, 0, 30, tzinfo=timezone.utc)
LAST_LIVE = int(datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc).timestamp())


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _stored(flag, taken_epoch):
    return CACHED.CachedVehicleStatus(
        basicVehicleStatus={"extendedData1": 58, "extendedData2": flag, "lockStatus": 1},
        gpsPosition={"wayPoint": {}},
        statusTime=taken_epoch,
        onlineStatus=1,
    )


# ── The pure parts ───────────────────────────────────────────────────────────


class WindowTests(unittest.TestCase):
    def test_overnight_window(self):
        start, end = time(22, 0), time(7, 0)
        self.assertTrue(QH.in_quiet_window(time(22, 0), start, end))
        self.assertTrue(QH.in_quiet_window(time(23, 59), start, end))
        self.assertTrue(QH.in_quiet_window(time(0, 0), start, end))
        self.assertTrue(QH.in_quiet_window(time(6, 59), start, end))
        self.assertFalse(QH.in_quiet_window(time(7, 0), start, end))
        self.assertFalse(QH.in_quiet_window(time(12, 0), start, end))
        self.assertFalse(QH.in_quiet_window(time(21, 59), start, end))

    def test_daytime_window(self):
        start, end = time(9, 0), time(17, 30)
        self.assertTrue(QH.in_quiet_window(time(9, 0), start, end))
        self.assertTrue(QH.in_quiet_window(time(17, 29), start, end))
        self.assertFalse(QH.in_quiet_window(time(17, 30), start, end))
        self.assertFalse(QH.in_quiet_window(time(8, 59), start, end))

    def test_equal_times_mean_no_window(self):
        self.assertFalse(QH.in_quiet_window(time(3, 0), time(22, 0), time(22, 0)))

    def test_seconds_do_not_matter(self):
        self.assertFalse(QH.in_quiet_window(time(6, 59, 59), time(7, 0), time(22, 0)))


class StoredTimeTests(unittest.TestCase):
    def test_parse_and_format(self):
        self.assertEqual(QH.parse_hhmm("22:30", time(1, 0)), time(22, 30))
        self.assertEqual(QH.parse_hhmm("07:05:00", time(1, 0)), time(7, 5))
        self.assertEqual(QH.parse_hhmm(time(6, 15, 42), time(1, 0)), time(6, 15))
        self.assertEqual(QH.format_hhmm(time(7, 5)), "07:05")

    def test_unusable_values_fall_back(self):
        for bad in (None, "", "25:00", "aa:bb", "7", 700, "12:60"):
            self.assertEqual(QH.parse_hhmm(bad, time(1, 0)), time(1, 0), bad)

    def test_defaults(self):
        self.assertEqual(QH.DEFAULT_QUIET_HOURS_START, time(22, 0))
        self.assertEqual(QH.DEFAULT_QUIET_HOURS_END, time(7, 0))


class ChargeStartedTests(unittest.TestCase):
    def test_flag_on_after_the_last_live_poll(self):
        self.assertTrue(
            QH.stored_charge_started(
                1, LAST_LIVE + 3600, known_charging=False, last_live_status_epoch=LAST_LIVE
            )
        )

    def test_flag_off(self):
        self.assertFalse(
            QH.stored_charge_started(
                0, LAST_LIVE + 3600, known_charging=False, last_live_status_epoch=LAST_LIVE
            )
        )

    def test_already_known_to_be_charging(self):
        self.assertFalse(
            QH.stored_charge_started(
                1, LAST_LIVE + 3600, known_charging=True, last_live_status_epoch=LAST_LIVE
            )
        )

    def test_a_flag_from_before_the_last_live_poll(self):
        # The live poll since then saw the car not charging; the old flag must
        # not send another one.
        for taken in (LAST_LIVE - 60, LAST_LIVE):
            self.assertFalse(
                QH.stored_charge_started(
                    1, taken, known_charging=False, last_live_status_epoch=LAST_LIVE
                )
            )

    def test_no_live_poll_yet(self):
        self.assertTrue(
            QH.stored_charge_started(1, LAST_LIVE, known_charging=False, last_live_status_epoch=None)
        )

    def test_missing_pieces(self):
        self.assertFalse(
            QH.stored_charge_started(None, LAST_LIVE + 9, known_charging=False, last_live_status_epoch=LAST_LIVE)
        )
        self.assertFalse(
            QH.stored_charge_started(1, None, known_charging=False, last_live_status_epoch=LAST_LIVE)
        )

    def test_reading_the_flag_and_time(self):
        summary = CACHED.summarise_cached_status(_stored(1, LAST_LIVE), NOW)
        self.assertEqual(QH.stored_charging_flag(summary), 1)
        self.assertEqual(QH.stored_status_epoch(_stored(1, LAST_LIVE)), LAST_LIVE)
        self.assertIsNone(QH.stored_charging_flag(None))
        self.assertIsNone(QH.stored_charging_flag({"fields": {"extendedData2": True}}))
        self.assertIsNone(QH.stored_status_epoch(_stored(1, 0)))
        self.assertIsNone(QH.stored_status_epoch(None))


class FinishTimeTests(unittest.TestCase):
    def test_the_cars_own_figure(self):
        self.assertEqual(
            QH.remaining_charge_time(remaining_minutes=95, remaining_valid_flag=0),
            timedelta(minutes=95),
        )

    def test_no_value_from_the_car_falls_back_to_the_battery(self):
        # 1023 with the flag set is the car's "no value" (seen on an MGS6).
        remaining = QH.remaining_charge_time(
            remaining_minutes=1023,
            remaining_valid_flag=1,
            soc_pct=60.0,
            target_pct=80.0,
            capacity_kwh=50.0,
            power_kw=2.5,
        )
        self.assertEqual(remaining, timedelta(hours=4))

    def test_no_power_no_estimate(self):
        # A car waiting for its slot (smart tariff) draws next to nothing.
        self.assertIsNone(
            QH.remaining_charge_time(soc_pct=60.0, target_pct=80.0, capacity_kwh=50.0, power_kw=0.1)
        )
        self.assertIsNone(QH.remaining_charge_time())

    def test_already_at_target(self):
        self.assertEqual(
            QH.remaining_charge_time(soc_pct=80.0, target_pct=80.0, capacity_kwh=50.0, power_kw=7.0),
            timedelta(0),
        )

    def test_the_check_lands_after_the_finish(self):
        self.assertEqual(QH.finish_check_delay(timedelta(hours=3)), timedelta(hours=3, minutes=15))
        self.assertEqual(QH.finish_check_delay(timedelta(0)), timedelta(minutes=15))
        self.assertEqual(QH.finish_check_delay(None), timedelta(hours=2))


# ── The coordinator ──────────────────────────────────────────────────────────


def _coordinator(*, flag=0, taken=LAST_LIVE + 3600, charging=False, read_error=None):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.quiet_hours = True
    c.live_polling = False
    c.quiet_hours_start = time(22, 0)
    c.quiet_hours_end = time(7, 0)
    c._quiet_finish_check_at = None
    c._quiet_unsubs = []
    c.quiet_last_stored = None
    c.is_powered_on = False
    c.is_charging = charging
    c._scheduled_refresh = True
    c._last_status_time = LAST_LIVE
    c._api_lock = None
    c._consecutive_update_failures = 0
    c._action_interval_active = False
    c.holiday_mode = False
    c.holiday_update_interval = timedelta(hours=12)
    c.default_update_interval = timedelta(minutes=30)
    c.data = {"status": "old", "charging": None}
    read = AsyncMock(side_effect=read_error) if read_error else AsyncMock(
        return_value=_stored(flag, taken)
    )
    c.client = NS(get_cached_vehicle_status=read)
    c._run_update_cycle = AsyncMock(return_value={"status": "new", "charging": None})
    c._adjust_update_interval = MagicMock()
    c.async_update_listeners = MagicMock()
    c.async_request_refresh = AsyncMock()
    c.config_entry = NS(options={})
    c.hass = MagicMock()
    return c


def _saved_options(c):
    return c.hass.config_entries.async_update_entry.call_args.kwargs["options"]


class QuietUpdateTests(unittest.TestCase):
    def _update(self, c):
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            return _run(c._async_update_data())

    def test_nothing_new_leaves_the_car_alone(self):
        c = _coordinator(flag=0)
        self.assertEqual(self._update(c), {"status": "old", "charging": None})
        c._run_update_cycle.assert_not_awaited()
        c.client.get_cached_vehicle_status.assert_awaited_once_with("TESTVIN")
        self.assertEqual(c.quiet_last_stored["charging_flag"], 0)
        self.assertEqual(c.quiet_last_stored["read_at"], NOW.isoformat())

    def test_a_charge_started_gets_one_live_poll(self):
        c = _coordinator(flag=1)
        self.assertEqual(self._update(c)["status"], "new")
        c._run_update_cycle.assert_awaited_once()

    def test_an_old_flag_does_not(self):
        c = _coordinator(flag=1, taken=LAST_LIVE)
        self._update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_already_charging_does_not(self):
        c = _coordinator(flag=1, charging=True)
        self._update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_the_finish_check_goes_to_the_car(self):
        c = _coordinator(flag=0)
        c._quiet_finish_check_at = NOW - timedelta(minutes=1)
        self.assertEqual(self._update(c)["status"], "new")
        c._run_update_cycle.assert_awaited_once()
        c.client.get_cached_vehicle_status.assert_not_awaited()
        self.assertIsNone(c._quiet_finish_check_at)

    def test_a_finish_check_not_yet_due_does_not(self):
        c = _coordinator(flag=0)
        c._quiet_finish_check_at = NOW + timedelta(hours=1)
        self._update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_a_failed_read_is_harmless(self):
        c = _coordinator(read_error=RuntimeError("code 500"))
        self.assertEqual(self._update(c), {"status": "old", "charging": None})
        c._run_update_cycle.assert_not_awaited()

    def test_a_refresh_the_owner_asks_for_still_reaches_the_car(self):
        c = _coordinator(flag=0)
        c._scheduled_refresh = False
        self._update(c)
        c._run_update_cycle.assert_awaited_once()
        c.client.get_cached_vehicle_status.assert_not_awaited()

    def test_a_car_switched_on_is_polled_as_normal(self):
        c = _coordinator(flag=0)
        c.is_powered_on = True
        self._update(c)
        c._run_update_cycle.assert_awaited_once()

    def test_with_live_polling_on_nothing_changes(self):
        c = _coordinator(flag=0)
        c.live_polling = True
        self._update(c)
        c._run_update_cycle.assert_awaited_once()
        c.client.get_cached_vehicle_status.assert_not_awaited()

    def test_with_quiet_hours_off_nothing_changes(self):
        c = _coordinator(flag=0)
        c.quiet_hours = False
        self._update(c)
        c._run_update_cycle.assert_awaited_once()


class FinishCheckTests(unittest.TestCase):
    def _charging_data(self, **chrg):
        values = dict(
            chrgngRmnngTime=1023,
            chrgngRmnngTimeV=1,
            bmsPackSOCDsp=600,
            bmsOnBdChrgTrgtSOCDspCmd=None,
            bmsPackCrnt=19950,  # 2.5 A into the pack...
            bmsPackVol=1600,  # ...at 400 V: 1 kW
        )
        values.update(chrg)
        return NS(chrgMgmtData=NS(**values))

    def _schedule(self, charging_data):
        c = _coordinator()
        c.resolve_battery_capacity_for = lambda _cd: (50.0, "test")
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            c._schedule_quiet_finish_check(charging_data, None)
        return c._quiet_finish_check_at

    def test_from_the_cars_time_remaining(self):
        due = self._schedule(self._charging_data(chrgngRmnngTime=90, chrgngRmnngTimeV=0))
        self.assertEqual(due, NOW + timedelta(minutes=105))

    def test_from_the_battery_when_the_car_gives_no_time(self):
        # 60 % to 100 % (no target set) of 50 kWh at 1 kW: 20 hours.
        due = self._schedule(self._charging_data())
        self.assertEqual(due, NOW + timedelta(hours=20, minutes=15))

    def test_nothing_to_go_on(self):
        due = self._schedule(None)
        self.assertEqual(due, NOW + timedelta(hours=2))

    def test_still_charging_after_a_live_poll_sets_a_check(self):
        c = _coordinator(charging=True)
        c._schedule_quiet_finish_check = MagicMock()
        c._after_quiet_live_poll({"charging": "c", "status": "s"})
        c._schedule_quiet_finish_check.assert_called_once_with("c", "s")

    def test_finished_or_paused_clears_it(self):
        c = _coordinator(charging=False)
        c._quiet_finish_check_at = NOW
        c._after_quiet_live_poll({"charging": None, "status": None})
        self.assertIsNone(c._quiet_finish_check_at)

    def test_nothing_is_set_once_live_polling_is_back_on(self):
        c = _coordinator(charging=True)
        c.live_polling = True
        c._schedule_quiet_finish_check = MagicMock()
        c._after_quiet_live_poll({"charging": "c", "status": "s"})
        c._schedule_quiet_finish_check.assert_not_called()


class IntervalTests(unittest.TestCase):
    def _coordinator(self):
        c = COORD_CLS.__new__(COORD_CLS)
        c.vin = "TESTVIN"
        c.quiet_hours = True
        c.live_polling = False
        c._quiet_finish_check_at = None
        c._action_interval_active = False
        c.is_powered_on = False
        c.is_charging = True
        c.is_dc_charging = False
        c.last_powered_off_time = None
        c.last_vehicle_activity = None
        c.default_update_interval = timedelta(minutes=30)
        c.powered_update_interval = timedelta(minutes=15)
        c.charging_update_interval = timedelta(minutes=5)
        c.dc_charging_update_interval = timedelta(minutes=1)
        c.grace_period_update_interval = timedelta(minutes=10)
        c.after_shutdown_update_interval = timedelta(minutes=2)
        c.holiday_mode = False
        c.holiday_update_interval = timedelta(hours=12)
        c._schedule_refresh = MagicMock()
        return c

    def test_quiet_reads_at_the_idle_interval_even_while_charging(self):
        c = self._coordinator()
        c._adjust_update_interval()
        self.assertEqual(c.update_interval, timedelta(minutes=30))
        c._schedule_refresh.assert_called_once()

    def test_sooner_when_the_finish_check_is_due(self):
        c = self._coordinator()
        c._quiet_finish_check_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        c._adjust_update_interval()
        self.assertLessEqual(c.update_interval, timedelta(minutes=10))
        self.assertGreater(c.update_interval, timedelta(minutes=9))

    def test_holiday_mode_stretches_the_reads(self):
        c = self._coordinator()
        c.holiday_mode = True
        c._adjust_update_interval()
        self.assertEqual(c.update_interval, timedelta(hours=12))

    def test_a_car_switched_on_uses_the_normal_interval(self):
        c = self._coordinator()
        c.is_powered_on = True
        c._adjust_update_interval()
        self.assertEqual(c.update_interval, timedelta(minutes=15))

    def test_live_polling_on_uses_the_normal_interval(self):
        c = self._coordinator()
        c.live_polling = True
        c._adjust_update_interval()
        self.assertEqual(c.update_interval, timedelta(minutes=5))


class SwitchingTests(unittest.TestCase):
    def test_turning_live_polling_on_catches_up(self):
        c = _coordinator()
        c._quiet_finish_check_at = NOW
        _run(c.async_set_live_polling(True, reason="test"))
        self.assertTrue(c.live_polling)
        self.assertIsNone(c._quiet_finish_check_at)
        c.async_request_refresh.assert_awaited_once()

    def test_turning_it_on_twice_polls_once(self):
        c = _coordinator()
        c.live_polling = True
        _run(c.async_set_live_polling(True))
        c.async_request_refresh.assert_not_awaited()

    def test_turning_it_off_mid_charge_sets_a_finish_check(self):
        c = _coordinator(charging=True)
        c.live_polling = True
        c._schedule_quiet_finish_check = MagicMock()
        _run(c.async_set_live_polling(False))
        self.assertFalse(c.live_polling)
        c._schedule_quiet_finish_check.assert_called_once()
        c._adjust_update_interval.assert_called_once()
        c.async_request_refresh.assert_not_awaited()

    def test_the_times_flip_it(self):
        c = _coordinator()
        c.live_polling = True
        _run(c._handle_quiet_hours_start(NOW))
        self.assertFalse(c.live_polling)
        _run(c._handle_quiet_hours_end(NOW))
        self.assertTrue(c.live_polling)
        c.async_request_refresh.assert_awaited_once()

    def test_the_times_do_nothing_with_quiet_hours_off(self):
        c = _coordinator()
        c.quiet_hours = False
        c.live_polling = True
        _run(c._handle_quiet_hours_start(NOW))
        self.assertTrue(c.live_polling)


LONDON = timezone(timedelta(hours=1))  # BST


def _local(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=LONDON)


class RestartTests(unittest.TestCase):
    """Live Polling as it was left, unless a start or end time was missed."""

    START, END = time(22, 0), time(7, 0)

    def _after(self, saved_on, saved_at, now):
        return QH.live_polling_after_restart(saved_on, saved_at, now, self.START, self.END)

    def test_switched_on_by_hand_stays_on(self):
        # On at 23:30 (say, the charge finished), restart at 01:00.
        self.assertTrue(self._after(True, _local(8, 23, 30), _local(9, 1, 0)))

    def test_off_in_the_window_stays_off(self):
        self.assertFalse(self._after(False, _local(8, 22, 0), _local(9, 3, 0)))

    def test_switched_off_by_hand_in_the_day_stays_off(self):
        self.assertFalse(self._after(False, _local(9, 14, 0), _local(9, 15, 0)))

    def test_an_end_time_missed_while_down(self):
        # Off at 22:00, Home Assistant down until 08:00: the 07:00 end applies.
        self.assertTrue(self._after(False, _local(8, 22, 0), _local(9, 8, 0)))

    def test_a_start_time_missed_while_down(self):
        # On by hand at 23:30, down until 22:30 the next night: 22:00 applies.
        self.assertFalse(self._after(True, _local(8, 23, 30), _local(9, 22, 30)))

    def test_nothing_saved_follows_the_clock(self):
        self.assertFalse(self._after(None, None, _local(9, 1, 0)))
        self.assertTrue(self._after(None, None, _local(9, 12, 0)))

    def test_a_saved_time_in_the_future_is_ignored(self):
        self.assertTrue(self._after(False, _local(9, 18, 0), _local(9, 12, 0)))

    def test_the_last_flip(self):
        at, state = QH.last_scheduled_flip(_local(9, 1, 0), self.START, self.END)
        self.assertEqual((at, state), (_local(8, 22, 0), False))
        at, state = QH.last_scheduled_flip(_local(9, 7, 0), self.START, self.END)
        self.assertEqual((at, state), (_local(9, 7, 0), True))
        self.assertIsNone(QH.last_scheduled_flip(_local(9, 7, 0), self.START, self.START))

    def test_reading_the_saved_time(self):
        self.assertEqual(
            QH.parse_saved_at("2026-10-08T21:00:00+00:00"),
            datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc),
        )
        for bad in (None, "", "yesterday", "2026-10-08T21:00:00"):
            self.assertIsNone(QH.parse_saved_at(bad), bad)

    def test_the_coordinator_restores_it(self):
        c = _coordinator()
        c.config_entry = NS(
            options={
                "quiet_hours_live_polling": True,
                "quiet_hours_live_polling_at": "2026-10-08T22:30:00+00:00",  # 23:30 BST
            }
        )
        with patch.object(COORD_MOD, "async_track_time_change", MagicMock()), patch.object(
            COORD_MOD, "local_now", lambda: _local(9, 1, 0)
        ):
            c._start_quiet_hours(restore=True)
        self.assertTrue(c.live_polling)

    def test_flipping_it_saves_it(self):
        c = _coordinator()
        c.live_polling = True
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            _run(c.async_set_live_polling(False))
        self.assertEqual(
            _saved_options(c),
            {
                "quiet_hours_live_polling": False,
                "quiet_hours_live_polling_at": NOW.isoformat(),
            },
        )

    def test_no_change_saves_nothing(self):
        c = _coordinator()
        _run(c.async_set_live_polling(False))
        c.hass.config_entries.async_update_entry.assert_not_called()


class StartTests(unittest.TestCase):
    def _start(self, local_time, enabled=True):
        c = _coordinator()
        c.quiet_hours = enabled
        c.hass = MagicMock()
        unsub = MagicMock()
        tracker = MagicMock(return_value=unsub)
        with patch.object(COORD_MOD, "async_track_time_change", tracker), patch.object(
            COORD_MOD, "local_now", lambda: datetime.combine(NOW.date(), local_time)
        ):
            c._start_quiet_hours()
        return c, tracker

    def test_inside_the_window(self):
        c, tracker = self._start(time(23, 0))
        self.assertFalse(c.live_polling)
        self.assertEqual(tracker.call_count, 2)
        kwargs = [call.kwargs for call in tracker.call_args_list]
        self.assertEqual((kwargs[0]["hour"], kwargs[0]["minute"]), (22, 0))
        self.assertEqual((kwargs[1]["hour"], kwargs[1]["minute"]), (7, 0))

    def test_outside_the_window(self):
        c, _ = self._start(time(12, 0))
        self.assertTrue(c.live_polling)

    def test_disabled(self):
        c, tracker = self._start(time(23, 0), enabled=False)
        self.assertTrue(c.live_polling)
        tracker.assert_not_called()

    def test_restarting_drops_the_old_times(self):
        old = MagicMock()
        c = _coordinator()
        c._quiet_unsubs = [old]
        c.hass = MagicMock()
        with patch.object(COORD_MOD, "async_track_time_change", MagicMock()), patch.object(
            COORD_MOD, "local_now", lambda: datetime.combine(NOW.date(), time(12, 0))
        ):
            c._start_quiet_hours()
        old.assert_called_once()


class SettingTheTimesTests(unittest.TestCase):
    def test_saved_in_the_options_alongside_everything_else(self):
        c = _coordinator()
        c.config_entry = NS(options={"holiday_mode": True, "update_interval": 30})
        c.hass = MagicMock()
        _run(c.async_set_quiet_hours_time("start", time(23, 15)))
        options = c.hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(
            options,
            {"holiday_mode": True, "update_interval": 30, "quiet_hours_start": "23:15"},
        )

    def test_end(self):
        c = _coordinator()
        c.config_entry = NS(options={})
        c.hass = MagicMock()
        _run(c.async_set_quiet_hours_time("end", time(6, 30)))
        options = c.hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(options, {"quiet_hours_end": "06:30"})


class OptionsFormTests(unittest.TestCase):
    """Saving the options form must not drop what the device's controls keep."""

    def _save(self, saved, entered):
        cf = sys.modules["mg_saic.config_flow"]
        flow = cf.SAICMGOptionsFlowHandler.__new__(cf.SAICMGOptionsFlowHandler)
        flow.config_entry = NS(options=saved, data={})
        flow._validate_abrp = AsyncMock(return_value={})
        flow.async_create_entry = lambda title, data: {"data": data}
        return _run(flow.async_step_init(dict(entered)))["data"]

    def test_holiday_mode_and_the_times_survive(self):
        saved = {
            "holiday_mode": True,
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "06:00",
            "quiet_hours_live_polling": False,
            "quiet_hours_live_polling_at": "2026-10-08T21:00:00+00:00",
            "update_interval": 30,
        }
        data = self._save(saved, {"update_interval": 45, "quiet_hours": True})
        self.assertFalse(data["quiet_hours_live_polling"])
        self.assertEqual(data["quiet_hours_live_polling_at"], "2026-10-08T21:00:00+00:00")
        self.assertEqual(data["update_interval"], 45)
        self.assertTrue(data["quiet_hours"])
        self.assertTrue(data["holiday_mode"])
        self.assertEqual(data["quiet_hours_start"], "23:00")
        self.assertEqual(data["quiet_hours_end"], "06:00")

    def test_a_cleared_override_stays_cleared(self):
        # Only the device's own settings are carried over, not old form values.
        saved = {"battery_capacity_override_kwh": 60.0}
        data = self._save(saved, {"battery_capacity_override_kwh": ""})
        self.assertNotIn("battery_capacity_override_kwh", data)

    def test_the_option_reloads_the_integration(self):
        # Ticking it adds the controls, which only happens at setup.
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1]
            / "custom_components" / "mg_saic" / "__init__.py"
        ).read_text()
        block = source.split("ENTITY_CAPABILITY_OPTIONS = (", 1)[1].split(")", 1)[0]
        self.assertIn('"quiet_hours"', block)


if __name__ == "__main__":
    unittest.main()
