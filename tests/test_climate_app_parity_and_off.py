"""MGS6 ordinary climate commands match the app, and Off shows straight away.

Two things from the MGS6 on 2026-10-03.

1. The AC flag. A decrypted iSmart capture at 09:44, the app starting the
   climate at an ordinary temperature (22 °C):

       POST /vehicle/control  rvcReqType 6
         paramId 19 (mode)          [2]
         paramId 20 (temperature)   [8]    index 8 = 22 °C
         paramId 22 (AC flag)       [0]    OFF
         paramId 255 (end marker)   [0]

   HA's Cool / Heat / Heat-Cool sent the same mode and index with the flag
   ON. With HIGH (24 Sep) and LOW (25 Sep) that makes three captures, all
   with the flag off, so on this car HA now sends it off too.

2. Off. At 09:11:45 HA sent a stop; the car accepted it at 09:11:48 and was
   off. HA went on showing Heat/Cool until its next status fetch at 09:12:11,
   and in between a second stop was sent to a car that was already off.
"""

import base64
import unittest
from unittest.mock import AsyncMock

from test_climate_mgs6_high import (
    HAVE_SAIC_LIB,
    _MGS6,
    _real_library_body,
    _run,
    _with_profile,
)
from test_climate_presets_and_local_control import CLIMATE, _HVACMode

APP_22C_2026_10_03 = {19: bytes([2]), 20: bytes([8]), 22: bytes([0]), 255: bytes([0])}


def _sent(entity):
    kwargs = entity._client.start_climate.call_args.kwargs
    return kwargs["fan_speed"], kwargs["temperature_idx"], kwargs["ac_on"]


class OrdinaryModesMatchTheAppTests(_MGS6):
    def test_heat_cool_cool_and_heat_send_the_flag_off(self):
        for mode in (_HVACMode.HEAT_COOL, _HVACMode.COOL, _HVACMode.HEAT):
            with self.subTest(mode=mode):
                entity = self._mgs6()
                _run(entity.async_set_hvac_mode(mode))
                self.assertEqual(_sent(entity), (2, 8, False))

    def test_clearing_a_preset_sends_the_flag_off(self):
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_NONE))
        self.assertEqual(_sent(entity), (2, 8, False))

    def test_the_setpoint_still_travels_with_the_command(self):
        entity = self._mgs6()
        entity.coordinator.requested_target_temp = 17.0
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertEqual(_sent(entity), (2, 3, False))  # 10:01, index 3 = 17 °C

    def test_high_and_low_are_unchanged(self):
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(_sent(entity), (2, 19, False))
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(_sent(entity), (2, 1, False))

    def test_fan_only_and_defrost_are_unchanged(self):
        # No capture of the app sending either on this car: left as they were.
        entity = self._mgs6()
        _run(entity.async_set_hvac_mode(_HVACMode.FAN_ONLY))
        self.assertEqual(_sent(entity)[0], 1)
        self.assertTrue(_sent(entity)[2])
        entity = self._mgs6()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_FRONT_WINDSCREEN))
        self.assertEqual(_sent(entity)[0], 5)
        self.assertTrue(_sent(entity)[2])

    @unittest.skipUnless(HAVE_SAIC_LIB, "pinned SAIC client library not installed")
    def test_request_body_matches_the_capture(self):
        """End to end, through the real SAIC library (see the HIGH test for
        why the end marker is 4 bytes rather than the app's 1)."""
        entity = self._mgs6()
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        fan_speed, temperature_idx, ac_on = _sent(entity)
        body = _real_library_body(
            fan_speed=fan_speed, ac_on=ac_on, temperature_idx=temperature_idx
        )
        self.assertEqual(str(body["rvcReqType"]), "6")
        ours = {p["paramId"]: base64.b64decode(p["paramValue"]) for p in body["rvcParams"]}
        self.assertEqual(sorted(ours), sorted(APP_22C_2026_10_03))
        for param_id in (19, 20, 22):
            self.assertEqual(ours[param_id], APP_22C_2026_10_03[param_id], f"paramId {param_id}")


class OtherCarsKeepTheFlagOnTests(_MGS6):
    """Only a car with a capture changes."""

    def test_other_mode_select_cars(self):
        for series in ("MZS3E", "P12L", "EP21", "AH4EM", "IS31P"):
            with self.subTest(series=series):
                entity = self._entity()
                _with_profile(entity.coordinator, series)
                _run(entity.async_set_hvac_mode(_HVACMode.COOL))
                self.assertTrue(entity._client.start_climate.call_args.kwargs["ac_on"])

    def test_a_coordinator_without_the_setting_defaults_to_on(self):
        entity = self._entity()  # fixture coordinator has no climate_ac_flag
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertTrue(entity._client.start_climate.call_args.kwargs["ac_on"])


class OffShowsStraightAwayTests(_MGS6):
    def _running(self):
        """HA started Heat/Cool and the status HA holds says running."""
        entity = self._mgs6(status=2)
        entity.coordinator.requested_hvac_mode = "heat_cool"
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)
        return entity

    def test_off_is_shown_before_the_status_catches_up(self):
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity._client.stop_ac.assert_awaited_once()
        # The status HA holds still says 2.
        self.assertEqual(entity.hvac_mode, _HVACMode.OFF)
        self.assertEqual(entity.preset_mode, CLIMATE.PRESET_NONE)

    def test_a_refresh_follows_a_stop_from_the_mode_list(self):
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity.coordinator.schedule_action_refresh.assert_awaited_once()

    def test_the_power_button_still_refreshes_once(self):
        entity = self._running()
        _run(entity.async_turn_off())
        entity._client.stop_ac.assert_awaited_once()
        entity.coordinator.schedule_action_refresh.assert_awaited_once()
        self.assertEqual(entity.hvac_mode, _HVACMode.OFF)

    def test_off_stays_off_once_the_car_reports_it(self):
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = 0
        self.assertEqual(entity.hvac_mode, _HVACMode.OFF)
        self.assertEqual(entity._off_sent_ts, 0.0)

    def test_a_session_started_elsewhere_afterwards_is_shown(self):
        # Car reported off, then the app started it: nothing is pending.
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        status = entity.coordinator.data["status"].basicVehicleStatus
        status.remoteClimateStatus = 0
        entity.hvac_mode
        status.remoteClimateStatus = 2
        entity.coordinator.requested_hvac_mode = "off"
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_a_start_from_ha_right_after_the_stop_is_shown(self):
        # 09:12:15: Heat/Cool again, five seconds after the stop.
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_a_stop_the_car_ignored_does_not_show_off_for_ever(self):
        entity = self._running()
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity._off_sent_ts -= CLIMATE.STOP_SYNC_GRACE_SECONDS + 1
        # Still reporting 2 after the grace period: believe the car.
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_a_failed_stop_changes_nothing(self):
        entity = self._running()
        entity._client.stop_ac = AsyncMock(side_effect=RuntimeError("return code: 4"))
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)
        entity.coordinator.schedule_action_refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
