"""Lock / unlock: check the car before reporting a failure (#488, @HarryFlatter).

HS PHEV (AS33P), 17 Nov 2026: an unlock from Home Assistant got return code 4
("The remote control instruction failed, please try again later.") for 15 s,
then return code 8 "Please confirm that the vehicle status or the overall
vehicle network status is normal.(253)" for another 15 s. A "command
rejected" notification fired, but the car had unlocked: he walked out, got in
and drove off. SAIC's confirmation never arrived.

Now a code 4 or code 8 is followed by one status read, straight away, and the
failure is reported only if the car's lock state isn't what was asked for.
"""

import asyncio
import sys
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_controls_after_failed_poll import LOCK

LOGIC = sys.modules["mg_saic.logic"]

HARRY_253 = (
    "Please confirm that the vehicle status or the overall vehicle network "
    "status is normal.(253)"
)


class SaicError(Exception):
    """Shaped like the library's error: a return code and SAIC's message."""

    def __init__(self, code, message):
        super().__init__(f"return code: {code}, message: {message}")
        self.return_code = code
        self.saic_message = message


def _rejected(message=HARRY_253):
    """What api.py raises for a code 8 (as in Harry's log)."""
    try:
        raise SaicError(8, message)
    except SaicError as e:
        try:
            raise LOCK.CommandsLimitReachedException(str(e)) from e
        except LOCK.CommandsLimitReachedException as wrapped:
            return wrapped


def _coordinator(lock_after):
    """``lock_after``: the car's lockStatus on the check (None = no reading,
    "fail" = the status read fails)."""
    c = NS(
        data={"status": NS(basicVehicleStatus=NS(lockStatus=1)), "charging": None},
        last_update_success=True,
        after_action_delay=timedelta(seconds=30),
        lock_unlock_long_interval=timedelta(minutes=5),
        notify_command_limit_reached=AsyncMock(),
        notify_vehicle_not_locked=AsyncMock(),
        record_command_error=MagicMock(),
        note_command_ok=MagicMock(),
        schedule_action_refresh=AsyncMock(),
    )

    async def refresh():
        if lock_after == "fail":
            c.last_update_success = False
            return
        basic = NS() if lock_after is None else NS(lockStatus=lock_after)
        c.data = {"status": NS(basicVehicleStatus=basic), "charging": None}

    c.async_refresh = AsyncMock(side_effect=refresh)
    return c


def _entity(coordinator, *, error=None):
    entity = LOCK.SAICMGLockEntity.__new__(LOCK.SAICMGLockEntity)
    entity.coordinator = coordinator
    entity._vin = "VIN1"
    entity._client = NS(
        unlock_vehicle=AsyncMock(side_effect=error),
        lock_vehicle=AsyncMock(side_effect=error),
    )
    entity.async_write_ha_state = MagicMock()
    return entity


def _run(coro):
    return asyncio.run(coro)


class HarrysUnlockTests(unittest.TestCase):
    def test_car_unlocked_anyway_means_no_notification(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=_rejected()).async_unlock())
        c.async_refresh.assert_awaited_once()
        c.notify_command_limit_reached.assert_not_awaited()
        c.record_command_error.assert_not_called()
        c.note_command_ok.assert_called_once()

    def test_car_still_locked_is_reported_as_before(self):
        c = _coordinator(lock_after=1)
        _run(_entity(c, error=_rejected()).async_unlock())
        c.async_refresh.assert_awaited_once()
        c.notify_command_limit_reached.assert_awaited_once_with("VIN1")
        c.note_command_ok.assert_not_called()

    def test_no_follow_up_refreshes_once_confirmed(self):
        # The check was the refresh; don't poll the car again straight after.
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=_rejected()).async_unlock())
        c.schedule_action_refresh.assert_not_awaited()


class LockTests(unittest.TestCase):
    def test_lock_confirmed_by_the_car(self):
        c = _coordinator(lock_after=1)
        _run(_entity(c, error=_rejected()).async_lock())
        c.notify_command_limit_reached.assert_not_awaited()
        c.note_command_ok.assert_called_once()

    def test_lock_not_done(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=_rejected()).async_lock())
        c.notify_command_limit_reached.assert_awaited_once()


class Code4Tests(unittest.TestCase):
    """The library can also give up on code 4 ("instruction failed")."""

    def _code4(self):
        return SaicError(4, "The remote control instruction failed, please try again later.")

    def test_code_4_but_the_car_did_it(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=self._code4()).async_unlock())
        c.async_refresh.assert_awaited_once()
        c.record_command_error.assert_not_called()

    def test_code_4_and_the_car_did_not(self):
        c = _coordinator(lock_after=1)
        _run(_entity(c, error=self._code4()).async_unlock())
        c.record_command_error.assert_called_once()
        self.assertEqual(c.record_command_error.call_args[0][0], "Error unlocking vehicle")


class CannotTellTests(unittest.TestCase):
    """When the check can't say, report the failure as before."""

    def test_status_read_fails(self):
        c = _coordinator(lock_after="fail")
        _run(_entity(c, error=_rejected()).async_unlock())
        c.notify_command_limit_reached.assert_awaited_once()

    def test_no_lock_state_in_the_reply(self):
        c = _coordinator(lock_after=None)
        _run(_entity(c, error=_rejected()).async_unlock())
        c.notify_command_limit_reached.assert_awaited_once()


class NotCheckedTests(unittest.TestCase):
    def test_other_failures_are_reported_without_a_check(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=RuntimeError("connection reset")).async_unlock())
        c.async_refresh.assert_not_awaited()
        c.record_command_error.assert_called_once()

    def test_vehicle_not_locked_keeps_its_own_notification(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c, error=LOCK.VehicleNotLockedException("not locked")).async_lock())
        c.async_refresh.assert_not_awaited()
        c.notify_vehicle_not_locked.assert_awaited_once_with("VIN1")

    def test_success_is_unchanged(self):
        c = _coordinator(lock_after=0)
        _run(_entity(c).async_unlock())
        c.async_refresh.assert_not_awaited()
        c.schedule_action_refresh.assert_awaited_once_with(
            "VIN1", timedelta(seconds=30), timedelta(minutes=5)
        )
        c.note_command_ok.assert_not_called()  # schedule_action_refresh does it


class NotificationWordingTests(unittest.TestCase):
    def test_253_says_the_car_may_have_done_it(self):
        advice = LOGIC.command_rejection_advice(HARRY_253)
        self.assertIn("didn't hear back from the car", advice)
        self.assertIn("Check the car", advice)

    def test_253_is_not_the_command_limit(self):
        self.assertFalse(LOGIC.command_rejection_is_limit(HARRY_253))

    def test_other_rejections_unchanged(self):
        self.assertIn(
            "Try again in a minute",
            LOGIC.command_rejection_advice("Request failed. Please check the vehicle status and try again.(8)"),
        )


if __name__ == "__main__":
    unittest.main()
