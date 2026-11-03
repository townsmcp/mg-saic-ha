"""A missing India charge cache must not trigger another poll or timeout."""

import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

with patch.dict("sys.modules", {
    "aiohttp": MagicMock(), "mg_ismart_india_client": MagicMock(),
}):
    import test_charging_data_freshness as freshness

COORD_MOD = freshness.COORD_MOD


class IndiaChargingNoDataTests(unittest.TestCase):
    def test_missing_cache_is_read_once_even_without_startup_poll_budget(self):
        status = NS(statusTime=1790011000, basicVehicleStatus=NS())
        c = freshness.UpdateCycleRecordingTests()._coord(
            status=lambda: status, charging=lambda: None, vehicle_type="BEV"
        )
        # Keep the real retry implementation as well as the real update cycle.
        del c._fetch_with_retries
        c.client = NS(
            _india_backend=True,
            get_vehicle_info=AsyncMock(return_value=[NS(vin=c.vin)]),
            get_vehicle_status=AsyncMock(return_value=status),
            get_charging_info=AsyncMock(return_value=None),
        )
        c.is_initial_setup = True
        c._note_refresh_step = MagicMock()
        c._show_saved_vehicle_data = MagicMock()
        with (
            patch.object(COORD_MOD, "_startup_wait", side_effect=[10, 0]),
            patch.object(COORD_MOD.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            self.assertLogs(COORD_MOD.LOGGER, "DEBUG") as logs,
        ):
            data = asyncio.run(c._run_update_cycle_inner())

        c.client.get_charging_info.assert_awaited_once_with(c.vin)
        c.client.get_vehicle_status.assert_awaited_once_with(c.vin)
        sleep.assert_not_awaited()
        self.assertIs(data["status"], status)
        self.assertIsNone(data["charging"])
        self.assertEqual(c.charging_data_freshness, "no_data")
        self.assertEqual(c.charging_freshness.last_error, "No cached charging frame")
        self.assertIsNone(c.charging_freshness.last_success)
        self.assertEqual(c.charging_freshness.consecutive_failures, 1)
        self.assertTrue(c._charging_outcome_recorded)
        self.assertFalse(any(r.levelno >= 30 for r in logs.records))
        self.assertNotRegex(
            " ".join(r.getMessage().lower() for r in logs.records),
            r"timed out|startup time used up|retry",
        )

    def test_source_clock_survives_replay_and_recovery_with_freshness_logs(self):
        source_times = (freshness.T0, freshness.T0, freshness.T0 + timedelta(minutes=3))
        receipt_times = tuple(freshness.T0 + timedelta(minutes=n) for n in (1, 2, 4))
        frames = [
            NS(
                india=True,
                chrgMgmtData=NS(bmsPackSOCDsp=620, bmsChrgSts=0),
                rvsChargeStatus=NS(india=True, measurement_time=stamp.timestamp()),
            )
            for stamp in source_times
        ]
        status = NS(statusTime=freshness.T0.timestamp(), basicVehicleStatus=NS())
        c = freshness.UpdateCycleRecordingTests()._coord(
            status=lambda: status, charging=lambda: None, vehicle_type="BEV"
        )
        del c._fetch_with_retries
        c.client = NS(
            _india_backend=True,
            get_vehicle_info=AsyncMock(return_value=[NS(vin=c.vin)]),
            get_vehicle_status=AsyncMock(return_value=status),
            get_charging_info=AsyncMock(side_effect=frames),
        )
        c.counter_reset_guard = COORD_MOD.SinceChargeCounterGuard()
        c.last_charging_data = None

        with (
            patch.object(COORD_MOD, "datetime", wraps=datetime) as clock,
            patch.object(COORD_MOD.asyncio, "sleep", new_callable=AsyncMock) as sleep,
            self.assertLogs(COORD_MOD.LOGGER, "INFO") as logs,
        ):
            for index, (measured, received, state, failures) in enumerate(zip(
                source_times, receipt_times, ("live", "stale", "live"), (0, 1, 0)
            )):
                with self.subTest(cycle=index):
                    clock.now.return_value = received
                    data = asyncio.run(c._run_update_cycle_inner())
                    self.assertIs(data["charging"], frames[index])
                    self.assertEqual(c.charging_data_freshness, state)
                    self.assertEqual(c.charging_freshness.last_success, measured)
                    self.assertEqual(c.charging_freshness.last_attempt, received)
                    self.assertEqual(c.charging_freshness.consecutive_failures, failures)
                    self.assertTrue(c._charging_outcome_recorded)
                    self.assertEqual(c.client.get_charging_info.await_count, index + 1)
                    if failures:
                        self.assertEqual(
                            c.charging_freshness.last_error,
                            "No advancing India charge measurement time",
                        )
                    else:
                        self.assertIsNone(c.charging_freshness.last_error)

        sleep.assert_not_awaited()
        transitions = [
            r.getMessage() for r in logs.records
            if r.getMessage().startswith("Charging data for VIN")
        ]
        self.assertEqual(len(transitions), 2)
        self.assertIn("not available (No advancing India charge measurement time)", transitions[0])
        self.assertIn("back after", transitions[1])
        self.assertFalse(any(r.levelno >= 30 for r in logs.records))


class IndiaChargingFlagExpiryTests(unittest.TestCase):
    """Missing or replayed charge frames cannot hold the active flag forever."""

    T0 = freshness.T0.timestamp()

    def _coordinator(self):
        c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(
            COORD_MOD.SAICMGDataUpdateCoordinator
        )
        c.client = NS(_india_backend=True)
        c.vin = "SYNTHETIC-VIN"
        c.trip_stats = None
        c.is_charging = c._prev_is_charging = False
        c.is_powered_on = c._prev_is_powered_on = False
        c.last_vehicle_activity = None
        for name in (
            "_save_vehicle_data", "_update_ventilation_from_status",
            "_note_charging_flag", "_update_charge_state",
            "_save_activity_times_if_changed", "_mark_reachable",
            "async_update_listeners",
        ):
            setattr(c, name, MagicMock())
        c._detect_activity = MagicMock(return_value=False)
        return c

    @staticmethod
    def _status(stamp):
        return NS(india=True, basicVehicleStatus=NS(
            india=True, measurement_time=stamp, powerMode=None,
        ))

    def _charging(self, stamp=None, code=3):
        stamp = self.T0 if stamp is None else stamp
        return NS(
            india=True,
            chrgMgmtData=NS(india=True, measurement_time=stamp, bmsChrgSts=code),
            rvsChargeStatus=NS(india=True, measurement_time=stamp),
        )

    def test_flag_expires_without_fabricating_charge_stop_activity(self):
        for status_advances in (True, False):
            for replay in (False, True):
                with self.subTest(status_advances=status_advances, replay=replay):
                    c = self._coordinator()
                    with patch.object(COORD_MOD, "datetime", wraps=datetime) as clock:
                        clock.now.return_value = freshness.T0
                        c._update_state({"status": self._status(self.T0), "charging": self._charging()})
                        self.assertTrue(c.is_charging)
                        for seconds, expected in ((600, True), (1800, True), (1801, False)):
                            later = freshness.T0 + timedelta(seconds=seconds)
                            clock.now.return_value = later
                            stamp = later.timestamp() if status_advances else self.T0
                            c._update_state({
                                "status": self._status(stamp),
                                "charging": self._charging() if replay else None,
                            })
                            self.assertIs(c.is_charging, expected, f"after {seconds} s")
                            self.assertIs(c._prev_is_charging, expected)
                            self.assertEqual(c._india_state_charge_time, self.T0)
                            self.assertIsNone(c.last_vehicle_activity)
                            c._mark_reachable.assert_not_called()

    def test_fresh_charge_frame_recovers_after_expiry_and_real_stop_keeps_its_time(self):
        c = self._coordinator()
        with patch.object(COORD_MOD, "datetime", wraps=datetime) as clock:
            clock.now.return_value = freshness.T0
            c._update_state({"status": self._status(self.T0), "charging": self._charging()})
            later = freshness.T0 + timedelta(minutes=31)
            clock.now.return_value = later
            c._update_state({"status": self._status(self.T0), "charging": None})
            self.assertFalse(c.is_charging)
            c._update_state({"status": self._status(self.T0), "charging": self._charging(later.timestamp())})
            self.assertTrue(c.is_charging)
            self.assertTrue(c._prev_is_charging)
            self.assertIsNone(c.last_vehicle_activity)

            stopped = later + timedelta(minutes=5)
            clock.now.return_value = stopped + timedelta(seconds=20)
            c._update_state({"status": self._status(self.T0), "charging": self._charging(stopped.timestamp(), code=0)})
            self.assertFalse(c.is_charging)
            self.assertEqual(c.last_vehicle_activity, stopped)
