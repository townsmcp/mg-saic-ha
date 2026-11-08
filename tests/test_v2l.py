"""V2L sensors and charging statuses (Has V2L).

James's MG S6 EV (MIS3E), iSmart app captured with mitmproxy, 8 Nov 2026,
SOC 78%, V2L adapter in the charge port:
- bmsChrgSts 11 with the adapter in, not discharging; 13 discharging (started
  on the car's screen); 14 after the discharge stopped (the port was
  unlocked in the app, which ended it).
- discharge limit 60 / 40 / 20 % -> imcuDschrgngEstdElecRng 206 / 134 / 60 km.
- a 2.5-3 kW kettle: onBdChrgrAltrCrntInptVol 110 (x2 = 220 V),
  onBdChrgrAltrCrntInptCrnt 58 (x0.2 = 11.6 A) -> 2.55 kW.
- ccuEleccLckCtrlDspCmd 1 locked, 2 unlocked, matching the app.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from test_erac_fallback import SENSOR
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

LOGIC = sys.modules["mg_saic.logic"]
PKG = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"
SENSOR_SOURCE = (PKG / "sensor.py").read_text()


def _chrg(**fields):
    base = dict(
        bmsChrgSts=13,
        disChrgngRmnngTime=95,
        disChrgngRmnngTimeV=0,
        imcuDschrgngEstdElecRng=206,
        imcuDschrgngEstdElecRngV=0,
        onBdChrgrAltrCrntInptVol=110,
        onBdChrgrAltrCrntInptCrnt=58,
    )
    base.update(fields)
    return NS(**base)


def _coordinator(chrg=None, has_v2l=True, factors=True):
    attrs = dict(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV"),
        data={"status": None, "charging": NS(chrgMgmtData=chrg) if chrg else None},
        has_v2l=has_v2l,
    )
    if factors:
        attrs.update(obc_input_voltage_factor=2.0, obc_input_current_factor=0.2)
    return NS(**attrs)


ENTRY = NS(entry_id="e")


class StatusTests(unittest.TestCase):
    def test_labels(self):
        self.assertIn('11: "V2L Adapter Connected"', SENSOR_SOURCE)
        self.assertIn('14: "V2X Discharge Stopped"', SENSOR_SOURCE)
        self.assertNotIn('11: "Super Offboard Charging"', SENSOR_SOURCE)

    def test_adapter_connected_is_not_dc_charging_with_v2l(self):
        self.assertFalse(LOGIC.is_dc_charging_status(11, True))

    def test_without_the_option_11_is_still_dc(self):
        self.assertTrue(LOGIC.is_dc_charging_status(11, False))

    def test_dc_charging(self):
        self.assertTrue(LOGIC.is_dc_charging_status(10, True))
        self.assertTrue(LOGIC.is_dc_charging_status(10, False))
        for status in (None, 0, 3, 13, 14):
            self.assertFalse(LOGIC.is_dc_charging_status(status, False))


class RemainingTimeTests(unittest.TestCase):
    def _sensor(self, chrg):
        c = _coordinator(chrg)
        return SENSOR.SAICMGV2LRemainingTimeSensor(c, ENTRY), c

    def test_identity(self):
        entity, _ = self._sensor(_chrg())
        self.assertEqual(entity.name, "MG MGS6 EV V2L Discharge Remaining Time")
        self.assertEqual(entity.unique_id, "e_VIN1_v2l_discharge_remaining_time")
        self.assertEqual(entity._attr_native_unit_of_measurement, "min")

    def test_while_discharging(self):
        self.assertEqual(self._sensor(_chrg())[0].native_value, 95)

    def test_not_discharging_is_zero(self):
        for status in (0, 11, 14):
            entity, _ = self._sensor(_chrg(bmsChrgSts=status, disChrgngRmnngTime=1023))
            self.assertEqual(entity.native_value, 0)

    def test_no_value_while_discharging_keeps_the_last(self):
        entity, c = self._sensor(_chrg())
        self.assertEqual(entity.native_value, 95)
        c.data["charging"] = NS(chrgMgmtData=_chrg(disChrgngRmnngTime=1023))
        self.assertEqual(entity.native_value, 95)
        c.data["charging"] = NS(chrgMgmtData=_chrg(disChrgngRmnngTimeV=1))
        self.assertEqual(entity.native_value, 95)

    def test_no_charging_data_keeps_the_last(self):
        entity, c = self._sensor(_chrg())
        self.assertEqual(entity.native_value, 95)
        c.data["charging"] = None
        self.assertEqual(entity.native_value, 95)


class RangeAtLimitTests(unittest.TestCase):
    def test_follows_the_limit(self):
        for km in (206, 134, 60):
            c = _coordinator(_chrg(imcuDschrgngEstdElecRng=km))
            self.assertEqual(SENSOR.SAICMGV2LRangeAtLimitSensor(c, ENTRY).native_value, km)

    def test_reported_when_not_discharging(self):
        c = _coordinator(_chrg(bmsChrgSts=0))
        self.assertEqual(SENSOR.SAICMGV2LRangeAtLimitSensor(c, ENTRY).native_value, 206)

    def test_flagged_value_keeps_the_last(self):
        c = _coordinator(_chrg())
        entity = SENSOR.SAICMGV2LRangeAtLimitSensor(c, ENTRY)
        self.assertEqual(entity.native_value, 206)
        c.data["charging"] = NS(chrgMgmtData=_chrg(imcuDschrgngEstdElecRngV=1))
        self.assertEqual(entity.native_value, 206)

    def test_identity(self):
        entity = SENSOR.SAICMGV2LRangeAtLimitSensor(_coordinator(_chrg()), ENTRY)
        self.assertEqual(entity.name, "MG MGS6 EV V2L Range At Discharge Limit")
        self.assertEqual(entity.unique_id, "e_VIN1_v2l_range_at_discharge_limit")


class OutputTests(unittest.TestCase):
    def _value(self, kind, chrg):
        return SENSOR.SAICMGV2LOutputSensor(_coordinator(chrg), ENTRY, kind).native_value

    def test_the_kettle(self):
        self.assertEqual(self._value("voltage", _chrg()), 220.0)
        self.assertEqual(self._value("current", _chrg()), 11.6)
        self.assertEqual(self._value("power", _chrg()), 2.55)

    def test_zero_when_not_discharging(self):
        # The same fields are the charger's input while AC charging.
        for kind in ("voltage", "current", "power"):
            self.assertEqual(self._value(kind, _chrg(bmsChrgSts=1)), 0)

    def test_identity(self):
        for kind, label in (("voltage", "Voltage"), ("current", "Current"), ("power", "Power")):
            entity = SENSOR.SAICMGV2LOutputSensor(_coordinator(_chrg()), ENTRY, kind)
            self.assertEqual(entity.name, f"MG MGS6 EV V2L Output {label}")
            self.assertEqual(entity.unique_id, f"e_VIN1_v2l_output_{kind}")

    def test_charger_input_unchanged(self):
        entity = SENSOR.SAICMGChargerInputSensor(_coordinator(_chrg()), ENTRY, "power")
        self.assertEqual(entity.unique_id, "e_VIN1_charger_input_power")
        self.assertEqual(entity.name, "MG MGS6 EV Charger Input Power")


class GatingTests(unittest.TestCase):
    def _names(self, **kwargs):
        return [e._name for e in SENSOR.v2l_sensors(_coordinator(_chrg(), **kwargs), ENTRY)]

    def test_option_off(self):
        self.assertEqual(self._names(has_v2l=False), [])

    def test_option_on(self):
        self.assertEqual(
            self._names(),
            [
                "V2L Discharge Remaining Time",
                "V2L Range At Discharge Limit",
                "V2L Output Voltage",
                "V2L Output Current",
                "V2L Output Power",
            ],
        )

    def test_no_output_sensors_without_the_scales(self):
        self.assertEqual(
            self._names(factors=False),
            ["V2L Discharge Remaining Time", "V2L Range At Discharge Limit"],
        )

    def test_registered_with_the_charging_sensors(self):
        self.assertIn("sensors.extend(v2l_sensors(coordinator, entry))", SENSOR_SOURCE)

    def test_option_change_reloads_entities(self):
        self.assertIn('"has_v2l"', (PKG / "__init__.py").read_text())

    def test_option_in_both_flows_and_translations(self):
        flow = (PKG / "config_flow.py").read_text()
        self.assertIn('vol.Required("has_v2l"', flow)
        self.assertIn('"has_v2l",\n                    default=self.options.get(', flow)
        import json

        for lang in ("en", "es", "fr", "pt"):
            text = json.dumps(json.loads((PKG / "translations" / f"{lang}.json").read_text()))
            self.assertEqual(text.count('"has_v2l"'), 4, lang)


class ChargingPortLockTests(unittest.TestCase):
    def test_one_is_locked(self):
        source = (PKG / "switch.py").read_text()
        self.assertIn("return lock_status == 1\n", source)
        self.assertNotIn("Assuming 1 represents locked", source)


if __name__ == "__main__":
    unittest.main()
