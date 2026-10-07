"""The 12 V battery voltage on a model that reports it about 3 V low (#407).

@hoffeck fitted a Bluetooth monitor to the 12 V battery of an MG4 EV Urban
(series AH4EM L) and paired its readings with the integration's Ancillary
Battery Voltage sensor, keeping only moments when the monitor was steady:

    reported   monitor    situation
       9.5      12.61     parked, evening
       9.7      12.79     parked, after power-off
      10.7      13.54     driving
      10.6      13.57     driving
      10.5      13.48     driving
      10.8      13.63     driving
      10.7      13.57     driving
      10.6      13.47     AC charging
      10.5      13.39     driving
      10.4      13.38     DC charging
       9.5      12.68  }
       9.4      12.57  }  from his first six
       9.6      12.74  }
      11.8      14.61  }

A straight line, reported x 0.82 + 4.85, is within 0.09 V of every one. Two
readings he set aside do not fit it and are not meant to: the car's figure had
not caught up with the battery (first poll of a trip; "Connecting" on DC).
"""

import sys
import unittest
from types import SimpleNamespace as NS

from test_erac_fallback import LOGIC, SENSOR
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

CONST = sys.modules["mg_saic.const"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator

PAIRS = [
    (9.5, 12.61), (9.7, 12.79), (10.7, 13.54), (10.6, 13.57), (10.5, 13.48),
    (10.8, 13.63), (10.7, 13.57), (10.6, 13.47), (10.5, 13.39), (10.4, 13.38),
    (9.5, 12.68), (9.4, 12.57), (9.6, 12.74), (11.8, 14.61),
]
URBAN = CONST.VEHICLE_PROFILES["AH4EM"]["aux_battery_voltage_correction"]


def _profiled(series):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vehicle_series = series
    c.battery_capacity_override = None
    COORD_CLS._apply_vehicle_profile(c)
    return c


def _voltage_sensor(raw, correction):
    coordinator = NS(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MG4 EV URBAN"),
        data={"status": NS(basicVehicleStatus=NS(batteryVoltage=raw))},
        last_update_success=True,
        aux_battery_voltage_correction=correction,
    )
    sensor = SENSOR.SAICMGVehicleSensor.__new__(SENSOR.SAICMGVehicleSensor)
    sensor.coordinator = coordinator
    sensor._name = "Ancillary Battery Voltage"
    sensor._field = "batteryVoltage"
    sensor._status_type = "basicVehicleStatus"
    sensor._factor = 0.1
    sensor._data_type = "status"
    sensor._last_valid_value = None
    sensor._last_valid_mapped = None
    sensor._last_valid_temperature = {}
    sensor._last_valid_temperature_ts = {}
    sensor._temp_spike_skipped = {}
    return sensor


class CorrectionTests(unittest.TestCase):
    def test_it_matches_the_monitor_on_every_pair(self):
        for reported, monitor in PAIRS:
            corrected = LOGIC.corrected_aux_voltage(reported, URBAN)
            self.assertLess(abs(corrected - monitor), 0.09, (reported, monitor))

    def test_the_uncorrected_figure_was_about_three_volts_low(self):
        for reported, monitor in PAIRS:
            self.assertGreater(monitor - reported, 2.8)

    def test_a_fixed_three_volts_would_not_do(self):
        worst = max(abs(reported + 3.0 - monitor) for reported, monitor in PAIRS)
        self.assertGreater(worst, 0.18)

    def test_no_correction_leaves_the_figure_alone(self):
        self.assertEqual(LOGIC.corrected_aux_voltage(12.6, None), 12.6)
        self.assertEqual(LOGIC.corrected_aux_voltage(12.6, ()), 12.6)
        self.assertEqual(LOGIC.corrected_aux_voltage(12.6, "nonsense"), 12.6)

    def test_something_that_is_not_a_voltage_is_passed_through(self):
        self.assertIsNone(LOGIC.corrected_aux_voltage(None, URBAN))
        self.assertIs(LOGIC.corrected_aux_voltage(True, URBAN), True)


class ProfileTests(unittest.TestCase):
    def test_the_urban_has_it(self):
        self.assertEqual(URBAN, (0.82, 4.85))
        self.assertEqual(_profiled("AH4EM L").aux_battery_voltage_correction, URBAN)

    def test_no_other_model_has_one(self):
        # Checked against a monitor on an MGS6: x0.1 is right there. Nobody
        # has measured the rest, so they are left as the car reports them.
        for series, profile in CONST.VEHICLE_PROFILES.items():
            if series == "AH4EM":
                continue
            self.assertNotIn("aux_battery_voltage_correction", profile, series)
        self.assertNotIn(
            "aux_battery_voltage_correction", CONST.DEFAULT_VEHICLE_PROFILE
        )
        for series in ("EH32 X3", "MIS3E S", "AS33P", "SOMETHING NEW"):
            self.assertIsNone(_profiled(series).aux_battery_voltage_correction, series)


class SensorTests(unittest.TestCase):
    def test_the_urban_sensor_shows_the_corrected_voltage(self):
        sensor = _voltage_sensor(95, URBAN)
        self.assertEqual(sensor.native_value, 12.64)
        self.assertEqual(
            sensor.extra_state_attributes,
            {
                "corrected": True,
                "correction": "reported x 0.82 + 4.85",
                "reported_voltage": 9.5,
            },
        )

    def test_any_other_car_is_untouched(self):
        # Exactly what it was before: the raw figure times 0.1, nothing else.
        sensor = _voltage_sensor(127, None)
        self.assertEqual(sensor.native_value, 127 * 0.1)
        self.assertIsNone(sensor.extra_state_attributes)

    def test_a_missing_reading_is_not_corrected_into_one(self):
        # 0 and -128 are "no reading"; the correction must not turn either
        # into a plausible-looking 4.85 V.
        for raw in (0, -128):
            sensor = _voltage_sensor(raw, URBAN)
            self.assertIsNone(sensor.native_value, raw)
            self.assertNotIn("reported_voltage", sensor.extra_state_attributes)

    def test_a_missing_reading_keeps_the_last_corrected_one(self):
        sensor = _voltage_sensor(95, URBAN)
        self.assertEqual(sensor.native_value, 12.64)
        sensor.coordinator.data["status"].basicVehicleStatus.batteryVoltage = -128
        self.assertEqual(sensor.native_value, 12.64)

    def test_other_sensors_of_that_class_get_no_attributes(self):
        sensor = _voltage_sensor(95, URBAN)
        sensor._field = "mileage"
        self.assertIsNone(sensor.extra_state_attributes)


class ReachabilityTests(unittest.TestCase):
    def _attrs(self, raw, correction):
        coordinator = NS(
            data={"status": NS(basicVehicleStatus=NS(batteryVoltage=raw))},
            aux_battery_voltage_correction=correction,
            last_vehicle_activity=None,
            vehicle_reachability="Reachable",
        )
        sensor = SENSOR.SAICMGVehicleReachabilitySensor.__new__(
            SENSOR.SAICMGVehicleReachabilitySensor
        )
        sensor.coordinator = coordinator
        return sensor.extra_state_attributes

    def test_the_cars_own_figure_stays_and_the_corrected_one_is_added(self):
        attrs = self._attrs(95, URBAN)
        self.assertEqual(attrs["reported_battery_voltage"], 9.5)
        self.assertEqual(attrs["corrected_battery_voltage"], 12.64)

    def test_nothing_is_added_for_other_cars(self):
        attrs = self._attrs(127, None)
        self.assertEqual(attrs["reported_battery_voltage"], 12.7)
        self.assertNotIn("corrected_battery_voltage", attrs)


if __name__ == "__main__":
    unittest.main()
