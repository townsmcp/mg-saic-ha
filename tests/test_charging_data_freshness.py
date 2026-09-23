"""Charging Data Freshness (#262).

The charging endpoint fails independently of vehicle status, and while it does
every charging sensor holds its last value. The Data Freshness sensor only
reflects the vehicle-status poll, so it could read "live" while the charging
figures were hours old. These tests cover the tracker (logic.py), the two
entities (sensor.py) and the coordinator's recording of every charging fetch
outcome -- including the silent one, where _fetch_with_retries exhausts its
retries and returns None instead of raising.

Also covers a Data Freshness fix found in @HarryFlatter's log: a cycle whose
status fetch exhausted its retries still counted as a successful poll, so the
sensor read "cached" when nothing had come back at all.

Harnesses are reused from test_erac_fallback (HA-stubbed sensor/logic) and
test_setup_and_config_flow (HA-stubbed coordinator), following the existing
test_india_tyre_pressure -> test_india_soc precedent.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_erac_fallback import LOGIC, SENSOR
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
T0 = datetime(2026, 9, 21, 15, 47, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Tracker (logic.py) ───────────────────────────────────────────────────────


class ChargingFreshnessTrackerTests(unittest.TestCase):
    def test_none_before_any_attempt(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        self.assertIsNone(tracker.state)
        self.assertEqual(tracker.attributes(T0), {"consecutive_failures": 0})

    def test_success_is_live(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_success(T0)
        self.assertEqual(tracker.state, "live")
        attrs = tracker.attributes(T0)
        self.assertEqual(attrs["last_success"], T0.isoformat())
        self.assertEqual(attrs["data_age_minutes"], 0)
        self.assertNotIn("stale_since", attrs)
        self.assertNotIn("last_error", attrs)

    def test_failure_after_success_is_stale_and_ages(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_success(T0)
        t1 = T0 + timedelta(hours=1)
        tracker.record_failure(t1, "Timed out after 20s")
        self.assertEqual(tracker.state, "stale")
        attrs = tracker.attributes(T0 + timedelta(minutes=95))
        self.assertEqual(attrs["data_age_minutes"], 95)
        self.assertEqual(attrs["stale_since"], t1.isoformat())
        self.assertEqual(attrs["consecutive_failures"], 1)
        self.assertEqual(attrs["last_error"], "Timed out after 20s")

    def test_stale_since_marks_the_first_failure_of_the_run(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_success(T0)
        first = T0 + timedelta(hours=1)
        tracker.record_failure(first, "a")
        tracker.record_failure(first + timedelta(hours=1), "b")
        attrs = tracker.attributes(first + timedelta(hours=2))
        self.assertEqual(attrs["stale_since"], first.isoformat())
        self.assertEqual(attrs["consecutive_failures"], 2)
        self.assertEqual(attrs["last_error"], "b")

    def test_failure_with_no_prior_success_is_no_data(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_failure(T0, "return code: 4")
        self.assertEqual(tracker.state, "no_data")
        self.assertNotIn("data_age_minutes", tracker.attributes(T0))

    def test_recovery_clears_the_outage(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_success(T0)
        tracker.record_failure(T0 + timedelta(hours=1), "x")
        tracker.record_success(T0 + timedelta(hours=3))
        self.assertEqual(tracker.state, "live")
        attrs = tracker.attributes(T0 + timedelta(hours=3))
        self.assertEqual(attrs["consecutive_failures"], 0)
        self.assertNotIn("stale_since", attrs)
        self.assertNotIn("last_error", attrs)

    def test_long_error_text_is_capped(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        tracker.record_failure(T0, "x" * 1000)
        self.assertEqual(len(tracker.last_error), LOGIC.LAST_ERROR_MAX_CHARS)

    def test_states_tuple_matches_what_the_tracker_can_return(self):
        self.assertEqual(
            set(LOGIC.CHARGING_DATA_FRESHNESS_STATES), {"live", "stale", "no_data"}
        )


# ── Entities (sensor.py) ─────────────────────────────────────────────────────
# The stubbed SensorEntity doesn't map _attr_* onto HA's properties
# (unique_id, name, options...), so those are asserted directly.


class ChargingFreshnessEntityTests(unittest.TestCase):
    def _coordinator(self):
        tracker = LOGIC.ChargingFreshnessTracker()
        return NS(
            vin_info=NS(vin="VIN1", brandName="MG", modelName="HS"),
            charging_freshness=tracker,
            charging_data_freshness=None,
        ), tracker

    def test_freshness_sensor(self):
        coordinator, tracker = self._coordinator()
        entity = SENSOR.SAICMGChargingDataFreshnessSensor(
            coordinator, NS(entry_id="e"), coordinator.vin_info, "VIN1"
        )
        self.assertEqual(entity._attr_unique_id, "e_VIN1_charging_data_freshness")
        self.assertEqual(entity._attr_name, "MG HS Charging Data Freshness")
        self.assertEqual(entity._attr_options, ["live", "stale", "no_data"])
        self.assertEqual(entity._attr_translation_key, "charging_data_freshness")
        self.assertTrue(entity.available)

        tracker.record_success(datetime.now(timezone.utc) - timedelta(minutes=30))
        tracker.record_failure(datetime.now(timezone.utc), "Timed out after 20s")
        coordinator.charging_data_freshness = tracker.state
        self.assertEqual(entity.native_value, "stale")
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["data_age_minutes"], 30)
        self.assertEqual(attrs["last_error"], "Timed out after 20s")

    def test_last_updated_sensor(self):
        coordinator, tracker = self._coordinator()
        entity = SENSOR.SAICMGChargingDataLastUpdatedSensor(
            coordinator, NS(entry_id="e"), coordinator.vin_info, "VIN1"
        )
        self.assertEqual(entity._attr_unique_id, "e_VIN1_charging_data_last_updated")
        self.assertEqual(entity._attr_name, "MG HS Charging Data Last Updated")
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        tracker.record_success(T0)
        tracker.record_failure(T0 + timedelta(hours=1))
        self.assertEqual(entity.native_value, T0, "must stay on the last SUCCESS")


# ── Coordinator: whole-cycle failures ────────────────────────────────────────


def _failing_cycle_coord(vehicle_type="PHEV"):
    c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(
        COORD_MOD.SAICMGDataUpdateCoordinator
    )
    c.vin = "TESTVIN"
    c.update_interval = timedelta(hours=6)
    c.default_update_interval = timedelta(hours=6)
    c.failure_retry_interval = COORD_MOD.UPDATE_INTERVAL_AFTER_FAILURE
    c._consecutive_update_failures = 0
    c._last_poll_result = None
    c.vehicle_type = vehicle_type
    c.client = NS()  # no supported_features -> treated as fully featured
    c.charging_freshness = COORD_MOD.ChargingFreshnessTracker()
    c._charging_outcome_recorded = False
    return c


async def _boom():
    raise Exception("return code: 6")


class WholeCycleFailureTests(unittest.TestCase):
    def test_failed_cycle_marks_charging_stale(self):
        c = _failing_cycle_coord()
        c.charging_freshness.record_success(T0)
        c._run_update_cycle = _boom
        with self.assertRaises(Exception):
            _run(c._async_update_data())
        self.assertEqual(c.charging_data_freshness, "stale")
        self.assertIn("return code: 6", c.charging_freshness.last_error)

    def test_not_double_counted_when_charging_already_recorded(self):
        c = _failing_cycle_coord()
        c.charging_freshness.record_success(T0)
        c._charging_outcome_recorded = True  # cycle failed AFTER the fetch
        c._run_update_cycle = _boom
        with self.assertRaises(Exception):
            _run(c._async_update_data())
        self.assertEqual(c.charging_data_freshness, "live")

    def test_ice_records_nothing(self):
        c = _failing_cycle_coord(vehicle_type="ICE")
        c._run_update_cycle = _boom
        with self.assertRaises(Exception):
            _run(c._async_update_data())
        self.assertIsNone(c.charging_data_freshness)

    def test_bookkeeping_error_cannot_break_fast_retry(self):
        """The freshness bookkeeping runs last and is guarded: even if it
        blew up, the #238 fast-retry interval and the re-raise must happen.
        (The first draft ran it first, and a missing attribute skipped the
        interval cap entirely.)"""
        c = _failing_cycle_coord()
        del c.vehicle_type  # make charging_data_applies raise
        c._run_update_cycle = _boom
        with self.assertRaisesRegex(Exception, "return code: 6"):
            _run(c._async_update_data())
        self.assertEqual(c.update_interval, COORD_MOD.UPDATE_INTERVAL_AFTER_FAILURE)


# ── Coordinator: per-fetch recording in the real _run_update_cycle ───────────


class UpdateCycleRecordingTests(unittest.TestCase):
    """Drives the real _run_update_cycle. _fetch_with_retries is scripted per
    endpoint (the real one sleeps 15 s per retry) and peripheral steps are
    mocked, as in TestUpdateStateSmoke -- this is about what gets recorded."""

    def _coord(self, *, status, charging, vehicle_type="PHEV"):
        c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(
            COORD_MOD.SAICMGDataUpdateCoordinator
        )
        c.vin = "TESTVIN"
        c._api_lock = None
        c.config_entry = NS(data={"vin": "TESTVIN"})
        c.client = NS(
            get_vehicle_info=None, get_vehicle_status=None, get_charging_info=None
        )
        c.vehicle_type = vehicle_type
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

        outcomes = {
            "vehicle info": lambda: [NS(vin="TESTVIN")],
            "vehicle status": status,
            "charging info": charging,
        }

        async def _scripted(_fetch, _is_generic, name):
            result = outcomes[name]()
            if asyncio.iscoroutine(result):
                result = await result
            return result

        c._fetch_with_retries = _scripted
        return c

    @staticmethod
    def _status():
        return NS(statusTime=1790011000)

    @staticmethod
    def _raise(exc):
        def _f():
            raise exc
        return _f

    def test_success_records_live(self):
        c = self._coord(status=self._status, charging=lambda: NS(chrgMgmtData=NS()))
        _run(c._run_update_cycle())
        self.assertEqual(c.charging_data_freshness, "live")
        self.assertTrue(c._charging_outcome_recorded)

    def test_retries_exhausted_none_is_a_failure(self):
        """_fetch_with_retries returns None rather than raising once its
        retries run out -- that must not be mistaken for 'nothing to record'."""
        c = self._coord(status=self._status, charging=lambda: None)
        c.charging_freshness.record_success(T0)
        _run(c._run_update_cycle())
        self.assertEqual(c.charging_data_freshness, "stale")
        self.assertEqual(c.charging_freshness.last_error, "No response after retries")

    def test_exception_is_a_failure_with_its_message(self):
        c = self._coord(
            status=self._status,
            charging=self._raise(Exception("return code: 4, message: failed")),
        )
        _run(c._run_update_cycle())
        self.assertEqual(c.charging_data_freshness, "no_data")
        self.assertIn("return code: 4", c.charging_freshness.last_error)

    def test_timeout_is_a_failure(self):
        c = self._coord(
            status=self._status, charging=self._raise(asyncio.TimeoutError())
        )
        _run(c._run_update_cycle())
        self.assertEqual(c.charging_data_freshness, "no_data")
        self.assertEqual(
            c.charging_freshness.last_error,
            f"Timed out after {COORD_MOD.RUNTIME_CHARGING_TIMEOUT}s",
        )

    def test_ice_never_fetches_or_records(self):
        called = []
        c = self._coord(
            status=self._status,
            charging=lambda: called.append(1),
            vehicle_type="ICE",
        )
        _run(c._run_update_cycle())
        self.assertEqual(called, [])
        self.assertIsNone(c.charging_data_freshness)

    # ── Data Freshness: status exhausted its retries ──

    def test_status_none_after_retries_reports_failed_not_cached(self):
        """@HarryFlatter's log, 16:50 and 17:55: status and charging both came
        back None, the cycle 'succeeded', and Data Freshness read cached."""
        c = self._coord(status=lambda: None, charging=lambda: None)
        c.charging_freshness.record_success(T0)
        _run(c._run_update_cycle())
        self.assertEqual(c.data_freshness, COORD_MOD.DATA_FRESHNESS_FAILED)
        self.assertEqual(c.charging_data_freshness, "stale")

    def test_unchanged_status_still_reports_cached(self):
        c = self._coord(status=self._status, charging=lambda: NS(chrgMgmtData=NS()))
        _run(c._run_update_cycle())
        self.assertEqual(c.data_freshness, COORD_MOD.DATA_FRESHNESS_LIVE)
        _run(c._run_update_cycle())  # same statusTime again
        self.assertEqual(c.data_freshness, COORD_MOD.DATA_FRESHNESS_CACHED)


if __name__ == "__main__":
    unittest.main()
