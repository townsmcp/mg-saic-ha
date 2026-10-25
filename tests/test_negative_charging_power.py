"""Negative Charging Power / Current while the status says charging (#442).

@bendruitt's MG4 (Australia) discharging through a two-way DC charger in the
evenings: Charging Status read 10 "Charging (DC)" the whole time, with the
current about -15 A (lowest -18 A) and the power -5 kW (lowest -6.1 kW), the
battery falling 7-11% an hour. Since 1.1.0, negatives under a charging status
are shown as 0 (small negatives around the start and end of a charge), so
both sensors read 0.

The "Negative charging values switch" option adds a Negative Charging Values
switch to the device; while it is on, the two sensors show negatives as
reported.
"""

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR
from test_charging_controls_dropout import MODS  # loads the platforms

PKG = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"
CONST = sys.modules["mg_saic.const"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
ENTRY = NS(entry_id="entry1")

# -15 A at 335 V: raw current 20300 (1000 - 20300 x 0.05), raw voltage 1340.
DISCHARGE = dict(crnt=20300, vol=1340)
DISCHARGE_KW = round(-15.0 * 335.0 / 1000.0, 2)
# The small negative the clamp was added for (#262): -1.9 A at raw 20038.
NOISE = dict(crnt=20038, vol=1600)


def _coordinator(*, allow=None):
    c = NS(
        data={"charging": None, "status": None},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MG4"),
        last_update_success=True,
        charging_capacity_correction=None,
    )
    if allow is not None:
        c.allow_negative_charging_power = allow
    return c


def _poll(c, *, sts, crnt, vol):
    c.data["charging"] = NS(
        chrgMgmtData=NS(bmsChrgSts=sts, bmsPackCrnt=crnt, bmsPackVol=vol)
    )


def _read(c):
    power = SENSOR.SAICMGChargingPowerSensor(
        c, ENTRY, "Charging Power", None, None, None, None
    )
    current = SENSOR.SAICMGChargingCurrentSensor(
        c, ENTRY, "Charging Current", "bmsPackCrnt", None, None, None, None, 0.05
    )
    return power.native_value, current.native_value


class BensEveningTests(unittest.TestCase):
    def test_option_off_reads_zero_as_before(self):
        c = _coordinator(allow=False)
        _poll(c, sts=10, **DISCHARGE)
        self.assertEqual(_read(c), (0.0, 0.0))

    def test_option_missing_reads_zero(self):
        # An entry saved before the option existed.
        c = _coordinator()
        _poll(c, sts=10, **DISCHARGE)
        self.assertEqual(_read(c), (0.0, 0.0))

    def test_option_on_shows_the_discharge(self):
        c = _coordinator(allow=True)
        _poll(c, sts=10, **DISCHARGE)
        self.assertEqual(_read(c), (DISCHARGE_KW, -15.0))


class OptionOnTests(unittest.TestCase):
    def test_small_negatives_are_shown_too(self):
        c = _coordinator(allow=True)
        _poll(c, sts=12, **NOISE)
        power, current = _read(c)
        self.assertEqual(current, -1.9)
        self.assertLess(power, 0)

    def test_every_charging_status(self):
        for sts in (1, 2, 3, 9, 10, 12):
            c = _coordinator(allow=True)
            _poll(c, sts=sts, **DISCHARGE)
            self.assertEqual(_read(c)[1], -15.0, sts)

    def test_charging_is_unchanged(self):
        # +15 A into the pack: raw 19700.
        for allow in (False, True):
            c = _coordinator(allow=allow)
            _poll(c, sts=10, crnt=19700, vol=1340)
            self.assertEqual(_read(c), (round(15.0 * 335.0 / 1000.0, 2), 15.0), allow)


class V2xDischargingTests(unittest.TestCase):
    def test_status_13_always_shows_negatives(self):
        for allow in (False, True, None):
            c = _coordinator(allow=allow)
            _poll(c, sts=13, **DISCHARGE)
            self.assertEqual(_read(c), (DISCHARGE_KW, -15.0), allow)


def _real_coordinator(*, switch, values):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "VIN1"
    c.negative_charging_switch = switch
    c.negative_charging_values = values
    return c


class SwitchAndOptionTests(unittest.TestCase):
    def test_needs_both_the_option_and_the_switch_on(self):
        for switch, values, expected in (
            (False, False, False),
            (False, True, False),  # option unticked: the switch is gone
            (True, False, False),
            (True, True, True),
        ):
            c = _real_coordinator(switch=switch, values=values)
            self.assertIs(c.allow_negative_charging_power, expected, (switch, values))

    def test_turning_the_switch_on_is_kept_in_the_options(self):
        c = _real_coordinator(switch=True, values=False)
        c.config_entry = NS(options={"negative_charging_switch": True, "holiday_mode": True})
        c.hass = MagicMock()
        c.async_update_listeners = MagicMock()
        import asyncio

        asyncio.run(c.async_set_negative_charging_values(True))
        options = c.hass.config_entries.async_update_entry.call_args.kwargs["options"]
        self.assertEqual(
            options,
            {"negative_charging_switch": True, "holiday_mode": True, "negative_charging_values": True},
        )
        self.assertTrue(c.allow_negative_charging_power)
        c.async_update_listeners.assert_called_once()

    def _switch(self, c):
        cls = MODS["switch"].SAICMGNegativeChargingValuesSwitch
        sw = cls.__new__(cls)
        sw.coordinator = c
        sw.async_write_ha_state = MagicMock()
        return sw

    def test_the_switch_follows_the_setting(self):
        for values in (False, True):
            sw = self._switch(_real_coordinator(switch=True, values=values))
            self.assertIs(sw.is_on, values)
            self.assertTrue(sw.available)

    def test_the_switch_turns_it_on_and_off(self):
        import asyncio
        from unittest.mock import AsyncMock

        c = _real_coordinator(switch=True, values=False)
        c.async_set_negative_charging_values = AsyncMock()
        sw = self._switch(c)
        asyncio.run(sw.async_turn_on())
        asyncio.run(sw.async_turn_off())
        self.assertEqual(
            [call.args for call in c.async_set_negative_charging_values.await_args_list],
            [(True,), (False,)],
        )

    def test_name_and_id(self):
        source = (PKG / "switch.py").read_text()
        self.assertIn('{vin_info.modelName} Negative Charging Values"', source)
        self.assertIn('_attr_unique_id = f"{entry.entry_id}_{vin}_negative_charging_values"', source)


class WiringTests(unittest.TestCase):
    def test_option_keys(self):
        self.assertEqual(CONST.CONF_NEGATIVE_CHARGING_SWITCH, "negative_charging_switch")
        self.assertEqual(CONST.CONF_NEGATIVE_CHARGING_VALUES, "negative_charging_values")

    def test_on_the_options_form_off_by_default(self):
        source = (PKG / "config_flow.py").read_text()
        self.assertIn(
            "CONF_NEGATIVE_CHARGING_SWITCH,\n"
            "                    default=self.options.get(CONF_NEGATIVE_CHARGING_SWITCH, False),",
            source,
        )

    def test_saving_the_form_keeps_the_switch_state(self):
        source = (PKG / "config_flow.py").read_text()
        keys = source.split("RUNTIME_OPTION_KEYS = (", 1)[1].split(")", 1)[0]
        self.assertIn("CONF_NEGATIVE_CHARGING_VALUES", keys)

    def test_ticking_the_option_adds_the_switch(self):
        source = (PKG / "__init__.py").read_text()
        caps = source.split("ENTITY_CAPABILITY_OPTIONS = (", 1)[1].split("\n)\n", 1)[0]
        self.assertIn('"negative_charging_switch"', caps)
        switch_source = (PKG / "switch.py").read_text()
        self.assertIn(
            'if getattr(coordinator, "negative_charging_switch", False):\n'
            "        switches.append(\n"
            "            SAICMGNegativeChargingValuesSwitch(",
            switch_source,
        )

    def test_coordinator_reads_both_at_setup_and_on_save(self):
        source = (PKG / "coordinator.py").read_text()
        for key in ("CONF_NEGATIVE_CHARGING_SWITCH", "CONF_NEGATIVE_CHARGING_VALUES"):
            self.assertIn(f"config_entry.options.get({key}, False)", source)
            self.assertIn(f"            options.get({key}, False)\n", source)

    def test_every_language_has_a_label_and_description(self):
        for path in sorted((PKG / "translations").glob("*.json")):
            init = json.loads(path.read_text())["options"]["step"]["init"]
            self.assertIn("negative_charging_switch", init["data"], path.name)
            self.assertIn("negative_charging_switch", init["data_description"], path.name)


if __name__ == "__main__":
    unittest.main()
