"""Last Charge Duration: how long the last charge spent charging (#262).

The car's own Charging Duration counter restarts whenever charging pauses and
restarts, so at the end of a charge it only covers the final stretch. The
whole-charge figure was already worked out (trip_stats.compute_charge_session)
but only published as ``duration_s``, an attribute on Last Charge Energy: a
bare number of seconds with no unit. This is the same figure as a sensor.

Harnesses are reused from test_erac_fallback (HA-stubbed sensor.py) and
test_charge_restarts (@HarryFlatter's charge of 2 Oct 2026, replayed).
"""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from test_charge_restarts import HARRYS_POLLS, _at, _replay
from test_erac_fallback import SENSOR

SENSOR_SOURCE = (
    Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic" / "sensor.py"
).read_text()


def _sensor(last_charge, *, has_stats=True):
    coordinator = NS(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="HS"),
        trip_stats=NS(last_charge=last_charge) if has_stats else None,
    )
    return SENSOR.SAICMGLastChargeDurationSensor(coordinator, NS(entry_id="e"))


class IdentityTests(unittest.TestCase):
    def test_name_and_unique_id(self):
        entity = _sensor(None)
        self.assertEqual(entity.name, "MG HS Last Charge Duration")
        self.assertEqual(entity.unique_id, "e_VIN1_last_charge_duration")

    def test_it_is_a_duration_in_minutes_like_charging_duration(self):
        entity = _sensor(None)
        self.assertEqual(
            entity._attr_device_class, SENSOR.SensorDeviceClass.DURATION
        )
        self.assertEqual(
            entity._attr_native_unit_of_measurement, SENSOR.UnitOfTime.MINUTES
        )
        # Not the stub answering the same thing for every name.
        self.assertNotEqual(
            entity._attr_native_unit_of_measurement, SENSOR.UnitOfTime.SECONDS
        )

    def test_registered_next_to_the_other_last_charge_sensors(self):
        energy = SENSOR_SOURCE.index(
            "sensors.append(SAICMGLastChargeEnergySensor(coordinator, entry))"
        )
        duration = SENSOR_SOURCE.index(
            "sensors.append(SAICMGLastChargeDurationSensor(coordinator, entry))"
        )
        freshness = SENSOR_SOURCE.index("SAICMGChargingDataFreshnessSensor(")
        # Inside the same BEV/PHEV + charging-data block as Last Charge Energy.
        self.assertLess(energy, duration)
        self.assertLess(duration, freshness)


class ValueTests(unittest.TestCase):
    def test_unknown_but_available_before_any_charge(self):
        entity = _sensor(None)
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)

    def test_no_trip_stats_yet(self):
        entity = _sensor(None, has_stats=False)
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)

    def test_seconds_become_minutes(self):
        self.assertEqual(_sensor({"duration_s": 1680}).native_value, 28.0)
        self.assertEqual(_sensor({"duration_s": 6216}).native_value, 103.6)

    def test_a_charge_with_no_duration_is_unknown_not_zero(self):
        for charge in (
            {"energy_added_kWh": 5.0},
            {"duration_s": None},
            {"duration_s": 0},
            {"duration_s": -40},
            {"duration_s": "6216"},
            {"duration_s": True},
        ):
            with self.subTest(charge=charge):
                self.assertIsNone(_sensor(charge).native_value)

    def test_follows_the_next_charge(self):
        entity = _sensor({"duration_s": 1680})
        self.assertEqual(entity.native_value, 28.0)
        entity.coordinator.trip_stats.last_charge = {"duration_s": 3600}
        self.assertEqual(entity.native_value, 60.0)


class AttributeTests(unittest.TestCase):
    def test_only_the_timing_fields(self):
        attrs = _sensor(
            {
                "energy_added_kWh": 8.65,
                "soc_start_pct": 62.7,
                "duration_s": 6216,
                "duration_source": "car",
                "interruptions": 2,
                "paused_s": 74,
                "charge_start_ts": "2026-10-01T23:36:43+00:00",
                "charge_end_ts": "2026-10-02T01:21:33+00:00",
                "start_ts": "2026-10-01T23:04:25+00:00",
                "end_ts": "2026-10-02T01:50:49+00:00",
            }
        ).extra_state_attributes
        self.assertEqual(
            attrs,
            {
                "duration_s": 6216,
                "duration_source": "car",
                "interruptions": 2,
                "paused_s": 74,
                "charge_start_ts": "2026-10-01T23:36:43+00:00",
                "charge_end_ts": "2026-10-02T01:21:33+00:00",
            },
        )

    def test_fields_the_charge_does_not_have_are_left_out(self):
        attrs = _sensor(
            {"duration_s": 1680, "duration_source": "polls"}
        ).extra_state_attributes
        self.assertEqual(attrs, {"duration_s": 1680, "duration_source": "polls"})


class HarrysChargeTests(unittest.TestCase):
    """1 h 44 min 50 s from first start to end, less 74 s of pauses."""

    def setUp(self):
        _charge, manager = _replay(HARRYS_POLLS)
        self.entity = _sensor(None)
        self.entity.coordinator.trip_stats = manager

    def test_the_whole_charge_not_the_last_stretch(self):
        # The car's own counter ended on the last stretch: 26 minutes.
        self.assertEqual(self.entity.native_value, 103.6)

    def test_attributes(self):
        attrs = self.entity.extra_state_attributes
        self.assertEqual(attrs["duration_s"], 6216)
        self.assertEqual(attrs["duration_source"], "car")
        self.assertEqual(attrs["interruptions"], 2)
        self.assertEqual(attrs["paused_s"], 74)
        self.assertEqual(attrs["charge_start_ts"], _at(1, 23, 36, 43).isoformat())
        self.assertEqual(attrs["charge_end_ts"], _at(2, 1, 21, 33).isoformat())

    def test_matches_the_attribute_on_last_charge_energy(self):
        energy = SENSOR.SAICMGLastChargeEnergySensor(
            self.entity.coordinator, NS(entry_id="e")
        )
        self.assertEqual(
            energy.extra_state_attributes["duration_s"],
            self.entity.extra_state_attributes["duration_s"],
        )


if __name__ == "__main__":
    unittest.main()
