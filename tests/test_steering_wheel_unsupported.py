"""A clearer message when the steering wheel command gets return code 4 (#463).

@iphikles3000's MG4 Electric (MY2023): its iSmart app has no remote steering
wheel heating, and every steering wheel command came back with return code 4,
"try again later", whether the car was on or off. Heated seats on the same car
worked. Code 4 is also what any command gets when the car can't be reached, so
the message says "probably", and only the steering wheel switch uses it.
"""

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_charging_controls_dropout import MODS

SWITCH = MODS["switch"]
CODE_4 = RuntimeError(
    "return code: 4, message: The remote control instruction failed, please try again later."
)
CODE_8 = RuntimeError(
    "return code: 8, message: Vehicle is powered on. Please turn it off and try again.(1)"
)


def _coordinator():
    return NS(
        record_command_error=MagicMock(),
        schedule_action_refresh=AsyncMock(),
        after_action_delay=10,
        heated_seats_long_interval=60,
        pending_seat_levels={},
    )


def _wheel(error):
    entity = SWITCH.SAICMGSteeringWheelHeatSwitch.__new__(SWITCH.SAICMGSteeringWheelHeatSwitch)
    entity.coordinator = _coordinator()
    entity._client = NS(control_steering_wheel_heat=AsyncMock(side_effect=error))
    entity._vin = "VIN1"
    entity._attr_is_on = False
    entity.async_write_ha_state = MagicMock()
    return entity


def _seat(error):
    entity = SWITCH.SAICMGHeatedSeatSwitch.__new__(SWITCH.SAICMGHeatedSeatSwitch)
    entity.coordinator = _coordinator()
    entity._client = NS(control_heated_seat=AsyncMock(side_effect=error))
    entity._vin = "VIN1"
    entity._seat_key = "front_left"
    return entity


class SteeringWheelTests(unittest.TestCase):
    def test_code_4_on(self):
        wheel = _wheel(CODE_4)
        with self.assertLogs(SWITCH.LOGGER, "ERROR") as logs:
            asyncio.run(wheel.async_turn_on())
        self.assertIn("probably doesn't support remote steering wheel heating", logs.output[0])
        self.assertIn("untick Has Steering Wheel Heat", logs.output[0])
        source, message = wheel.coordinator.record_command_error.call_args.args
        self.assertEqual(source, "Error steering wheel heat")
        self.assertIn("couldn't be reached", message)
        # SAIC's own words are kept.
        self.assertIn("return code: 4", message)

    def test_code_4_off(self):
        wheel = _wheel(CODE_4)
        asyncio.run(wheel.async_turn_off())
        message = wheel.coordinator.record_command_error.call_args.args[1]
        self.assertIn("Has Steering Wheel Heat", message)

    def test_other_codes_unchanged(self):
        wheel = _wheel(CODE_8)
        asyncio.run(wheel.async_turn_on())
        message = wheel.coordinator.record_command_error.call_args.args[1]
        self.assertEqual(message, str(CODE_8))

    def test_code_4_still_counts_as_unreachable(self):
        # The text still carries the code, so reachability sees it as before.
        self.assertEqual(
            SWITCH.is_vehicle_unreachable(SWITCH.steering_wheel_error_message(CODE_4)), True
        )


class HeatedSeatTests(unittest.TestCase):
    def test_a_seat_never_gets_the_steering_wheel_message(self):
        seat = _seat(CODE_4)
        asyncio.run(seat._apply(1))
        source, error = seat.coordinator.record_command_error.call_args.args
        self.assertEqual(source, "Error controlling heated seat")
        self.assertNotIn("steering wheel", str(error))


class OptionTextTests(unittest.TestCase):
    def test_in_every_language_at_setup_and_in_configure(self):
        folder = Path(SWITCH.__file__).parent / "translations"
        for path in sorted(folder.glob("*.json")):
            data = json.loads(path.read_text())
            for step in (
                data["config"]["step"]["vehicle_capabilities"],
                data["options"]["step"]["init"],
            ):
                self.assertIn("has_steering_wheel_heat", step["data_description"], path.name)


if __name__ == "__main__":
    unittest.main()
