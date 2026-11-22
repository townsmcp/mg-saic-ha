"""Tyre pressures kept through the Marvel R's filler and across restarts (#504).

@stfvrg, MG Marvel R (EP21), 22 Nov 2026: shortly after the car is switched
off, all four tyres read exactly 55 (2.20 bar). Switched on in the garage, not
driven, they read 71/72 (2.84/2.88 bar), matching the cluster's TPMS page. The
front left alternated between real values and exactly 2.2 dozens of times in
a week, always 2.2 with the car off, so Tyre Pressure Low went off every time.

Now 55 on all four tyres (on that model) is no reading: the last real value
is kept, with whether it was read with the car on or off (``reading``), and
the values are restored after a Home Assistant restart.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from test_erac_fallback import SENSOR
from test_data_refresh_sensor import BINARY
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

TYRES = sys.modules["mg_saic.tyres"]
FILLER = 55
FIELDS = ("frontLeftTyrePressure", "frontRightTyrePressure",
          "rearLeftTyrePressure", "rearRightTyrePressure")


def _status(raw, power_mode, status_time=1795300000):
    values = raw if isinstance(raw, dict) else dict(zip(FIELDS, raw))
    return NS(
        basicVehicleStatus=NS(powerMode=power_mode, **values),
        statusTime=status_time,
    )


ON_GARAGE = _status((71, 72, 71, 72), power_mode=2)   # car on, not driven
OFF_FILLER = _status((55, 55, 55, 55), power_mode=0)  # car off, filler
OFF_REAL = _status((71, 72, 71, 72), power_mode=0)    # a car that keeps real values


def _coordinator(status, filler=FILLER):
    return NS(
        data={"status": status},
        tyre_pressure_no_reading_raw=filler,
        vin_info=NS(vin="VIN1", brandName="MG", modelName="Marvel R"),
        last_update_success=True,
    )


def _tyre_sensor(coordinator, field="frontLeftTyrePressure"):
    entity = SENSOR.SAICMGTyrePressureSensor.__new__(SENSOR.SAICMGTyrePressureSensor)
    entity.coordinator = coordinator
    entity._field = field
    return entity


def _tyre_low(coordinator):
    entity = BINARY.SAICMGTyrePressureLowBinarySensor.__new__(
        BINARY.SAICMGTyrePressureLowBinarySensor
    )
    entity.coordinator = coordinator
    entity._entry = NS(entry_id="e", options={"tyre_low_threshold": "2.5 bar"})
    entity._last_pressures = None
    return entity


class FillerTests(unittest.TestCase):
    def test_55_on_all_four_is_no_reading(self):
        self.assertIsNone(TYRES.tyre_pressures_bar(OFF_FILLER.basicVehicleStatus, FILLER))

    def test_one_tyre_at_55_is_a_real_reading(self):
        basic = _status((55, 72, 71, 72), 0).basicVehicleStatus
        self.assertEqual(TYRES.tyre_pressures_bar(basic, FILLER)["Front Left"], 2.2)

    def test_other_models_take_55_at_face_value(self):
        self.assertEqual(
            TYRES.tyre_pressures_bar(OFF_FILLER.basicVehicleStatus, None)["Front Left"], 2.2
        )


class StfvrgsDayTests(unittest.TestCase):
    def test_kept_through_the_filler(self):
        c = _coordinator(ON_GARAGE)
        tyre, low = _tyre_sensor(c), _tyre_low(c)
        self.assertEqual(tyre.native_value, 2.84)
        self.assertFalse(low.is_on)
        c.data["status"] = OFF_FILLER
        self.assertEqual(tyre.native_value, 2.84)
        self.assertTrue(tyre.extra_state_attributes["held"])
        self.assertFalse(low.is_on)  # no false alarm at 2.5 bar

    def test_reading_says_car_on(self):
        attrs = _tyre_sensor(_coordinator(ON_GARAGE)).extra_state_attributes
        self.assertEqual(attrs["reading"], "car_on")
        self.assertFalse(attrs["held"])
        self.assertEqual(attrs["pressure_bar"], 2.84)
        self.assertTrue(attrs["reading_time"].startswith("2026-"))

    def test_reading_says_last_drive_when_off(self):
        # A car that reports its real last-drive values while off.
        attrs = _tyre_sensor(_coordinator(OFF_REAL, filler=None)).extra_state_attributes
        self.assertEqual(attrs["reading"], "last_drive")
        self.assertFalse(attrs["held"])

    def test_failed_poll_keeps_the_value(self):
        c = _coordinator(ON_GARAGE)
        tyre = _tyre_sensor(c)
        self.assertEqual(tyre.native_value, 2.84)
        c.data["status"] = None
        self.assertEqual(tyre.native_value, 2.84)
        self.assertTrue(tyre.extra_state_attributes["held"])

    def test_a_real_low_tyre_still_alarms(self):
        c = _coordinator(_status((50, 72, 71, 72), 2))
        self.assertTrue(_tyre_low(c).is_on)


class RestartTests(unittest.TestCase):
    def _restored(self, coordinator, attrs):
        entity = _tyre_sensor(coordinator)

        async def last_state():
            return NS(state="41.2", attributes=attrs)

        entity.async_get_last_state = last_state
        asyncio.run(entity.async_added_to_hass())
        return entity

    def test_restart_with_the_car_off(self):
        c = _coordinator(OFF_FILLER)
        tyre = self._restored(c, {
            "pressure_bar": 2.84, "reading": "car_on",
            "reading_time": "2026-11-22T08:30:00+00:00", "held": False,
        })
        self.assertEqual(tyre.native_value, 2.84)
        attrs = tyre.extra_state_attributes
        self.assertTrue(attrs["held"])
        self.assertEqual(attrs["reading"], "car_on")
        self.assertEqual(attrs["reading_time"], "2026-11-22T08:30:00+00:00")
        # Tyre Pressure Low works from the restored value too.
        self.assertIsNotNone(_tyre_low(c).is_on)

    def test_a_newer_reading_wins(self):
        c = _coordinator(_status((70, 72, 71, 72), 2))
        tyre = self._restored(c, {"pressure_bar": 2.5, "reading": "car_on"})
        self.assertEqual(tyre.native_value, 2.8)
        self.assertFalse(tyre.extra_state_attributes["held"])

    def test_nothing_to_restore(self):
        c = _coordinator(OFF_FILLER)
        tyre = self._restored(c, {})
        self.assertFalse(tyre.available)
        self.assertIsNone(tyre.native_value)

    def test_after_a_restart_the_next_real_reading_takes_over(self):
        c = _coordinator(OFF_FILLER)
        tyre = self._restored(c, {"pressure_bar": 2.84})
        c.data["status"] = _status((70, 72, 71, 72), 2, status_time=1795400000)
        self.assertEqual(tyre.native_value, 2.8)
        self.assertFalse(tyre.extra_state_attributes["held"])


class SetupTests(unittest.TestCase):
    def test_ep21_has_the_filler(self):
        const = sys.modules["mg_saic.const"]
        self.assertEqual(const.VEHICLE_PROFILES["EP21"]["tyre_pressure_no_reading_raw"], 55)

    def test_the_four_tyre_sensors_use_the_new_class(self):
        source = open(SENSOR.__file__).read()
        for name in ("Front Left", "Front Right", "Rear Left", "Rear Right"):
            self.assertIn(f'SAICMGTyrePressureSensor(\n                coordinator,\n                entry,\n                "Tyre Pressure {name}",', source)


class PushSensorNameTests(unittest.TestCase):
    def test_renamed_but_same_key(self):
        cls = SENSOR.SAICMGLastChargingFinishedPushSensor if hasattr(
            SENSOR, "SAICMGLastChargingFinishedPushSensor"
        ) else next(
            c for c in vars(SENSOR).values()
            if isinstance(c, type) and getattr(c, "_key", None) == "last_charging_finished_push"
        )
        self.assertEqual(cls._label, "Last Charging Stopped Push")
        self.assertEqual(cls._key, "last_charging_finished_push")


if __name__ == "__main__":
    unittest.main()
