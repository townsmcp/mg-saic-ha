"""MG Cyberster (EC32) heating (#509, @SecurityJon).

23 Nov 2026, 7 C outside, car locked, debug log:

    15:05:53  start_climate: temperature index 10 (24 C), fan 2, AC off
    15:07:28  remoteClimateStatus 2, interior 17 C
    15:11:38  remoteClimateStatus 2, interior 19 C
    At the car: heating running, fan on Auto, 24 C set.

The MG4's heat recipe (#173), at the temperature asked for. Before this the
Cyberster had no Heat mode, every "on" mode ran the compressor (cold air), and
status 2 -- the default fan-only status -- showed as Fan Only while it heated.
"""

import unittest

import test_vehicle_profiles as profiles
from test_climate_presets_and_local_control import _Base, _HVACMode, _run

EC32 = profiles.const.VEHICLE_PROFILES["EC32"]


def _cyberster(base, *, status=0, target=22.0, uses_target=True):
    entity = base._entity(scheme="fan_speed", status=status, heat={2}, cool=3, defrost=set())
    c = entity.coordinator
    c.climate_status_fan_only = {4}
    c.requested_climate_byte = None
    c.requested_target_temp = target
    c.heat_uses_target_temp = uses_target
    return entity


class ProfileTests(unittest.TestCase):
    def test_status_2_is_heat(self):
        self.assertEqual(EC32["climate_status_heat"], {2})
        self.assertNotIn(2, EC32["climate_status_fan_only"])
        self.assertNotIn(2, EC32["climate_status_cool"])

    def test_heats_to_the_target(self):
        self.assertTrue(EC32["heat_uses_target_temp"])

    def test_no_cool_fan_byte_collides_with_heat(self):
        # Cool at any fan speed must not send the heat status as its byte.
        fans = {EC32["fan_speed_low"], EC32["fan_speed_medium"], EC32["fan_speed_high"]}
        self.assertNotIn(2, fans)

    def test_only_the_cyberster_heats_to_the_target(self):
        others = [
            series
            for series, p in profiles.const.VEHICLE_PROFILES.items()
            if p.get("heat_uses_target_temp") and series != "EC32"
        ]
        self.assertEqual(others, [])


class HeatTests(_Base):
    def test_heat_is_offered(self):
        entity = _cyberster(self)
        self.assertIn(_HVACMode.HEAT, entity._attr_hvac_modes)

    def test_jons_command(self):
        entity = _cyberster(self, target=24.0)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        kwargs = entity._client.start_climate.await_args.kwargs
        self.assertEqual(kwargs["fan_speed"], 2)
        self.assertIs(kwargs["ac_on"], False)
        entity.coordinator.get_ac_temperature_idx.assert_called_with(24)
        self.assertEqual(kwargs["temperature_idx"], 24 - 13)

    def test_the_setpoint_is_left_alone(self):
        entity = _cyberster(self, target=22.0)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        self.assertEqual(entity.coordinator.requested_target_temp, 22.0)

    def test_reads_back_as_heat(self):
        entity = _cyberster(self)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = 2
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)

    def test_started_from_the_app_reads_heat_not_fan_only(self):
        self.assertEqual(_cyberster(self, status=2).hvac_mode, _HVACMode.HEAT)

    def test_cool_still_runs_the_compressor(self):
        entity = _cyberster(self)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        self.assertIs(entity._client.start_climate.await_args.kwargs["ac_on"], True)


class Mg4UnchangedTests(_Base):
    def test_mg4_still_heats_at_the_top_of_the_range(self):
        entity = _cyberster(self, target=22.0, uses_target=False)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        entity.coordinator.get_ac_temperature_idx.assert_called_with(30)

    def test_a_coordinator_without_the_setting_heats_at_the_top(self):
        entity = _cyberster(self, target=22.0)
        del entity.coordinator.heat_uses_target_temp
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        entity.coordinator.get_ac_temperature_idx.assert_called_with(30)


if __name__ == "__main__":
    unittest.main()
