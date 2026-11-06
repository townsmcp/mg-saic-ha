"""The A/C on control on start_ac cars: failures recorded, not raised (#470).

@sharky-os, 6 Nov 2026: on an MG4 Urban (AH4EM), turning the A/C on from an
automation was refused with return code 8 ("Too frequent operations ...
(71)"). Nothing reached the Command Errors event or a notification, and the
error stopped the automation. On cars with cool_uses_start_ac (AH4EM, ZP22,
ZS32, EP21, P12L, MIS3E) the climate power button and the A/C switch send
start_ac straight from climate.async_turn_on, which had no error handling.
"""

import unittest

from test_climate_presets_and_local_control import _Base, _HVACMode, _run

CODE_8 = RuntimeError(
    "return code: 8, message: Too frequent operations. Please use the physical "
    "key to restart the vehicle, lock the car and turn off the engine, and "
    "then try again.(71)"
)
CODE_4 = RuntimeError(
    "return code: 4, message: The remote control instruction failed, please try again later."
)


class TurnOnFailureTests(_Base):
    def _failing(self, error):
        entity = self._entity(cool_uses_start_ac=True)
        entity._attr_hvac_mode = _HVACMode.OFF
        entity._client.start_ac.side_effect = error
        return entity

    def test_refusal_is_recorded_and_not_raised(self):
        entity = self._failing(CODE_8)
        _run(entity.async_turn_on())  # must not raise
        entity.coordinator.record_command_error.assert_called_once_with(
            "Error turning the A/C on", CODE_8
        )

    def test_any_other_failure_too(self):
        entity = self._failing(CODE_4)
        _run(entity.async_turn_on())
        entity.coordinator.record_command_error.assert_called_once_with(
            "Error turning the A/C on", CODE_4
        )

    def test_not_shown_as_on_and_no_refresh_after_a_failure(self):
        entity = self._failing(CODE_8)
        _run(entity.async_turn_on())
        self.assertEqual(entity._attr_hvac_mode, _HVACMode.OFF)
        entity.coordinator.schedule_action_refresh.assert_not_awaited()

    def test_success_unchanged(self):
        entity = self._entity(cool_uses_start_ac=True)
        _run(entity.async_turn_on())
        entity._client.start_ac.assert_awaited_once()
        self.assertEqual(entity._attr_hvac_mode, _HVACMode.COOL)
        entity.coordinator.schedule_action_refresh.assert_awaited_once()
        entity.coordinator.record_command_error.assert_not_called()


if __name__ == "__main__":
    unittest.main()
