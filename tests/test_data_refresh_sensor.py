"""Data Refresh: shows when the integration is polling the car.

James's MGS6, 15 Oct 2026, 13:35: Update Vehicle Data pressed with the car
not answering. The poll ran until about 13:39:30 (5 tries of ~45 s each,
all code 4) and nothing on screen changed meanwhile: Last Update Time still
said "6 minutes ago", so it looked as if the button did nothing.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_charging_controls_dropout import MODS, _stub  # noqa: F401 - platform stubs
import test_setup_and_config_flow

COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator


class _Any:
    def __getattr__(self, name):
        return name.lower()


class _Entity:
    def __init__(self, *args, **kwargs):
        pass


_stub("homeassistant.components.binary_sensor",
      BinarySensorEntity=_Entity, BinarySensorDeviceClass=_Any())
BINARY = test_setup_and_config_flow._load(
    "mg_saic.binary_sensor", test_setup_and_config_flow.PKG_DIR / "binary_sensor.py",
    force=True,
)


def _coordinator():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    return c


class RefreshStateTests(unittest.TestCase):
    def test_on_while_the_poll_runs_and_off_after(self):
        c = _coordinator()
        seen = []

        async def poll():
            seen.append((c.refresh_in_progress, c.refresh_step))
            c._note_refresh_step("vehicle status", 1)
            seen.append((c.refresh_in_progress, c.refresh_step))
            c._note_refresh_step("vehicle status", 3)
            seen.append((c.refresh_in_progress, c.refresh_step))
            return {"status": None}

        c._run_update_cycle_inner = poll
        asyncio.run(c._run_update_cycle())
        self.assertEqual(seen, [
            (True, "vehicle info"),
            (True, "vehicle status"),
            (True, "vehicle status, try 3 of 5"),
        ])
        self.assertFalse(c.refresh_in_progress)
        self.assertIsNone(c.refresh_step)
        self.assertIsNotNone(c.last_refresh_duration_s)

    def test_off_after_a_poll_that_fails(self):
        c = _coordinator()
        c._run_update_cycle_inner = AsyncMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            asyncio.run(c._run_update_cycle())
        self.assertFalse(c.refresh_in_progress)

    def test_listeners_hear_each_change(self):
        c = _coordinator()
        heard = MagicMock()
        remove = c.add_refresh_listener(heard)
        c._set_refresh_state(True, "vehicle info")
        c._note_refresh_step("vehicle status", 1)
        c._note_refresh_step("vehicle status", 1)  # no change, no call
        c._set_refresh_state(False)
        self.assertEqual(heard.call_count, 3)
        remove()
        c._set_refresh_state(True)
        self.assertEqual(heard.call_count, 3)

    def test_a_command_outside_a_poll_shows_nothing(self):
        c = _coordinator()
        c._note_refresh_step("vehicle status", 2)
        self.assertFalse(getattr(c, "refresh_in_progress", False))

    def test_every_try_is_noted(self):
        source = (test_setup_and_config_flow.PKG_DIR / "coordinator.py").read_text()
        loop = source.split("async def _fetch_with_retries", 1)[1][:1500]
        self.assertIn("self._note_refresh_step(data_name, retries + 1)", loop)


class SensorTests(unittest.TestCase):
    def _sensor(self, coordinator):
        cls = BINARY.SAICMGDataRefreshBinarySensor
        sensor = cls.__new__(cls)
        sensor.coordinator = coordinator
        return sensor

    def test_running(self):
        c = _coordinator()
        c._set_refresh_state(True, "vehicle status, try 3 of 5")
        sensor = self._sensor(c)
        self.assertTrue(sensor.is_on)
        self.assertTrue(sensor.available)
        attrs = sensor.extra_state_attributes
        self.assertEqual(attrs["step"], "vehicle status, try 3 of 5")
        self.assertIsNotNone(attrs["started"])

    def test_idle(self):
        c = _coordinator()
        c._set_refresh_state(True)
        c._set_refresh_state(False)
        sensor = self._sensor(c)
        self.assertFalse(sensor.is_on)
        self.assertIsNone(sensor.extra_state_attributes["started"])
        self.assertIsNotNone(sensor.extra_state_attributes["last_duration_s"])

    def test_diagnostic_and_named_to_sit_by_data_freshness(self):
        cls = BINARY.SAICMGDataRefreshBinarySensor
        self.assertEqual(cls._attr_entity_category, "diagnostic")
        sensor = cls.__new__(cls)
        BINARY.create_device_info = lambda *a: {}
        cls.__init__(sensor, NS(vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV")),
                     NS(entry_id="entry1"))
        self.assertEqual(sensor._attr_name, "MG MGS6 EV Data Refresh")
        self.assertEqual(sensor._attr_unique_id, "entry1_VIN1_data_refresh")


if __name__ == "__main__":
    unittest.main()
