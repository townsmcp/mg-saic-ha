"""Locally-held settings kept across a restart (from 2.0.0-beta11).

The front heated seat level selects (select.<car>_heated_seat_front_left_level
/ _front_right_level) and the Climate Target Temperature number hold what you
chose until the next command sends it -- but only in memory. After a restart
the seat levels went back to the car's reading (Off with the car off), so the
next switch-on used Low, and the target temperature went back to 22 C.
"""

import asyncio
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_charging_controls_dropout import MODS

SELECT = MODS["select"]
NUMBER = MODS["number"]


def _seat(last_state, *, pending=None, car_level=0):
    coordinator = NS(
        pending_seat_levels={} if pending is None else {"front_left": pending},
        data={"status": NS(basicVehicleStatus=NS(frontLeftSeatHeatLevel=car_level))},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV"),
    )
    select = SELECT.SAICMGHeatedSeatLevelSelect.__new__(SELECT.SAICMGHeatedSeatLevelSelect)
    select.coordinator = coordinator
    select._seat_key = "front_left"
    select._status_field = "frontLeftSeatHeatLevel"
    select._client = MagicMock(spec=[])
    select.async_write_ha_state = MagicMock()

    async def last():
        return None if last_state is None else NS(state=last_state, attributes={})

    select.async_get_last_state = last
    return select


class SeatLevelTests(unittest.TestCase):
    def test_a_chosen_level_comes_back(self):
        for option, level in (("Low", 1), ("Medium", 2), ("High", 3)):
            select = _seat(option)
            asyncio.run(select.async_added_to_hass())
            self.assertEqual(select.coordinator.pending_seat_levels["front_left"], level)
            self.assertEqual(select.current_option, option)
            select.async_write_ha_state.assert_called_once()

    def test_off_is_left_to_the_car(self):
        # Restoring Off would hide the car's level if the seat is heating now.
        select = _seat("Off", car_level=2)
        asyncio.run(select.async_added_to_hass())
        self.assertNotIn("front_left", select.coordinator.pending_seat_levels)
        self.assertEqual(select.current_option, "Medium")

    def test_nothing_to_restore(self):
        for state in (None, "unknown", "unavailable"):
            select = _seat(state)
            asyncio.run(select.async_added_to_hass())
            self.assertEqual(select.coordinator.pending_seat_levels, {}, state)

    def test_a_level_already_chosen_wins(self):
        select = _seat("High", pending=1)
        asyncio.run(select.async_added_to_hass())
        self.assertEqual(select.coordinator.pending_seat_levels["front_left"], 1)

    def test_the_switch_then_uses_it(self):
        select = _seat("High")
        asyncio.run(select.async_added_to_hass())
        switch = MODS["switch"].SAICMGHeatedSeatSwitch.__new__(MODS["switch"].SAICMGHeatedSeatSwitch)
        switch.coordinator = select.coordinator
        switch._seat_class = "front"
        switch._seat_key = "front_left"
        switch._client = MagicMock(spec=[])
        self.assertEqual(switch._on_level(), 3)


def _temp(last_state, *, min_temp=16, max_temp=30):
    coordinator = NS(
        requested_target_temp=22.0,
        min_temp=min_temp,
        max_temp=max_temp,
        async_update_listeners=MagicMock(),
    )
    number = NUMBER.SAICMGClimateTargetTempNumber.__new__(NUMBER.SAICMGClimateTargetTempNumber)
    number.coordinator = coordinator
    number.async_write_ha_state = MagicMock()

    async def last():
        return None if last_state is None else NS(state=last_state, attributes={})

    number.async_get_last_state = last
    return number


class TargetTemperatureTests(unittest.TestCase):
    def test_comes_back(self):
        number = _temp("24.0")
        asyncio.run(number.async_added_to_hass())
        self.assertEqual(number.coordinator.requested_target_temp, 24)
        self.assertEqual(number.native_value, 24.0)
        number.coordinator.async_update_listeners.assert_called_once()

    def test_outside_this_cars_range_is_ignored(self):
        number = _temp("31", max_temp=30)
        asyncio.run(number.async_added_to_hass())
        self.assertEqual(number.coordinator.requested_target_temp, 22.0)

    def test_nothing_to_restore(self):
        for state in (None, "unknown", "unavailable", ""):
            number = _temp(state)
            asyncio.run(number.async_added_to_hass())
            self.assertEqual(number.coordinator.requested_target_temp, 22.0, state)


if __name__ == "__main__":
    unittest.main()
