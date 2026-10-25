"""Home Assistant starting while the car is asleep (#425, @jmerifjKriwe).

At startup the first refresh has STARTUP_API_TIMEOUT (30 s). A sleeping car
doesn't answer, and SAIC keeps trying it for about 30 s per status request
(@SteveMSJ's log, 10 Oct: 09:13:00 to 09:13:30), with 15 s retries on top,
so the 30 s always ran out: setup failed, and every retry Home Assistant made
asked the sleeping car again and failed the same way until it woke.

Now the status gets STARTUP_STATUS_TIMEOUT at startup, one attempt, and
setup finishes without it; the charging request is skipped too.
"""

import asyncio
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
CONST = sys.modules["mg_saic.const"]
PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"


def _coord(status, *, initial_setup=True):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c._code4_this_cycle = False
    c._consecutive_unreachable_polls = 0
    c._last_command_unreachable = False
    c._scheduled_refresh = False
    c._gave_up_car_asleep = False
    c._api_lock = None
    c.config_entry = NS(data={"vin": "TESTVIN"})
    c.client = NS(get_vehicle_info=None, get_vehicle_status=None, get_charging_info=None)
    c.vehicle_type = "BEV"
    c.is_initial_setup = initial_setup
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
    c.charging_calls = []

    async def scripted(_fetch, _is_generic, name):
        if name == "vehicle info":
            return [NS(vin="TESTVIN")]
        if name == "vehicle status":
            return await status()
        c.charging_calls.append(1)
        return NS(chrgMgmtData=NS())

    c._fetch_with_retries = scripted
    return c


async def _asleep():
    # The car never answers.
    await asyncio.sleep(3600)


async def _awake():
    return NS(statusTime=1790011000)


class StartupTests(unittest.TestCase):
    def test_a_sleeping_car_does_not_stop_setup(self):
        c = _coord(_asleep)
        with patch.object(COORD_MOD, "STARTUP_STATUS_TIMEOUT", 0.05):
            data = asyncio.run(c._run_update_cycle())
        self.assertIsNone(data["status"])
        self.assertEqual(data["info"][0].vin, "TESTVIN")
        self.assertTrue(c._gave_up_car_asleep)
        # Charging comes from the same car: not asked.
        self.assertEqual(c.charging_calls, [])

    def test_an_awake_car_is_read_as_before(self):
        c = _coord(_awake)
        with patch.object(COORD_MOD, "STARTUP_STATUS_TIMEOUT", 0.05):
            data = asyncio.run(c._run_update_cycle())
        self.assertEqual(data["status"].statusTime, 1790011000)
        self.assertEqual(c.charging_calls, [1])

    def test_after_startup_there_is_no_cut_off(self):
        # Later refreshes keep their own retry rules (the car-asleep single
        # attempt once it's flagged unreachable), not the startup cap.
        async def slow():
            await asyncio.sleep(0.1)
            return NS(statusTime=1790011000)

        c = _coord(slow, initial_setup=False)
        with patch.object(COORD_MOD, "STARTUP_STATUS_TIMEOUT", 0.05):
            data = asyncio.run(c._run_update_cycle())
        self.assertIsNotNone(data["status"])

    def test_startup_budget(self):
        # The 30 s startup allowance is unchanged. Since beta12 the status may
        # have up to 25 s of it (a waking car can take over 20 s), and every
        # wait is taken from what's left, so the two caps can add up to more.
        self.assertEqual(CONST.STARTUP_API_TIMEOUT, 30)
        self.assertEqual(CONST.STARTUP_STATUS_TIMEOUT, 25)
        self.assertLess(
            CONST.STARTUP_STATUS_TIMEOUT + CONST.STARTUP_SAFETY_MARGIN,
            CONST.STARTUP_API_TIMEOUT,
        )


class SharingTheThirtySecondsTests(unittest.TestCase):
    """@HarryFlatter's HS took 24 s to answer as it woke (14 Oct)."""

    def test_wait_helper(self):
        self.assertEqual(COORD_MOD._startup_wait(25, None, 0), 25)
        self.assertEqual(COORD_MOD._startup_wait(25, 28.0, 0.0), 25)
        self.assertEqual(COORD_MOD._startup_wait(12, 28.0, 24.0), 4.0)
        self.assertEqual(COORD_MOD._startup_wait(12, 28.0, 29.0), 0.0)

    def _run(self, status_after):
        async def waking():
            await asyncio.sleep(status_after)
            return NS(statusTime=1790011000)

        c = _coord(waking)
        with patch.multiple(
            COORD_MOD,
            STARTUP_API_TIMEOUT=0.5,
            STARTUP_SAFETY_MARGIN=0.0,
            STARTUP_STATUS_TIMEOUT=0.45,
            STARTUP_CHARGING_MIN=0.2,
        ):
            data = asyncio.run(c._run_update_cycle())
        return c, data

    def test_a_slow_waking_car_still_gets_its_status(self):
        # Answers at 0.3 of 0.5: past the old share, inside the new one.
        c, data = self._run(0.3)
        self.assertIsNotNone(data["status"])
        self.assertFalse(c._gave_up_car_asleep)

    def test_charging_skipped_when_the_status_used_the_time(self):
        # 0.5 - 0.35 leaves 0.15, under the minimum.
        c, data = self._run(0.35)
        self.assertIsNotNone(data["status"])
        self.assertEqual(c.charging_calls, [])
        self.assertIsNone(data["charging"])

    def test_a_quick_car_gets_its_charging_too(self):
        c, data = self._run(0.0)
        self.assertEqual(c.charging_calls, [1])

    def test_a_car_that_never_answers_stays_inside_the_allowance(self):
        c, data = self._run(3600)
        self.assertIsNone(data["status"])
        self.assertTrue(c._gave_up_car_asleep)


class OneAttemptAtStartupTests(unittest.TestCase):
    def test_a_car_not_answering_at_startup_gets_one_attempt(self):
        c = _coord(_awake)
        self.assertTrue(c._car_asleep_single_attempt())

    def test_after_startup_the_usual_rule(self):
        c = _coord(_awake, initial_setup=False)
        self.assertFalse(c._car_asleep_single_attempt())
        c._scheduled_refresh = True
        c._last_command_unreachable = True
        self.assertTrue(c._car_asleep_single_attempt())


class SetupRetriesTests(unittest.TestCase):
    def test_an_unexpected_coordinator_error_is_retried(self):
        source = (PKG_DIR / "__init__.py").read_text()
        block = source.split("await coordinator.async_setup()", 1)[1][:900]
        self.assertIn("raise ConfigEntryNotReady(", block)
        self.assertNotIn("return False", block.split("domain[f\"{entry.entry_id}_coordinator\"]")[0])


if __name__ == "__main__":
    unittest.main()
