"""A missing India charge cache must not trigger another poll or timeout."""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

with patch.dict("sys.modules", {
    "aiohttp": MagicMock(), "mg_ismart_india_client": MagicMock(),
}):
    import test_charging_data_freshness as freshness

COORD_MOD = freshness.COORD_MOD


class IndiaChargingNoDataTests(unittest.TestCase):
    def test_missing_cache_is_read_once_even_without_startup_poll_budget(self):
        status = NS(statusTime=1790011000)
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
