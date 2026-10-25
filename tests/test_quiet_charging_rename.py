"""Quiet hours became Quiet Charging in 1.4.0-beta5 (#269).

@HarryFlatter and James both read "Quiet Hours Live Polling" backwards (on
meant normal polling). Harry suggested "Quiet Charging", with on meaning
quiet, and "Quiet Charging Start" / "Quiet Charging End" for the times.
"""

import asyncio
import sys
import unittest
from datetime import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_charging_controls_dropout import MODS  # loads the platforms

COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
VIN_INFO = NS(brandName="MG", modelName="MGS6 EV")
ENTRY = NS(entry_id="entry1")


def _coordinator(live_polling):
    return NS(
        live_polling=live_polling,
        quiet_hours_start=time(22, 0),
        quiet_hours_end=time(7, 0),
        quiet_finish_check_at=None,
        quiet_last_stored=None,
        async_set_live_polling=AsyncMock(),
    )


def _switch(coordinator):
    cls = MODS["switch"].SAICMGQuietChargingSwitch
    switch = cls.__new__(cls)
    switch.coordinator = coordinator
    switch.async_write_ha_state = MagicMock()
    return switch


class SwitchTests(unittest.TestCase):
    def test_name_and_id(self):
        cls = MODS["switch"].SAICMGQuietChargingSwitch
        switch = cls.__new__(cls)
        MODS["switch"].create_device_info = lambda *a: {}
        cls.__init__(switch, _coordinator(True), ENTRY, VIN_INFO, "VIN1")
        self.assertEqual(switch._attr_name, "MG MGS6 EV Quiet Charging")
        self.assertEqual(switch._attr_unique_id, "entry1_VIN1_quiet_charging")

    def test_on_means_quiet(self):
        self.assertTrue(_switch(_coordinator(live_polling=False)).is_on)
        self.assertFalse(_switch(_coordinator(live_polling=True)).is_on)

    def test_turning_it_on_stops_live_polling(self):
        c = _coordinator(True)
        asyncio.run(_switch(c).async_turn_on())
        c.async_set_live_polling.assert_awaited_once_with(False, reason="switched on")

    def test_turning_it_off_brings_live_polling_back(self):
        c = _coordinator(False)
        asyncio.run(_switch(c).async_turn_off())
        c.async_set_live_polling.assert_awaited_once_with(True, reason="switched off")

    def test_attributes(self):
        attrs = _switch(_coordinator(False)).extra_state_attributes
        self.assertEqual(attrs["quiet_charging_start"], "22:00")
        self.assertEqual(attrs["quiet_charging_end"], "07:00")


class TimeTests(unittest.TestCase):
    def test_names_and_ids(self):
        cls = MODS["time"].SAICMGQuietHoursTime
        MODS["time"].create_device_info = lambda *a: {}
        for which, label in (("start", "Start"), ("end", "End")):
            entity = cls.__new__(cls)
            cls.__init__(entity, _coordinator(True), ENTRY, VIN_INFO, "VIN1", which)
            self.assertEqual(entity._attr_name, f"MG MGS6 EV Quiet Charging {label}")
            self.assertEqual(entity._attr_unique_id, f"entry1_VIN1_quiet_charging_{which}")


class OldEntitiesRemovedTests(unittest.TestCase):
    def _run(self, existing):
        registry = MagicMock()
        registry.async_get_entity_id = lambda platform, domain, uid: existing.get((platform, uid))
        er = NS(async_get=lambda hass: registry)
        helpers = sys.modules["homeassistant.helpers"]
        previous = getattr(helpers, "entity_registry", None)
        helpers.entity_registry = er
        sys.modules["homeassistant.helpers.entity_registry"] = er
        try:
            c = COORD_CLS.__new__(COORD_CLS)
            c.hass = MagicMock()
            c.vin = "VIN1"
            c.config_entry = NS(entry_id="entry1")
            c._remove_renamed_quiet_entities()
        finally:
            if previous is not None:
                helpers.entity_registry = previous
            sys.modules.pop("homeassistant.helpers.entity_registry", None)
        return registry

    def test_the_beta4_entities_go(self):
        existing = {
            ("switch", "entry1_VIN1_quiet_hours_live_polling"): "switch.mg_quiet_hours_live_polling",
            ("time", "entry1_VIN1_quiet_hours_start"): "time.mg_quiet_hours_start",
            ("time", "entry1_VIN1_quiet_hours_end"): "time.mg_quiet_hours_end",
        }
        registry = self._run(existing)
        removed = {call.args[0] for call in registry.async_remove.call_args_list}
        self.assertEqual(removed, set(existing.values()))

    def test_nothing_to_remove(self):
        registry = self._run({})
        registry.async_remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
