"""Classic fan_speed cars: Cool must not read back as Heat (#495, @kimkha).

MG4 Electric (EH32, fan_speed scheme), Australia, 18 Nov 2026, debug log:

    15:47:40  remoteClimateStatus 0
    15:49:34  Cool sent: temperature index 8 (22°C), fan speed 2 (medium), AC on
    15:49:47  remoteClimateStatus 2
    15:49:50  climate entity, Climate Mode select and sensor all switch to Heat

The car reports the fan byte it was sent as remoteClimateStatus. Cool at
medium fan sends 2, which is also the PTC heater's status (#173: Heat sends
heat_fan_speed 2 with the compressor off). Status 2 can't say which, so a
status equal to the byte HA just sent is read as the mode HA asked for. With
nothing sent this session (an app start), status 2 still reads as Heat.
"""

import unittest

from test_climate_mode_sensor_heat_cool import CLIMATE_MODE_FROM_STATUS
from test_climate_presets_and_local_control import _Base, _HVACMode, _run
from types import SimpleNamespace


def _eh32(base, *, status=0, fan="Medium"):
    """An EH32-like classic car: heat status {2}, cool status {3}, fans 1/2/3."""
    entity = base._entity(scheme="fan_speed", status=status, heat={2}, cool=3)
    entity.coordinator.requested_climate_byte = None
    entity._attr_fan_mode = fan
    return entity


def _car_reports(entity, status):
    entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = status


class KimkhasSessionTests(_Base):
    def test_cool_at_medium_fan_stays_cool(self):
        entity = _eh32(self)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        kwargs = entity._client.start_climate.await_args.kwargs
        self.assertEqual((kwargs["fan_speed"], kwargs["ac_on"]), (2, True))
        _car_reports(entity, 2)  # 15:49:47
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)
        self.assertEqual(entity.coordinator.requested_hvac_mode, "cool")

    def test_cool_at_low_fan_no_longer_unrecognised(self):
        entity = _eh32(self, fan="Low")
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        _car_reports(entity, 1)
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_cool_at_high_fan_still_cool(self):
        entity = _eh32(self, fan="High")
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        _car_reports(entity, 3)
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_ac_on_reads_back_as_ac_on(self):
        entity = _eh32(self)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        _car_reports(entity, 2)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)


class HeatUnchangedTests(_Base):
    def test_ptc_heat_still_reads_heat(self):
        entity = _eh32(self)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        kwargs = entity._client.start_climate.await_args.kwargs
        self.assertEqual((kwargs["fan_speed"], kwargs["ac_on"]), (2, False))
        _car_reports(entity, 2)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)
        self.assertEqual(entity.coordinator.requested_hvac_mode, "heat")

    def test_status_2_with_nothing_sent_is_still_heat(self):
        # App start, or HA restarted mid-session: nothing to go on, as before.
        entity = _eh32(self, status=2)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)


class OtherStatusesTests(_Base):
    def test_a_different_status_falls_through_to_the_sets(self):
        # Cool sent at medium (2), car reports 5 (front defrost): not the byte
        # HA sent, so the profile's sets decide, as before.
        entity = _eh32(self)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        _car_reports(entity, 5)
        self.assertEqual(entity.coordinator.requested_climate_byte, 2)
        # 5 is the defrost status here, which hvac_mode shows as Cool.
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_fan_only_clears_the_byte(self):
        entity = _eh32(self)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        _run(entity.async_set_hvac_mode(_HVACMode.FAN_ONLY))
        self.assertIsNone(entity.coordinator.requested_climate_byte)

    def test_low_preset_records_its_byte(self):
        entity = _eh32(self)
        _run(entity.async_set_preset_mode("low"))
        self.assertEqual(entity.coordinator.requested_climate_byte, 3)
        _car_reports(entity, 3)
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_mode_select_cars_never_record_a_byte(self):
        entity = self._entity(scheme="mode_select", status=0)
        entity.coordinator.requested_climate_byte = None
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        self.assertIsNone(entity.coordinator.requested_climate_byte)

    def test_nothing_recorded_when_the_command_fails(self):
        entity = _eh32(self)
        entity._client.start_climate.side_effect = RuntimeError("return code: 4")
        try:
            _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        except Exception:  # noqa: BLE001 - only the bookkeeping matters here
            pass
        self.assertIsNone(entity.coordinator.requested_climate_byte)
        self.assertEqual(entity.coordinator.requested_hvac_mode, "off")


def _sensor(status, *, requested="off", byte=None):
    """The Climate Mode sensor's coordinator side, for EH32."""
    return SimpleNamespace(
        current_remote_climate_status=status,
        climate_mode_cool=2,  # mode_select default; meaningless on EH32
        climate_mode_heat=4,
        climate_status_cool={3},
        climate_status_heat={2},
        climate_status_defrost={5},
        climate_status_fan_only={4},
        requested_hvac_mode=requested,
        requested_climate_byte=byte,
    )


class ClimateModeSensorTests(unittest.TestCase):
    def test_cool_at_medium(self):
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(_sensor(2, requested="cool", byte=2)), "cool")

    def test_ac_on_at_medium(self):
        self.assertEqual(
            CLIMATE_MODE_FROM_STATUS(_sensor(2, requested="heat_cool", byte=2)), "heat_cool"
        )

    def test_cool_at_low(self):
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(_sensor(1, requested="cool", byte=1)), "cool")

    def test_heat_unchanged(self):
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(_sensor(2, requested="heat", byte=2)), "heat")

    def test_app_start_unchanged(self):
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(_sensor(2)), "heat")

    def test_defrost_not_swallowed(self):
        self.assertEqual(
            CLIMATE_MODE_FROM_STATUS(_sensor(5, requested="cool", byte=2)), "defrost"
        )


if __name__ == "__main__":
    unittest.main()
