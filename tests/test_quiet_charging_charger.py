"""Quiet charging follows the owner's charger, if they pick one (#269).

@HarryFlatter's HS PHEV, 13 Oct 2026 (BST), from his security camera: the
integration's live polls at 00:39 and 01:33, both while charging, flashed
the lights. The car also flashes them by itself when a charge starts
(00:32) or resumes (00:58). His zappi knew the start (00:32) and the finish
(01:39) to the minute, and the car never tells SAIC either.

So with a charger entity: poll the moment it says charging (inside the
car's own flash), and once it has said "not charging" for 2 minutes (after
the car has stopped). A one-minute zappi pause costs nothing.
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
CF = sys.modules["mg_saic.config_flow"]
CACHED = sys.modules["mg_saic.cached_status"]

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 23, 40, tzinfo=UTC)
ZAPPI = "sensor.myenergi_zappi_status"


def _event(old, new):
    return NS(data={"old_state": NS(state=old), "new_state": NS(state=new)})


def _coordinator(charger_state="Charging", *, entity=ZAPPI, charging=False):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.quiet_hours = True
    c.live_polling = False
    c.quiet_hours_start = time(0, 0)
    c.quiet_hours_end = time(6, 0)
    c.quiet_charger_entity = entity
    c._charger_unsub = None
    c._charger_stop_unsub = None
    c._quiet_finish_check_at = None
    c._quiet_unsubs = []
    c.quiet_last_stored = None
    c.is_powered_on = False
    c.is_charging = charging
    c._scheduled_refresh = True
    c._last_status_time = int(datetime(2026, 10, 12, 22, 0, tzinfo=UTC).timestamp())
    c._api_lock = None
    c._consecutive_update_failures = 0
    c._action_interval_active = False
    c.holiday_mode = False
    c.default_update_interval = timedelta(minutes=15)
    c.hass = MagicMock()
    c.hass.states.get = lambda eid: NS(state=charger_state) if eid == entity else None
    c.async_request_refresh = AsyncMock()
    c._run_update_cycle = AsyncMock(return_value={"status": "new", "charging": None})
    c._after_quiet_live_poll = MagicMock()
    c._adjust_update_interval = MagicMock()
    c.data = {
        "status": NS(basicVehicleStatus=NS(lockStatus=1)),
        "charging": NS(
            rvsChargeStatus=NS(chargingGunState=1),
            chrgMgmtData=NS(bmsPackSOCDsp=784, bmsOnBdChrgTrgtSOCDspCmd=None),
        ),
    }
    return c


def _run(coro):
    return asyncio.run(coro)


class ChargerStateTests(unittest.TestCase):
    def test_states_that_mean_charging(self):
        for state in ("on", "Charging", "charging", " CHARGING "):
            self.assertTrue(QH.charger_says_charging(state), state)

    def test_states_that_do_not(self):
        for state in ("off", "Waiting for EV", "Completed", "plugged_in", "Paused",
                      "Boosting", "discharging"):
            self.assertFalse(QH.charger_says_charging(state), state)

    def test_no_usable_state(self):
        for state in (None, "unavailable", "unknown", ""):
            self.assertIsNone(QH.charger_says_charging(state), state)


class ChargerStartsTests(unittest.TestCase):
    def test_one_live_poll_the_moment_it_starts(self):
        c = _coordinator()
        _run(c._handle_charger_change(_event("Waiting for EV", "Charging")))
        c.async_request_refresh.assert_awaited_once()

    def test_not_when_already_known_charging(self):
        c = _coordinator(charging=True)
        _run(c._handle_charger_change(_event("Waiting for EV", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_not_outside_quiet_charging(self):
        c = _coordinator()
        c.live_polling = True
        _run(c._handle_charger_change(_event("Waiting for EV", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_charger_dropping_off_the_network_is_not_a_change(self):
        c = _coordinator()
        _run(c._handle_charger_change(_event("Charging", "unavailable")))
        _run(c._handle_charger_change(_event("unavailable", "Charging")))
        c.async_request_refresh.assert_not_awaited()


class ChargerStopsTests(unittest.TestCase):
    def setUp(self):
        self.later = MagicMock(return_value=MagicMock())
        self.patch = patch.object(COORD_MOD, "async_call_later", self.later)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_stop_waits_two_minutes_then_polls(self):
        c = _coordinator("Completed", charging=True)
        _run(c._handle_charger_change(_event("Charging", "Completed")))
        c.async_request_refresh.assert_not_awaited()
        self.assertEqual(self.later.call_args.args[1], 120)
        # Two minutes on, still stopped: one poll.
        _run(c._handle_charger_stopped())
        c.async_request_refresh.assert_awaited_once()

    def test_a_one_minute_zappi_pause_costs_nothing(self):
        # 00:57 Waiting for EV, 00:58 Charging again.
        c = _coordinator("Charging", charging=True)
        _run(c._handle_charger_change(_event("Charging", "Waiting for EV")))
        cancel = c._charger_stop_unsub
        _run(c._handle_charger_change(_event("Waiting for EV", "Charging")))
        cancel.assert_called_once()
        c.async_request_refresh.assert_not_awaited()

    def test_charging_again_when_the_wait_ends_means_no_poll(self):
        c = _coordinator("Charging", charging=True)
        _run(c._handle_charger_stopped())
        c.async_request_refresh.assert_not_awaited()


class FinishCheckTests(unittest.TestCase):
    def _update(self, c):
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            return _run(c._async_update_data())

    def test_held_while_the_charger_says_charging(self):
        # Harry's 01:33 check, which flashed the lights.
        c = _coordinator("Charging", charging=True)
        c._quiet_finish_check_at = NOW - timedelta(minutes=1)
        c.client = NS(get_cached_vehicle_status=AsyncMock(return_value=None))
        self._update(c)
        c._run_update_cycle.assert_not_awaited()
        self.assertIsNone(c._quiet_finish_check_at)

    def test_goes_ahead_without_a_charger(self):
        c = _coordinator(entity=None, charging=True)
        c._quiet_finish_check_at = NOW - timedelta(minutes=1)
        self._update(c)
        c._run_update_cycle.assert_awaited_once()

    def test_goes_ahead_when_the_charger_has_stopped(self):
        c = _coordinator("Completed", charging=True)
        c._quiet_finish_check_at = NOW - timedelta(minutes=1)
        self._update(c)
        c._run_update_cycle.assert_awaited_once()


class StoredCopyIsTheFallbackTests(unittest.TestCase):
    def test_with_a_charger_the_stored_copy_does_not_trigger_polls(self):
        # The plug-in write (unlocked) would have cost a poll at 00:24.
        c = _coordinator("Waiting for EV")
        reply = CACHED.CachedVehicleStatus(
            basicVehicleStatus={"extendedData1": 78, "extendedData2": 0, "lockStatus": 0},
            gpsPosition={"wayPoint": {}},
            statusTime=int(datetime(2026, 10, 12, 23, 18, 49, tzinfo=UTC).timestamp()),
            onlineStatus=1,
        )
        c.client = NS(get_cached_vehicle_status=AsyncMock(return_value=reply))
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            _run(c._async_update_data())
        c._run_update_cycle.assert_not_awaited()

    def test_with_the_charger_unavailable_the_stored_copy_still_works(self):
        c = _coordinator("unavailable")
        reply = CACHED.CachedVehicleStatus(
            basicVehicleStatus={"extendedData1": 78, "extendedData2": 0, "lockStatus": 0},
            gpsPosition={"wayPoint": {}},
            statusTime=int(datetime(2026, 10, 12, 23, 18, 49, tzinfo=UTC).timestamp()),
            onlineStatus=1,
        )
        c.client = NS(get_cached_vehicle_status=AsyncMock(return_value=reply))
        with patch.object(COORD_MOD, "utcnow", lambda: NOW):
            _run(c._async_update_data())
        c._run_update_cycle.assert_awaited_once()


class WatchingTests(unittest.TestCase):
    def test_subscribes_to_the_picked_entity(self):
        c = _coordinator()
        track = MagicMock(return_value=MagicMock())
        with patch.object(COORD_MOD, "async_track_state_change_event", track):
            c._watch_charger()
        self.assertEqual(track.call_args.args[1], [ZAPPI])
        self.assertIs(c._charger_unsub, track.return_value)

    def test_nothing_picked_nothing_watched(self):
        c = _coordinator(entity=None)
        track = MagicMock()
        with patch.object(COORD_MOD, "async_track_state_change_event", track):
            c._watch_charger()
        track.assert_not_called()

    def test_re_picking_drops_the_old_subscription(self):
        c = _coordinator()
        old = MagicMock()
        c._charger_unsub = old
        with patch.object(COORD_MOD, "async_track_state_change_event", MagicMock()):
            c._watch_charger()
        old.assert_called_once()


class PickerTests(unittest.TestCase):
    def test_offers_sensors_binary_sensors_and_input_booleans(self):
        # The selector is stubbed under the harness, so check the source.
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic" / "config_flow.py"
        ).read_text()
        self.assertIn(
            'EntitySelectorConfig(domain=["sensor", "binary_sensor", "input_boolean"])',
            source,
        )

    def test_an_input_boolean_set_by_an_automation_works(self):
        c = _coordinator("on", entity="input_boolean.car_charging")
        self.assertTrue(c._charger_charging())
        _run(c._handle_charger_change(_event("off", "on")))
        c.async_request_refresh.assert_awaited_once()


class OptionsFormTests(unittest.TestCase):
    def _save(self, saved, entered):
        flow = CF.SAICMGOptionsFlowHandler.__new__(CF.SAICMGOptionsFlowHandler)
        flow.config_entry = NS(options=saved, data={})
        flow._validate_abrp = AsyncMock(return_value={})
        flow.async_create_entry = lambda title, data: {"data": data}
        return _run(flow.async_step_init(dict(entered)))["data"]

    def test_picked_entity_is_saved(self):
        data = self._save({}, {CF.CONF_QUIET_CHARGER_ENTITY: ZAPPI})
        self.assertEqual(data[CF.CONF_QUIET_CHARGER_ENTITY], ZAPPI)

    def test_clearing_the_picker_removes_it(self):
        data = self._save({CF.CONF_QUIET_CHARGER_ENTITY: ZAPPI}, {CF.CONF_QUIET_CHARGER_ENTITY: ""})
        self.assertNotIn(CF.CONF_QUIET_CHARGER_ENTITY, data)


if __name__ == "__main__":
    unittest.main()
