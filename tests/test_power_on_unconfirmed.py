"""A power-on that nothing confirms stops the fast polling (#472).

@stfvrg, 6 Nov 2026 (Marvel R, EP21): after the 12 V battery was replaced,
the car's clock reset and every status came back stamped around 2019-01-01,
so each one was thrown away as untrustworthy. A Vehicle Start message had just
set the car to powered on, and only an accepted status can switch that off,
so the parked car was polled (and woken) every 15 minutes for about 3 hours.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
CONST = sys.modules["mg_saic.const"]
LIMIT = CONST.POWER_ON_UNCONFIRMED_LIMIT


def _coordinator():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "VIN1"
    c.client = None
    c.is_powered_on = False
    c.is_charging = False
    c._prev_is_powered_on = False
    c._prev_is_charging = False
    c._power_on_confirmed_at = None
    c._power_on_unconfirmed = False
    c.last_powered_on_time = None
    c.last_powered_off_time = None
    c.last_vehicle_activity = None
    c.last_charging_data = None
    c._shutdown_refresh_task = None
    c.enable_shutdown_refresh_sequence = True
    for name in (
        "_save_vehicle_data",
        "_update_ventilation_from_status",
        "_update_trip_state",
        "_note_charging_flag",
        "_update_charge_state",
        "_save_activity_times_if_changed",
        "_mark_reachable",
        "_start_shutdown_refresh_sequence",
        "_adjust_update_interval",
        "async_update_listeners",
    ):
        setattr(c, name, MagicMock())
    c._detect_activity = MagicMock(return_value=False)
    return c


def _status(power_mode):
    return NS(basicVehicleStatus=NS(powerMode=power_mode))


def _ago(c, minutes):
    """Pretend the last confirmation was ``minutes`` ago."""
    c._power_on_confirmed_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)


class Issue472Tests(unittest.TestCase):
    def test_the_report(self):
        # Vehicle Start message, then statuses that are all thrown away.
        c = _coordinator()
        c.hint_vehicle_started(datetime.now(timezone.utc))
        self.assertTrue(c.is_powered_on)
        c._update_state({"status": None, "charging": None})
        self.assertTrue(c.is_powered_on)  # 15 minutes on: still within the limit
        _ago(c, 31)
        c._update_state({"status": None, "charging": None})
        self.assertFalse(c.is_powered_on)
        # Only the fast polling stops: nothing was seen, so nothing recorded.
        self.assertIsNone(c.last_powered_off_time)
        c._start_shutdown_refresh_sequence.assert_not_called()

    def test_a_status_without_a_power_mode_doesnt_confirm(self):
        c = _coordinator()
        c.hint_vehicle_started(datetime.now(timezone.utc))
        _ago(c, 31)
        c._update_state({"status": _status(None), "charging": None})
        self.assertFalse(c.is_powered_on)

    def test_the_limit_is_two_powered_polls(self):
        self.assertEqual(LIMIT, timedelta(minutes=30))


class DrivingWithoutSignalTests(unittest.TestCase):
    """A drive that loses signal for an hour, then gets it back."""

    def _drive_then_lose_signal(self):
        c = _coordinator()
        c._update_state({"status": _status(2), "charging": None})
        self.started = c.last_powered_on_time
        self.assertTrue(c.is_powered_on)
        _ago(c, 45)
        c._update_state({"status": None, "charging": None})
        self.assertFalse(c.is_powered_on)
        return c

    def test_signal_back_while_still_driving(self):
        c = self._drive_then_lose_signal()
        c._update_state({"status": _status(2), "charging": None})
        self.assertTrue(c.is_powered_on)
        # The same drive: Last Powered On isn't moved to now.
        self.assertEqual(c.last_powered_on_time, self.started)
        self.assertFalse(c._power_on_unconfirmed)

    def test_signal_back_once_parked(self):
        c = self._drive_then_lose_signal()
        c._update_state({"status": _status(0), "charging": None})
        self.assertFalse(c.is_powered_on)
        # Handled as the end of the drive, as it would have been.
        self.assertIsNotNone(c.last_powered_off_time)
        c._start_shutdown_refresh_sequence.assert_called_once()
        self.assertFalse(c._power_on_unconfirmed)

    def test_a_good_status_renews_it(self):
        c = _coordinator()
        c._update_state({"status": _status(2), "charging": None})
        _ago(c, 25)
        c._update_state({"status": _status(2), "charging": None})
        c._update_state({"status": None, "charging": None})
        self.assertTrue(c.is_powered_on)


class UnchangedTests(unittest.TestCase):
    def test_parked_car_unaffected(self):
        c = _coordinator()
        c._update_state({"status": None, "charging": None})
        self.assertFalse(c.is_powered_on)
        self.assertFalse(c._power_on_unconfirmed)
        c._update_state({"status": _status(0), "charging": None})
        self.assertIsNone(c.last_powered_off_time)
        c._start_shutdown_refresh_sequence.assert_not_called()

    def test_a_new_start_message_after_it_lapsed(self):
        c = _coordinator()
        c.hint_vehicle_started(datetime.now(timezone.utc) - timedelta(hours=2))
        _ago(c, 31)
        c._update_state({"status": None, "charging": None})
        later = datetime.now(timezone.utc)
        c.hint_vehicle_started(later)
        self.assertTrue(c.is_powered_on)
        self.assertEqual(c.last_powered_on_time, later)


if __name__ == "__main__":
    unittest.main()
