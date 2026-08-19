"""Regression test for the holiday-mode update interval unit (PR #290).

The holiday-mode update interval is configured and defaulted in HOURS
(``DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS``), but was previously parsed through
the generic minutes helper in ``async_update_options`` — so a user-set value of
e.g. 12 became 12 *minutes* instead of 12 hours, polling ~60x too often (visible
in the "Last Update Time" sensor). These tests pin the correct behaviour so the
holiday line can't be silently collapsed back onto the minutes helper.

Runs under the repo's normal harness:
    python -m unittest discover -s tests -p "test_*.py"
"""

import unittest
from datetime import timedelta
from unittest.mock import MagicMock

from custom_components.mg_saic.coordinator import SAICMGDataUpdateCoordinator
from custom_components.mg_saic.const import CONF_HOLIDAY_UPDATE_INTERVAL


class HolidayIntervalUnitTest(unittest.IsolatedAsyncioTestCase):
    """async_update_options must interpret the holiday interval as hours."""

    def _make_coordinator(self) -> SAICMGDataUpdateCoordinator:
        """Build a bare coordinator without running __init__.

        We only exercise the option parsing in ``async_update_options``, so we
        avoid needing a real hass / client / config entry: create an
        uninitialised instance, seed the attributes the method reads as
        fallbacks, and stub the reschedule/notify tail so it doesn't touch HA
        scheduling.
        """
        coord = SAICMGDataUpdateCoordinator.__new__(SAICMGDataUpdateCoordinator)

        # Capability flags read via options.get("...", self.<flag>).
        coord.has_sunroof = False
        coord.has_heated_seats = False
        coord.has_rear_heated_seats = False
        coord.has_battery_heating = False
        coord.has_steering_wheel_heat = False
        coord.has_window_control = False
        coord.enable_shutdown_refresh_sequence = False
        coord.holiday_mode = False

        # Timedeltas the method reads as fallbacks (not used here because we
        # pass the option explicitly, but they must exist and be timedeltas).
        coord.holiday_update_interval = timedelta(hours=24)
        coord.stale_data_threshold = timedelta(hours=6)

        # Stub the parts that reschedule / notify HA listeners.
        coord._adjust_update_interval = MagicMock()
        coord.async_update_listeners = MagicMock()
        return coord

    async def test_holiday_interval_read_as_hours(self):
        coord = self._make_coordinator()
        await coord.async_update_options({CONF_HOLIDAY_UPDATE_INTERVAL: 12})
        self.assertEqual(coord.holiday_update_interval, timedelta(hours=12))

    async def test_holiday_interval_not_read_as_minutes(self):
        """Guard the exact regression: 12 must not become 12 minutes."""
        coord = self._make_coordinator()
        await coord.async_update_options({CONF_HOLIDAY_UPDATE_INTERVAL: 12})
        self.assertNotEqual(coord.holiday_update_interval, timedelta(minutes=12))

    async def test_holiday_interval_other_value(self):
        coord = self._make_coordinator()
        await coord.async_update_options({CONF_HOLIDAY_UPDATE_INTERVAL: 6})
        self.assertEqual(coord.holiday_update_interval, timedelta(hours=6))


if __name__ == "__main__":
    unittest.main()
