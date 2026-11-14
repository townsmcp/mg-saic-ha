"""Charging data outages in the log, and Mileage (the odometer) through one.

20 Oct 2026: SAIC's charging data went missing for about two hours on more
than one car. @HarryFlatter's HS PHEV (#445) went stale at 07:00 and came back
about two hours later; James's MGS6 went stale at 06:47:16 and came back at
08:54:08 (BST). Harry only knew from an alert on Charging Data Freshness:
nothing in the log said when it went or came back.

Separately, Mileage (the odometer) on a BEV/PHEV needed charging data before it had a value,
so a restart during an outage like that left it unavailable until charging
data came back, with a good odometer sitting in the status the whole time.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from test_erac_fallback import LOGIC, SENSOR
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

COORD_MOD = sys.modules["mg_saic.coordinator"]
STALE_AT = datetime(2026, 10, 20, 5, 47, 16, tzinfo=timezone.utc)  # 06:47:16 BST
BACK_AT = datetime(2026, 10, 20, 7, 54, 8, tzinfo=timezone.utc)  # 08:54:08 BST
LAST_GOOD = datetime(2026, 10, 20, 5, 0, tzinfo=timezone.utc)  # 06:00 BST
BST = timezone(timedelta(hours=1))
OUTAGE = "return code: 4, message: The remote control instruction failed"


class FormatOutageTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(LOGIC.format_outage(20), "under a minute")
        self.assertEqual(LOGIC.format_outage(45 * 60 + 59), "45 min")
        self.assertEqual(LOGIC.format_outage(3600), "1 h")
        self.assertEqual(LOGIC.format_outage((BACK_AT - STALE_AT).total_seconds()), "2 h 6 min")


class MessageTests(unittest.TestCase):
    def _change(self, tracker, record, now):
        before = tracker.state, tracker.stale_since
        record(now)
        return LOGIC.charging_freshness_change(*before, tracker, now, tz=BST)

    def test_james_morning(self):
        t = LOGIC.ChargingFreshnessTracker()
        t.record_success(LAST_GOOD)
        gone = self._change(t, lambda n: t.record_failure(n, OUTAGE), STALE_AT)
        self.assertEqual(
            gone,
            f"not available ({OUTAGE}); charging sensors are holding the values from 06:00",
        )
        # Further failures say nothing more.
        later = STALE_AT + timedelta(hours=1)
        self.assertIsNone(self._change(t, lambda n: t.record_failure(n, OUTAGE), later))
        back = self._change(t, t.record_success, BACK_AT)
        self.assertEqual(back, "back after 2 h 6 min")
        # And nothing while it stays live.
        self.assertIsNone(self._change(t, t.record_success, BACK_AT + timedelta(hours=1)))

    def test_first_fetch_after_startup_is_quiet(self):
        t = LOGIC.ChargingFreshnessTracker()
        self.assertIsNone(self._change(t, t.record_success, LAST_GOOD))

    def test_none_since_startup(self):
        t = LOGIC.ChargingFreshnessTracker()
        msg = self._change(t, lambda n: t.record_failure(n, "Timed out after 20s"), STALE_AT)
        self.assertEqual(
            msg,
            "not available yet (Timed out after 20s); charging sensors stay unknown until it arrives",
        )
        self.assertEqual(self._change(t, t.record_success, BACK_AT), "back after 2 h 6 min")


class CoordinatorLogTests(unittest.TestCase):
    def _coordinator(self):
        c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(COORD_MOD.SAICMGDataUpdateCoordinator)
        c.vin = "VIN1"
        c.charging_freshness = COORD_MOD.ChargingFreshnessTracker()
        return c

    def test_one_line_each_way(self):
        c = self._coordinator()
        c._record_charging_outcome(LAST_GOOD)
        with self.assertLogs(COORD_MOD.LOGGER, "INFO") as logs:
            c._record_charging_outcome(STALE_AT, OUTAGE)
            c._record_charging_outcome(STALE_AT + timedelta(hours=1), OUTAGE)
            c._record_charging_outcome(BACK_AT)
            c._record_charging_outcome(BACK_AT + timedelta(hours=1))
        lines = [r.getMessage() for r in logs.records]
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("Charging data for VIN VIN1: not available (return code: 4", lines[0])
        self.assertEqual(lines[1], "Charging data for VIN VIN1: back after 2 h 6 min")

    def test_still_records_the_outcome(self):
        c = self._coordinator()
        c._record_charging_outcome(LAST_GOOD)
        c._record_charging_outcome(STALE_AT, OUTAGE)
        self.assertEqual(c.charging_data_freshness, "stale")
        self.assertEqual(c.charging_freshness.last_error, OUTAGE)

    def test_tracker_writes_are_centralized(self):
        from pathlib import Path

        source = Path(COORD_MOD.__file__).read_text()
        self.assertNotIn("self.charging_freshness.record_", source)


def _mileage_sensor(*, status_mileage, charging=None, vehicle_type="PHEV"):
    sensor = SENSOR.SAICMGMileageSensor.__new__(SENSOR.SAICMGMileageSensor)
    status = NS(basicVehicleStatus=NS(mileage=status_mileage))
    sensor.coordinator = NS(
        backend_supports=lambda feature: True,
        last_update_success=True,
        data={"status": status, "charging": charging},
    )
    sensor._vehicle_type = vehicle_type
    sensor._last_valid_mileage = None
    sensor._status_type = "basicVehicleStatus"
    sensor._charging_status_type = "rvsChargeStatus"
    sensor._field = "mileage"
    sensor._factor = 0.1
    sensor._name = "Odometer"
    return sensor


class OdometerWithoutChargingDataTests(unittest.TestCase):
    def test_restart_during_an_outage(self):
        # Harry's HS PHEV on 20 Oct: status mileage 65300, no charging data.
        for vehicle_type in ("BEV", "PHEV"):
            sensor = _mileage_sensor(status_mileage=65300, vehicle_type=vehicle_type)
            self.assertTrue(sensor.available, vehicle_type)
            self.assertEqual(sensor.native_value, 6530.0)

    def test_stuck_status_odometer_still_needs_charging_data(self):
        # #280: status mileage stuck at 6553.5 km; the real one is in charging data.
        sensor = _mileage_sensor(status_mileage=SENSOR.MILEAGE_UINT16_SATURATION)
        self.assertFalse(sensor.available)
        charging = NS(rvsChargeStatus=NS(mileage=123456))
        sensor = _mileage_sensor(status_mileage=SENSOR.MILEAGE_UINT16_SATURATION, charging=charging)
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 12345.6)

    def test_no_odometer_anywhere(self):
        for bad in (None, 0, -128):
            self.assertFalse(_mileage_sensor(status_mileage=bad).available, bad)


if __name__ == "__main__":
    unittest.main()
