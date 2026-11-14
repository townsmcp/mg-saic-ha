"""Charging Completion Time, like the iSmart app's Completion Time.

James's MGS6, 14 Nov 2026: the charging data received at 21:55 had
chrgngRmnngTime 441, and the app showed Completion Time 05:16, which is
21:55 + 7 h 21 min. The API has no completion field: it's the time plus the
time remaining.
"""

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

from test_erac_fallback import SENSOR

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"
TAKEN = datetime(2026, 11, 14, 21, 55, 14, tzinfo=timezone.utc)
ENTRY = NS(entry_id="e")


def _coordinator(status=1, remaining=441, flag=0, estimate=None, taken=TAKEN):
    chrg = NS(bmsChrgSts=status, chrgngRmnngTime=remaining, chrgngRmnngTimeV=flag)
    return NS(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV"),
        data={"status": None, "charging": NS(chrgMgmtData=chrg)},
        charging_freshness=NS(last_success=taken),
        estimated_remaining_charge=lambda: estimate,
    )


def _sensor(**kwargs):
    c = _coordinator(**kwargs)
    return SENSOR.SAICMGChargingCompletionTimeSensor(c, ENTRY), c


class AppMatchTests(unittest.TestCase):
    def test_the_app_screenshot(self):
        sensor, _ = _sensor()
        self.assertEqual(
            sensor.native_value, datetime(2026, 11, 15, 5, 16, 14, tzinfo=timezone.utc)
        )
        self.assertEqual(
            sensor.extra_state_attributes, {"charging": True, "source": "car"}
        )

    def test_counts_from_the_reading_not_now(self):
        # The same reading gives the same time however long after it's read.
        sensor, _ = _sensor()
        self.assertEqual(sensor.native_value, sensor.native_value)

    def test_identity(self):
        sensor, _ = _sensor()
        self.assertEqual(sensor.name, "MG MGS6 EV Charging Completion Time")
        self.assertEqual(sensor.unique_id, "e_VIN1_charging_completion_time")
        self.assertIsNone(sensor._attr_state_class)


class SourceTests(unittest.TestCase):
    def test_car_says_no_time_yet_uses_the_estimate(self):
        sensor, _ = _sensor(remaining=1023, flag=1, estimate=timedelta(hours=6))
        self.assertEqual(sensor.native_value, TAKEN.replace(microsecond=0) + timedelta(hours=6))
        self.assertEqual(sensor.extra_state_attributes["source"], "estimated")

    def test_no_time_at_all_keeps_the_last(self):
        sensor, c = _sensor()
        first = sensor.native_value
        c.data["charging"] = NS(chrgMgmtData=NS(bmsChrgSts=1, chrgngRmnngTime=1023, chrgngRmnngTimeV=1))
        self.assertEqual(sensor.native_value, first)
        self.assertEqual(sensor.extra_state_attributes["source"], "unknown")


class NotChargingTests(unittest.TestCase):
    def test_keeps_the_last_value(self):
        sensor, c = _sensor()
        first = sensor.native_value
        for status in (0, 5, 13):
            c.data["charging"] = NS(chrgMgmtData=NS(bmsChrgSts=status, chrgngRmnngTime=1023, chrgngRmnngTimeV=1))
            self.assertEqual(sensor.native_value, first)
            self.assertFalse(sensor.extra_state_attributes["charging"])

    def test_no_charging_data_keeps_the_last(self):
        sensor, c = _sensor()
        first = sensor.native_value
        c.data["charging"] = None
        self.assertEqual(sensor.native_value, first)

    def test_restored_after_a_restart(self):
        sensor, c = _sensor(status=0)

        async def last_state():
            return NS(state="2026-11-15T05:16:14+00:00", attributes={"source": "car"})

        sensor.async_get_last_state = last_state
        asyncio.run(sensor.async_added_to_hass())
        self.assertEqual(sensor.native_value, datetime(2026, 11, 15, 5, 16, 14, tzinfo=timezone.utc))
        self.assertFalse(sensor.extra_state_attributes["charging"])

    def test_nothing_to_restore(self):
        sensor, c = _sensor(status=0)

        async def last_state():
            return NS(state="unknown", attributes={})

        sensor.async_get_last_state = last_state
        asyncio.run(sensor.async_added_to_hass())
        self.assertIsNone(sensor.native_value)


class RegistrationTests(unittest.TestCase):
    def test_added_with_the_charging_sensors(self):
        source = (PKG / "sensor.py").read_text()
        self.assertIn("sensors.append(SAICMGChargingCompletionTimeSensor(coordinator, entry))", source)


if __name__ == "__main__":
    unittest.main()
