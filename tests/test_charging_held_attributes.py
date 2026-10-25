"""Charging Power / Current say when they are showing a held value (#442).

@bendruitt manages household loads from Charging Power during a discharge.
Charging Data Freshness says whether the charging reply as a whole came
back, but if the car marks a single figure as "no value" (-128) in an
otherwise good reply, the sensor keeps showing the last good figure and
nothing said so. Both sensors now carry:

    held           true when the value shown is a kept one, not this poll's
    reading_time   when the value shown was read from the car
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR

LOGIC = sys.modules["mg_saic.logic"]
ENTRY = NS(entry_id="entry1")
T0 = datetime(2026, 10, 18, 5, 3, 28, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=5)

# -16.25 A at 346.4 V (Ben's 16:03 reading): raw 20325, raw voltage 1386.
DISCHARGE = dict(bmsChrgSts=10, bmsPackCrnt=20325, bmsPackVol=1386)


def _coordinator():
    return NS(
        data={"charging": None, "status": None},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MG4"),
        last_update_success=True,
        charging_capacity_correction=None,
        allow_negative_charging_power=True,
        charging_freshness=LOGIC.ChargingFreshnessTracker(),
    )


def _sensors(c):
    return (
        SENSOR.SAICMGChargingPowerSensor(c, ENTRY, "Charging Power", None, None, None, None),
        SENSOR.SAICMGChargingCurrentSensor(
            c, ENTRY, "Charging Current", "bmsPackCrnt", None, None, None, None, 0.05
        ),
    )


def _reply(c, at, **fields):
    c.data["charging"] = NS(chrgMgmtData=NS(**fields))
    c.charging_freshness.record_success(at)


def _no_reply(c, at):
    c.data["charging"] = None
    c.charging_freshness.record_failure(at, "return code: 4")


def _read(sensors):
    out = []
    for s in sensors:
        value = s.native_value
        out.append((value, s.extra_state_attributes))
    return out


class HeldTests(unittest.TestCase):
    def test_a_good_reading(self):
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, **DISCHARGE)
        (power, p_attrs), (current, c_attrs) = _read(sensors)
        self.assertEqual(current, -16.25)
        self.assertLess(power, 0)
        for attrs in (p_attrs, c_attrs):
            self.assertEqual(attrs, {"held": False, "reading_time": T0.isoformat()})

    def test_no_value_in_a_good_reply(self):
        # The reply came back (freshness live) but the current is -128.
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, **DISCHARGE)
        _read(sensors)
        _reply(c, T1, **{**DISCHARGE, "bmsPackCrnt": -128})
        (power, p_attrs), (current, c_attrs) = _read(sensors)
        self.assertEqual(current, -16.25)
        self.assertEqual(c.charging_freshness.state, "live")
        for attrs in (p_attrs, c_attrs):
            self.assertEqual(attrs, {"held": True, "reading_time": T0.isoformat()})

    def test_no_value_in_the_voltage_holds_the_power_only(self):
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, **DISCHARGE)
        _read(sensors)
        _reply(c, T1, **{**DISCHARGE, "bmsPackVol": -128})
        (_, p_attrs), (_, c_attrs) = _read(sensors)
        self.assertTrue(p_attrs["held"])
        self.assertEqual(c_attrs, {"held": False, "reading_time": T1.isoformat()})

    def test_the_charging_reply_did_not_come_back(self):
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, **DISCHARGE)
        _read(sensors)
        _no_reply(c, T1)
        (_, p_attrs), (current, c_attrs) = _read(sensors)
        self.assertEqual(current, -16.25)
        for attrs in (p_attrs, c_attrs):
            self.assertEqual(attrs, {"held": True, "reading_time": T0.isoformat()})

    def test_back_again(self):
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, **DISCHARGE)
        _read(sensors)
        _no_reply(c, T1)
        _read(sensors)
        later = T1 + timedelta(minutes=5)
        _reply(c, later, **DISCHARGE)
        for _, attrs in _read(sensors):
            self.assertEqual(attrs, {"held": False, "reading_time": later.isoformat()})

    def test_a_kept_reply_is_not_a_new_reading(self):
        # The saved reply shown at startup while the car doesn't answer: data
        # is there, but no charging fetch has succeeded.
        c = _coordinator()
        sensors = _sensors(c)
        c.data["charging"] = NS(chrgMgmtData=NS(**DISCHARGE))
        c.charging_freshness.record_failure(T0, "timed out")
        for value, attrs in _read(sensors):
            self.assertIsNotNone(value)
            self.assertEqual(attrs, {"held": True, "reading_time": None})


class NotHeldTests(unittest.TestCase):
    def test_unplugged_reads_zero_from_the_reply(self):
        c = _coordinator()
        sensors = _sensors(c)
        _reply(c, T0, bmsChrgSts=0, bmsPackCrnt=20000, bmsPackVol=1386)
        for value, attrs in _read(sensors):
            self.assertEqual(value, 0)
            self.assertEqual(attrs, {"held": False, "reading_time": T0.isoformat()})

    def test_zero_from_the_status_while_the_reply_is_missing(self):
        c = _coordinator()
        c.charging_stopped_per_status = True
        sensors = _sensors(c)
        _no_reply(c, T1)
        for value, attrs in _read(sensors):
            self.assertEqual(value, 0)
            self.assertEqual(attrs, {"held": False, "reading_time": T1.isoformat()})

    def test_nothing_yet(self):
        c = _coordinator()
        sensors = _sensors(c)
        for value, attrs in _read(sensors):
            self.assertIsNone(value)
            self.assertIsNone(attrs)


if __name__ == "__main__":
    unittest.main()
