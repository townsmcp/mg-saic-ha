"""The checks after switching the car off stop while Quiet Charging is on.

@paulfcarney-ship-it's MG HS PHEV, 15 Oct 2026 (times BST), Quiet Charging
19:00-07:00:

    18:50  car switched off; the post-shutdown checks start (1, 3, 7, 15
           and 25 minutes later)
    18:59  plugged in, charging
    19:00  Quiet Charging on
    19:06  check: a live poll (5%), the charging data too slow to answer
    19:16  check: a live poll (7%), the same

Both live polls reached the car mid-charge during quiet hours. On an HS that
can turn the lights on.
"""

import asyncio
import sys
import unittest
from unittest.mock import AsyncMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator


def _coordinator(*, quiet_from_step=None, powered_on=False):
    """quiet_from_step: the check (0-based) from which quiet charging is on."""
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.is_charging = False
    c.is_powered_on = powered_on
    c.quiet_hours = False
    c.live_polling = True
    c._shutdown_refresh_task = None
    steps = {"n": 0}

    async def fake_sleep(_delay):
        if quiet_from_step is not None and steps["n"] >= quiet_from_step:
            c.quiet_hours = True
            c.live_polling = False
        steps["n"] += 1

    c.async_request_refresh = AsyncMock()
    return c, fake_sleep


def _run(c, fake_sleep):
    with patch.object(COORD_MOD.asyncio, "sleep", fake_sleep):
        asyncio.run(c._run_shutdown_refresh_sequence())
    return c.async_request_refresh.await_count


class PaulsEveningTests(unittest.TestCase):
    def test_checks_stop_when_quiet_charging_starts(self):
        # 18:51, 18:53 and 18:57 before 19:00; 19:05 and 19:15 after.
        c, sleep = _coordinator(quiet_from_step=3)
        self.assertEqual(_run(c, sleep), 3)

    def test_without_quiet_charging_all_five_run(self):
        c, sleep = _coordinator()
        self.assertEqual(_run(c, sleep), len(COORD_MOD.POST_SHUTDOWN_REFRESH_SEQUENCE))


class WhenTheyStillRunTests(unittest.TestCase):
    def test_switched_off_during_quiet_charging(self):
        c, sleep = _coordinator(quiet_from_step=0)
        self.assertEqual(_run(c, sleep), 0)

    def test_live_polling_turned_back_on(self):
        # Quiet hours configured, but Live Polling on: not quiet.
        c, sleep = _coordinator()
        c.quiet_hours = True
        c.live_polling = True
        self.assertEqual(_run(c, sleep), len(COORD_MOD.POST_SHUTDOWN_REFRESH_SEQUENCE))

    def test_car_switched_on_again(self):
        # A car that is on is polled as normal, quiet charging or not.
        c, sleep = _coordinator(quiet_from_step=0, powered_on=True)
        self.assertEqual(_run(c, sleep), len(COORD_MOD.POST_SHUTDOWN_REFRESH_SEQUENCE))

    def test_still_ends_early_on_charging(self):
        c, sleep = _coordinator()

        async def refresh():
            c.is_charging = True

        c.async_request_refresh = AsyncMock(side_effect=refresh)
        self.assertEqual(_run(c, sleep), 1)

    def test_the_task_is_cleared(self):
        c, sleep = _coordinator(quiet_from_step=1)
        c._shutdown_refresh_task = object()
        _run(c, sleep)
        self.assertIsNone(c._shutdown_refresh_task)


if __name__ == "__main__":
    unittest.main()
