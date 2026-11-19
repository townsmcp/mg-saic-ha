"""Tyre Pressure Low (#497, @SteveMSJ).

MGS5 EV (MZS3E), 18 Nov 2026: a slow puncture took the rear right to 0.88 bar
(raw 22; iSmart showed 0.9 bar, "Low Pressure", "Abnormal") while the car's
own wheelTyreMonitorStatus stayed 0, so Wheel Tyre Monitor Status said OK.
James's MGS6 had a nail take a rear tyre to 33.1 psi (normally 37-39) with no
warning from the car or iSmart; a 34 psi automation caught it.

Tyre Pressure Low works it out from the four pressures, against a threshold
set under Configure (default 34 psi).
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from test_data_refresh_sensor import BINARY

CF = sys.modules["mg_saic.config_flow"]
CONST = sys.modules["mg_saic.const"]
OPTION = CONST.CONF_TYRE_LOW_THRESHOLD

# Steve's log, raw values (x0.04 bar).
STEVE = dict(frontLeftTyrePressure=60, frontRightTyrePressure=63,
             rearLeftTyrePressure=61, rearRightTyrePressure=22)
# James's MGS6 this morning, all fine: 2.56 / 2.56 / 2.64 / 2.60 bar.
JAMES_OK = dict(frontLeftTyrePressure=64, frontRightTyrePressure=64,
                rearLeftTyrePressure=66, rearRightTyrePressure=65)
# The nail: 33.1 psi = 2.28 bar = raw 57.
JAMES_NAIL = dict(JAMES_OK, rearLeftTyrePressure=57)


def _status(tyres, alerts=None):
    status = NS(basicVehicleStatus=NS(**tyres))
    if alerts is not None:
        status.extendedVehicleStatus = NS(alertDataSum=alerts)
    return status


def _alerts(**slots):
    alerts = [0] * 64
    for slot, value in slots.items():
        alerts[int(slot[1:])] = value
    return alerts


def _entity(tyres, options=None, alerts=None):
    coordinator = NS(
        data={"status": _status(tyres, alerts) if tyres is not None else None},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS5 EV"),
    )
    entry = NS(entry_id="e", options=dict(options or {}))
    entity = BINARY.SAICMGTyrePressureLowBinarySensor.__new__(
        BINARY.SAICMGTyrePressureLowBinarySensor
    )
    entity.coordinator = coordinator
    entity._entry = entry
    entity._last_pressures = None
    return entity


class StevesPunctureTests(unittest.TestCase):
    def test_on_for_the_rear_right(self):
        entity = _entity(STEVE)
        self.assertTrue(entity.is_on)
        self.assertEqual(
            entity.extra_state_attributes["low_tyres"],
            [{"tyre": "Rear Right", "bar": 0.88, "psi": 12.8}],
        )

    def test_default_threshold_is_34_psi(self):
        attrs = _entity(STEVE).extra_state_attributes
        self.assertEqual(attrs["threshold"], "34 psi")
        self.assertEqual(attrs["threshold_psi"], 34.0)
        self.assertEqual(attrs["threshold_bar"], 2.34)


class JamesNailTests(unittest.TestCase):
    def test_nail_caught_at_the_default(self):
        entity = _entity(JAMES_NAIL)
        self.assertTrue(entity.is_on)
        self.assertEqual(entity.extra_state_attributes["low_tyres"][0]["tyre"], "Rear Left")

    def test_all_fine(self):
        entity = _entity(JAMES_OK)
        self.assertFalse(entity.is_on)
        self.assertEqual(entity.extra_state_attributes["low_tyres"], [])


class ThresholdTests(unittest.TestCase):
    def test_owner_threshold_applies_straight_away(self):
        entity = _entity(JAMES_NAIL)
        self.assertTrue(entity.is_on)
        entity._entry.options[OPTION] = "2.2 bar"  # an options save, no reload
        self.assertFalse(entity.is_on)
        self.assertEqual(entity.extra_state_attributes["threshold"], "2.2 bar")

    def test_bad_stored_value_falls_back_to_the_default(self):
        self.assertEqual(
            _entity(STEVE, {OPTION: "nonsense"}).extra_state_attributes["threshold"], "34 psi"
        )

    def test_several_low_tyres_lowest_first(self):
        tyres = dict(STEVE, frontLeftTyrePressure=50)  # 2.0 bar
        low = _entity(tyres).extra_state_attributes["low_tyres"]
        self.assertEqual([t["tyre"] for t in low], ["Rear Right", "Front Left"])


class ReadingTests(unittest.TestCase):
    def test_minus_128_is_no_reading(self):
        tyres = dict(JAMES_OK, rearRightTyrePressure=-128)
        entity = _entity(tyres)
        self.assertFalse(entity.is_on)

    def test_a_flat_tyre_reading_0_counts(self):
        entity = _entity(dict(JAMES_OK, rearRightTyrePressure=0))
        self.assertTrue(entity.is_on)

    def test_all_zero_is_no_reading(self):
        zeros = {k: 0 for k in JAMES_OK}
        self.assertFalse(_entity(zeros).available)

    def test_poll_without_status_keeps_the_last_state(self):
        entity = _entity(STEVE)
        self.assertTrue(entity.is_on)
        entity.coordinator.data["status"] = None
        self.assertTrue(entity.available)
        self.assertTrue(entity.is_on)

    def test_unavailable_before_any_reading(self):
        entity = _entity(None)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.is_on)


class CarAlertTests(unittest.TestCase):
    """alertDataSum: shown, not yet used for the state."""

    def test_steves_puncture_shows_slot_4(self):
        attrs = _entity(STEVE, alerts=_alerts(s4=2)).extra_state_attributes
        self.assertEqual(attrs["car_tyre_alert"], 2)
        self.assertEqual(attrs["car_alert_slots"], {4: 2})

    def test_repaired_reads_0(self):
        attrs = _entity(JAMES_OK, alerts=_alerts()).extra_state_attributes
        self.assertEqual(attrs["car_tyre_alert"], 0)
        self.assertEqual(attrs["car_alert_slots"], {})

    def test_other_slots_are_listed(self):
        attrs = _entity(JAMES_OK, alerts=_alerts(s4=1, s9=3)).extra_state_attributes
        self.assertEqual(attrs["car_alert_slots"], {4: 1, 9: 3})

    def test_does_not_change_the_state(self):
        self.assertFalse(_entity(JAMES_OK, alerts=_alerts(s4=2)).is_on)

    def test_not_sent_is_none(self):
        attrs = _entity(JAMES_OK).extra_state_attributes
        self.assertIsNone(attrs["car_tyre_alert"])
        self.assertIsNone(attrs["car_alert_slots"])

    def test_held_through_a_poll_without_status(self):
        entity = _entity(STEVE, alerts=_alerts(s4=2))
        entity.extra_state_attributes
        entity.coordinator.data["status"] = None
        self.assertEqual(entity.extra_state_attributes["car_tyre_alert"], 2)


class ParseTests(unittest.TestCase):
    def test_units(self):
        cases = {
            "": (2.344, "34 psi"),
            None: (2.344, "34 psi"),
            "34": (2.344, "34 psi"),
            "34 psi": (2.344, "34 psi"),
            "34PSI": (2.344, "34 psi"),
            "2.3": (2.3, "2.3 bar"),
            "2,3 bar": (2.3, "2.3 bar"),
            "235": (2.35, "235 kPa"),
            "235 kpa": (2.35, "235 kPa"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(CONST.parse_tyre_threshold(raw), expected)

    def test_rejects(self):
        for raw in ("abc", "0", "-30", "80 psi", "0.5 bar", "900 kPa", "8"):
            with self.subTest(raw=raw):
                self.assertIsNone(CONST.parse_tyre_threshold(raw))


class OptionFlowTests(unittest.TestCase):
    def _submit(self, value):
        flow = CF.SAICMGOptionsFlowHandler.__new__(CF.SAICMGOptionsFlowHandler)
        flow.config_entry = NS(options={}, data={})
        flow._validate_abrp = AsyncMock(return_value={})
        flow.async_create_entry = lambda title, data: {"data": data}
        flow.async_show_form = lambda **kw: kw
        return asyncio.run(flow.async_step_init({OPTION: value}))

    def test_saved_as_text_with_its_unit(self):
        self.assertEqual(self._submit("32")["data"][OPTION], "32 psi")
        self.assertEqual(self._submit("2.2 bar")["data"][OPTION], "2.2 bar")

    def test_blank_is_the_default(self):
        self.assertEqual(self._submit("")["data"][OPTION], "34 psi")

    def test_bad_value(self):
        self.assertEqual(self._submit("lots")["errors"], {OPTION: "tyre_threshold_invalid"})

    def test_text_in_every_language(self):
        folder = Path(CF.__file__).parent / "translations"
        for path in sorted(folder.glob("*.json")):
            options = json.loads(path.read_text())["options"]
            init = options["step"]["init"]
            self.assertIn(OPTION, init["data"], path.name)
            self.assertIn(OPTION, init["data_description"], path.name)
            self.assertIn("tyre_threshold_invalid", options["error"], path.name)


class RegisteredTests(unittest.TestCase):
    def test_added_for_every_car(self):
        source = Path(BINARY.__file__).read_text()
        self.assertIn(
            "binary_sensors.append(SAICMGTyrePressureLowBinarySensor(coordinator, entry))",
            source,
        )


if __name__ == "__main__":
    unittest.main()
