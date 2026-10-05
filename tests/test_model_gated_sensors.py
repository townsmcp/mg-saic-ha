"""Readings the car sends that were not shown, and which models get them (#408).

Requested by @hoffeck. What each field does was checked on two cars:

                              MGS6 (MIS3E)                HS PHEV (AS33P)
    currentJourneyDistance    tenths of a km, = odometer  same
    handBrake                 0 parked, driving,          1 parked,
                              charging -- always          0 driving
    bmsChrgOtptCrntReq        102-105 beside 5.0-5.4 A    81 beside ~15 A
                              (x0.05 fits)                (x0.05 does not)
    onBdChrgrAltrCrntInptCrnt 51 with the wall charger    not checked
                              showing 10.5 A (x0.2)
    onBdChrgrAltrCrntInptVol  117-120 with it showing     not checked
                              238 V (x2)

So Journey Distance is for any car that reports it, and the rest only where a
vehicle profile says the reading is good, with its scale.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from test_erac_fallback import SENSOR
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

CONST = sys.modules["mg_saic.const"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
BINARY_SENSOR_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "mg_saic" / "binary_sensor.py"
).read_text()
SENSOR_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components" / "mg_saic" / "sensor.py"
).read_text()

FACTOR_KEYS = (
    "charge_current_request_factor",
    "obc_input_current_factor",
    "obc_input_voltage_factor",
)


def _coordinator(*, basic=None, chrg=None, **attrs):
    data = {
        "status": NS(basicVehicleStatus=basic) if basic is not None else None,
        "charging": NS(chrgMgmtData=chrg) if chrg is not None else None,
    }
    return NS(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV"),
        data=data,
        **attrs,
    )


def _profiled(series):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vehicle_series = series
    c.battery_capacity_override = None
    COORD_CLS._apply_vehicle_profile(c)
    return c


class ProfileTests(unittest.TestCase):
    def test_the_mgs6_has_the_charging_scales(self):
        profile = CONST.VEHICLE_PROFILES["MIS3E"]
        self.assertEqual(profile["charge_current_request_factor"], 0.05)
        self.assertEqual(profile["obc_input_current_factor"], 0.2)
        self.assertEqual(profile["obc_input_voltage_factor"], 2.0)

    def test_no_other_model_has_them_yet(self):
        for series, profile in CONST.VEHICLE_PROFILES.items():
            if series == "MIS3E":
                continue
            for key in FACTOR_KEYS:
                self.assertNotIn(key, profile, f"{series} {key}")

    def test_only_the_hs_phev_has_a_handbrake_reading(self):
        with_it = [
            series for series, profile in CONST.VEHICLE_PROFILES.items()
            if profile.get("handbrake_reported")
        ]
        self.assertEqual(with_it, ["AS33P"])

    def test_the_coordinator_carries_them(self):
        mgs6 = _profiled("MIS3E S")
        self.assertEqual(mgs6.charge_current_request_factor, 0.05)
        self.assertEqual(mgs6.obc_input_current_factor, 0.2)
        self.assertEqual(mgs6.obc_input_voltage_factor, 2.0)
        self.assertFalse(mgs6.handbrake_reported)

        hs = _profiled("AS33P S")
        self.assertTrue(hs.handbrake_reported)
        for key in FACTOR_KEYS:
            self.assertIsNone(getattr(hs, key), key)

    def test_an_unprofiled_car_gets_none_of_them(self):
        unknown = _profiled("ZZ99 X")
        self.assertFalse(unknown.handbrake_reported)
        for key in FACTOR_KEYS:
            self.assertIsNone(getattr(unknown, key), key)


class JourneyDistanceTests(unittest.TestCase):
    def _sensor(self, distance, journey=681):
        c = _coordinator(
            basic=NS(currentJourneyDistance=distance, currentJourneyId=journey)
        )
        return SENSOR.SAICMGJourneyDistanceSensor(c, NS(entry_id="e")), c

    def test_identity(self):
        entity, _ = self._sensor(110)
        self.assertEqual(entity.name, "MG MGS6 EV Journey Distance")
        self.assertEqual(entity.unique_id, "e_VIN1_journey_distance")
        self.assertEqual(entity._attr_device_class, SENSOR.SensorDeviceClass.DISTANCE)

    def test_tenths_of_a_kilometre(self):
        # 5 Oct, second drive: 60 at 18:19, 110 at 18:32, odometer +6.0, +11.0.
        self.assertEqual(self._sensor(60)[0].native_value, 6.0)
        self.assertEqual(self._sensor(110)[0].native_value, 11.0)
        self.assertEqual(self._sensor(0)[0].native_value, 0.0)

    def test_the_journey_number_is_an_attribute(self):
        entity, _ = self._sensor(110)
        self.assertEqual(entity.extra_state_attributes, {"journey_id": 681})
        self.assertIsNone(self._sensor(110, journey=None)[0].extra_state_attributes)

    def test_a_poll_with_nothing_keeps_the_last_reading(self):
        entity, c = self._sensor(110)
        self.assertEqual(entity.native_value, 11.0)
        for nothing in (
            {"status": None, "charging": None},
            {"status": NS(basicVehicleStatus=NS(currentJourneyDistance=-128))},
            {},
        ):
            c.data = nothing
            self.assertEqual(entity.native_value, 11.0)
            self.assertTrue(entity.available)

    def test_only_created_where_the_car_reports_it(self):
        reported = SENSOR.SAICMGJourneyDistanceSensor.reported_by
        self.assertTrue(reported(_coordinator(basic=NS(currentJourneyDistance=0))))
        self.assertFalse(reported(_coordinator(basic=NS(currentJourneyDistance=None))))
        self.assertFalse(reported(_coordinator(basic=NS())))

    def test_created_when_the_car_could_not_be_asked(self):
        # Home Assistant started while the car was asleep: there is no way
        # to tell, and a sensor that comes and goes with restarts is worse
        # than one that reads unknown for a while.
        reported = SENSOR.SAICMGJourneyDistanceSensor.reported_by
        self.assertTrue(reported(_coordinator()))
        self.assertTrue(reported(NS(data=None)))
        entity = SENSOR.SAICMGJourneyDistanceSensor(_coordinator(), NS(entry_id="e"))
        self.assertIsNone(entity.native_value)
        self.assertTrue(entity.available)

    def test_registered_behind_that_check(self):
        self.assertIn(
            "if SAICMGJourneyDistanceSensor.reported_by(coordinator):", SENSOR_SOURCE
        )


class RequestedChargingCurrentTests(unittest.TestCase):
    def _sensor(self, **chrg):
        c = _coordinator(chrg=NS(**chrg), charge_current_request_factor=0.05)
        return SENSOR.SAICMGRequestedChargingCurrentSensor(c, NS(entry_id="e")), c

    def test_identity(self):
        entity, _ = self._sensor(bmsChrgOtptCrntReq=102, bmsChrgOtptCrntReqV=0, bmsChrgSts=1)
        self.assertEqual(entity.name, "MG MGS6 EV Requested Charging Current")
        self.assertEqual(entity.unique_id, "e_VIN1_requested_charging_current")
        self.assertEqual(entity._attr_native_unit_of_measurement, "A")

    def test_while_charging(self):
        # Beside a pack current of 5.0-5.4 A.
        for raw, amps in ((102, 5.1), (105, 5.25)):
            entity, _ = self._sensor(
                bmsChrgOtptCrntReq=raw, bmsChrgOtptCrntReqV=0, bmsChrgSts=1
            )
            self.assertEqual(entity.native_value, amps)

    def test_not_charging_is_zero(self):
        # What the car sends when idle: 1023 with the "no value" flag set.
        for status in (0, 2, 5, 8):
            entity, _ = self._sensor(
                bmsChrgOtptCrntReq=1023, bmsChrgOtptCrntReqV=1, bmsChrgSts=status
            )
            self.assertEqual(entity.native_value, 0, status)

    def test_no_value_while_charging_keeps_the_last_reading(self):
        entity, c = self._sensor(
            bmsChrgOtptCrntReq=102, bmsChrgOtptCrntReqV=0, bmsChrgSts=1
        )
        self.assertEqual(entity.native_value, 5.1)
        c.data["charging"] = NS(
            chrgMgmtData=NS(bmsChrgOtptCrntReq=1023, bmsChrgOtptCrntReqV=1, bmsChrgSts=1)
        )
        self.assertEqual(entity.native_value, 5.1)
        c.data["charging"] = None
        self.assertEqual(entity.native_value, 5.1)

    def test_unknown_before_any_reading(self):
        entity, _ = self._sensor(
            bmsChrgOtptCrntReq=1023, bmsChrgOtptCrntReqV=1, bmsChrgSts=1
        )
        self.assertIsNone(entity.native_value)
        self.assertTrue(entity.available)


class ChargerInputTests(unittest.TestCase):
    """AC charging, 5 Oct: the wall charger showed 10.5 A, 238 V, 2.5 kW."""

    def _sensor(self, kind, current=51, volts=119):
        c = _coordinator(
            chrg=NS(onBdChrgrAltrCrntInptCrnt=current, onBdChrgrAltrCrntInptVol=volts),
            obc_input_current_factor=0.2,
            obc_input_voltage_factor=2.0,
        )
        return SENSOR.SAICMGChargerInputSensor(c, NS(entry_id="e"), kind), c

    def test_identity(self):
        for kind, name in (
            ("voltage", "Charger Input Voltage"),
            ("current", "Charger Input Current"),
            ("power", "Charger Input Power"),
        ):
            entity, _ = self._sensor(kind)
            self.assertEqual(entity.name, f"MG MGS6 EV {name}")
            self.assertEqual(entity.unique_id, f"e_VIN1_charger_input_{kind}")

    def test_units(self):
        self.assertEqual(self._sensor("current")[0]._attr_native_unit_of_measurement, "A")
        self.assertEqual(
            self._sensor("voltage")[0]._attr_native_unit_of_measurement,
            SENSOR.UnitOfElectricPotential.VOLT,
        )
        self.assertEqual(
            self._sensor("power")[0]._attr_native_unit_of_measurement,
            SENSOR.UnitOfPower.KILO_WATT,
        )

    def test_values(self):
        self.assertEqual(self._sensor("current")[0].native_value, 10.2)
        self.assertEqual(self._sensor("voltage")[0].native_value, 238.0)
        self.assertEqual(self._sensor("power")[0].native_value, 2.43)

    def test_unplugged_is_zero(self):
        for kind in ("current", "voltage", "power"):
            self.assertEqual(self._sensor(kind, 0, 0)[0].native_value, 0)

    def test_plugged_in_but_not_charging(self):
        # 18:32:36: 1 / 120 -- mains present, next to nothing flowing.
        self.assertEqual(self._sensor("current", 1, 120)[0].native_value, 0.2)
        self.assertEqual(self._sensor("voltage", 1, 120)[0].native_value, 240.0)
        self.assertEqual(self._sensor("power", 1, 120)[0].native_value, 0.05)

    def test_a_poll_with_nothing_keeps_the_last_reading(self):
        entity, c = self._sensor("power")
        self.assertEqual(entity.native_value, 2.43)
        c.data["charging"] = None
        self.assertEqual(entity.native_value, 2.43)
        c.data["charging"] = NS(
            chrgMgmtData=NS(onBdChrgrAltrCrntInptCrnt=-128, onBdChrgrAltrCrntInptVol=119)
        )
        self.assertEqual(entity.native_value, 2.43)


class GatingTests(unittest.TestCase):
    def _names(self, **factors):
        c = _coordinator(chrg=NS(), **factors)
        return [
            entity._name
            for entity in SENSOR.profile_charging_sensors(c, NS(entry_id="e"))
        ]

    def test_no_factors_no_sensors(self):
        self.assertEqual(self._names(), [])
        self.assertEqual(
            self._names(
                charge_current_request_factor=None,
                obc_input_current_factor=None,
                obc_input_voltage_factor=None,
            ),
            [],
        )

    def test_the_mgs6_gets_all_four(self):
        self.assertEqual(
            self._names(
                charge_current_request_factor=0.05,
                obc_input_current_factor=0.2,
                obc_input_voltage_factor=2.0,
            ),
            [
                "Requested Charging Current",
                "Charger Input Voltage",
                "Charger Input Current",
                "Charger Input Power",
            ],
        )

    def test_power_needs_both_scales(self):
        self.assertEqual(self._names(obc_input_current_factor=0.2), ["Charger Input Current"])
        self.assertEqual(self._names(obc_input_voltage_factor=2.0), ["Charger Input Voltage"])

    def test_one_scale_does_not_bring_the_others(self):
        self.assertEqual(
            self._names(charge_current_request_factor=0.05),
            ["Requested Charging Current"],
        )

    def test_registered_with_the_other_charging_sensors(self):
        self.assertIn(
            "sensors.extend(profile_charging_sensors(coordinator, entry))", SENSOR_SOURCE
        )

    def test_the_handbrake_sensor_is_behind_its_profile_flag(self):
        gate = BINARY_SENSOR_SOURCE.index(
            'if getattr(coordinator, "handbrake_reported", False):'
        )
        entity = BINARY_SENSOR_SOURCE.index('"Handbrake",')
        field = BINARY_SENSOR_SOURCE.index('"handBrake",')
        self.assertLess(gate, entity)
        self.assertLess(entity, field)
        self.assertEqual(BINARY_SENSOR_SOURCE.count('"handBrake",'), 1)


if __name__ == "__main__":
    unittest.main()
