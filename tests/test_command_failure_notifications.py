"""Every failed command: the Command Errors event and a notification.

James, 5 Nov 2026: failures need the same visibility whichever way a command
was sent and whatever went wrong -- the Command Errors event and a persistent
notification -- without raising an error that stops the user's automations.
Before this, only a refusal (return code 8) from an entity got a
notification; a car that couldn't be reached (code 4) or any other failure
was only in the event, and Target SOC and Charging Current Limit failures
were only logged.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charging_controls_dropout import MODS

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
API = sys.modules["mg_saic.api"]

CODE_4 = RuntimeError(
    "return code: 4, message: The remote control instruction failed, please try again later."
)
CODE_500 = RuntimeError("return code: 500, message: unexpected")
CODE_8 = RuntimeError("return code: 8, message: Too frequent operations")


def _coordinator():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "VIN1"
    c.vin_info = NS(brandName="MG", modelName="MG4")
    c.hass = MagicMock()
    c.hass.services.async_call = AsyncMock()
    tasks = []

    def create_task(coro):
        tasks.append(coro)

    c.hass.async_create_task.side_effect = create_task
    c._tasks = tasks
    c._command_error_event_entity = MagicMock()
    c._last_command_unreachable = False
    c._last_command_unreachable_time = None
    c._consecutive_unreachable_polls = 0
    c.client = NS(last_rejection_message="Too frequent operations")
    return c


def _run_tasks(c):
    async def run():
        for coro in list(c._tasks):
            await coro

    asyncio.run(run())
    c._tasks.clear()


def _notifications(c, service="create"):
    return [
        call.args[2]
        for call in c.hass.services.async_call.await_args_list
        if call.args[:2] == ("persistent_notification", service)
    ]


class FailureTests(unittest.TestCase):
    def test_car_couldnt_be_reached(self):
        c = _coordinator()
        c.note_command_unreachable = MagicMock()
        c.record_command_error("Error setting HVAC mode", CODE_4)
        _run_tasks(c)
        (note,) = _notifications(c)
        self.assertEqual(note["notification_id"], "mg_saic_command_unreachable_VIN1")
        self.assertEqual(note["title"], "MG SAIC: Couldn't Reach the Car")
        self.assertIn("**Setting HVAC mode** for MG MG4 (VIN: VIN1) wasn't carried out", note["message"])
        self.assertIn("return code: 4", note["message"])
        # The event and reachability, as before.
        c._command_error_event_entity.record_command_error.assert_called_once()
        c.note_command_unreachable.assert_called_once()

    def test_any_other_failure(self):
        c = _coordinator()
        c.record_command_error("service.locking_vehicle", CODE_500)
        _run_tasks(c)
        (note,) = _notifications(c)
        self.assertEqual(note["notification_id"], "mg_saic_command_failed_VIN1")
        self.assertEqual(note["title"], "MG SAIC: Command Failed")
        self.assertIn("**Locking vehicle**", note["message"])
        self.assertIn("**Reason:**", note["message"])
        c._command_error_event_entity.record_command_error.assert_called_once()

    def test_a_refusal_gets_the_rejection_notification_not_a_second_one(self):
        # e.g. an mg_saic action, or Target SOC, getting code 8.
        for error in (CODE_8, API.CommandsLimitReachedException(str(CODE_8))):
            c = _coordinator()
            c.notify_command_limit_reached = AsyncMock()
            c.record_command_error("Error setting Target SOC", error)
            _run_tasks(c)
            c.notify_command_limit_reached.assert_awaited_once_with("VIN1", "Error setting Target SOC")
            c._command_error_event_entity.record_command_error.assert_not_called()

    def test_vehicle_not_locked(self):
        c = _coordinator()
        c.notify_vehicle_not_locked = AsyncMock()
        c.record_command_error(
            "Error", API.VehicleNotLockedException("return code: 8, message: Vehicle not locked")
        )
        _run_tasks(c)
        c.notify_vehicle_not_locked.assert_awaited_once()

    def test_the_same_kind_replaces_rather_than_stacks(self):
        c = _coordinator()
        c.note_command_unreachable = MagicMock()
        for _ in range(3):
            c.record_command_error("Error setting HVAC mode", CODE_4)
        _run_tasks(c)
        ids = {n["notification_id"] for n in _notifications(c)}
        self.assertEqual(ids, {"mg_saic_command_unreachable_VIN1"})


class ClearedOnSuccessTests(unittest.TestCase):
    def test_a_later_success_clears_them(self):
        c = _coordinator()
        c.note_command_unreachable = MagicMock()
        c.record_command_error("Error setting HVAC mode", CODE_4)
        c.record_command_error("service.locking_vehicle", CODE_500)
        _run_tasks(c)
        c.note_command_ok()
        _run_tasks(c)
        dismissed = {n["notification_id"] for n in _notifications(c, "dismiss")}
        self.assertEqual(
            dismissed,
            {"mg_saic_command_unreachable_VIN1", "mg_saic_command_failed_VIN1"},
        )
        # Nothing more to dismiss next time.
        c.hass.services.async_call.reset_mock()
        c.note_command_ok()
        _run_tasks(c)
        self.assertEqual(_notifications(c, "dismiss"), [])

    def test_rejection_notifications_are_cleared_too(self):
        c = _coordinator()
        asyncio.run(c.notify_command_limit_reached("VIN1", "x"))
        asyncio.run(c.notify_vehicle_not_locked("VIN1", "x"))
        c.note_command_ok()
        _run_tasks(c)
        dismissed = {n["notification_id"] for n in _notifications(c, "dismiss")}
        self.assertEqual(
            dismissed,
            {"mg_saic_command_limit_VIN1", "mg_saic_vehicle_not_locked_VIN1"},
        )

    def test_nothing_shown_nothing_dismissed(self):
        c = _coordinator()
        c.note_command_ok()
        self.assertEqual(c._tasks, [])


class GapTests(unittest.TestCase):
    def test_target_soc(self):
        number = MODS["number"].SAICMGTargetSOCNumber
        entity = number.__new__(number)
        entity.coordinator = NS(
            after_action_delay=10,
            target_soc_long_interval=60,
            record_command_error=MagicMock(),
        )
        entity._client = NS(set_target_soc=AsyncMock(side_effect=CODE_4))
        entity._vin = "VIN1"
        asyncio.run(entity.async_set_native_value(80))
        entity.coordinator.record_command_error.assert_called_once()
        self.assertEqual(
            entity.coordinator.record_command_error.call_args.args[0], "Error setting Target SOC"
        )

    def test_charging_current_limit(self):
        from pathlib import Path

        source = Path(MODS["select"].__file__).read_text()
        self.assertIn('"Error setting Charging Current Limit", e', source)


if __name__ == "__main__":
    unittest.main()
