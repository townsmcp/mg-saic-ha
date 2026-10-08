"""Lock-engaged trigger must not fire on auto-lock while driving.

Many cars lock themselves once they pass a set speed -- an unlocked -> locked
transition mid-drive. The trigger meant for "just arrived home, about to plug
in" started the post-shutdown refresh sequence on it: seen live on a MGS6 at
07:39 (powerMode 2, 17.5 km/h, 9 km into the trip), costing two pointless
refreshes on every drive. Now it only fires with the car off; switching the
car off starts the same sequence through the power-off trigger anyway.

Reuses the stubbed coordinator from test_setup_and_config_flow.
"""

import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]


def _coordinator(*, is_charging=False):
    c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(COORD_MOD.SAICMGDataUpdateCoordinator)
    c.vin = "TESTVIN"
    c.is_charging = is_charging
    c.enable_shutdown_refresh_sequence = True
    c._shutdown_refresh_task = None
    c._start_shutdown_refresh_sequence = MagicMock()
    return c


def _status(*, lock, power_mode):
    return NS(lockStatus=lock, powerMode=power_mode, driverDoor=0, passengerDoor=0,
              rearLeftDoor=0, rearRightDoor=0, bootStatus=0, bonnetStatus=0,
              remoteClimateStatus=0, rmtHtdRrWndSt=0, engineStatus=0)


def _lock(c, *, power_mode):
    """Unlocked, then locked, both at the given power mode."""
    c._detect_activity(_status(lock=0, power_mode=power_mode))
    c._start_shutdown_refresh_sequence.reset_mock()
    c._detect_activity(_status(lock=1, power_mode=power_mode))


class LockTriggerTests(unittest.TestCase):
    def test_auto_lock_while_driving_does_not_start_the_sequence(self):
        c = _coordinator()
        _lock(c, power_mode=2)  # the 07:39 case
        c._start_shutdown_refresh_sequence.assert_not_called()

    def test_locking_with_the_car_off_still_starts_it(self):
        """Arriving home: car off, then locked -- unchanged."""
        c = _coordinator()
        _lock(c, power_mode=0)
        c._start_shutdown_refresh_sequence.assert_called_once()

    def test_unknown_power_mode_keeps_the_old_behaviour(self):
        c = _coordinator()
        _lock(c, power_mode=None)
        c._start_shutdown_refresh_sequence.assert_called_once()

    def test_still_not_started_while_charging(self):
        c = _coordinator(is_charging=True)
        _lock(c, power_mode=0)
        c._start_shutdown_refresh_sequence.assert_not_called()

    def test_lock_while_driving_is_still_recorded_as_activity(self):
        """Only the sequence is skipped; the lock still counts as activity."""
        c = _coordinator()
        c._detect_activity(_status(lock=0, power_mode=2))
        self.assertTrue(c._detect_activity(_status(lock=1, power_mode=2)))


if __name__ == "__main__":
    unittest.main()
