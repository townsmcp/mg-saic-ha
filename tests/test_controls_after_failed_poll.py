"""The car's controls after one failed poll.

James's MGS6, 15 Oct 2026 (BST): switched off after a drive at 08:02:40;
polls at 08:03 and 08:06 worked; the next, 08:10:37-08:14:16, got code 4 on
all five status tries (the car had just dropped off to sleep). The lock,
climate, climate mode, air conditioning, heated seats and steering wheel went
unavailable until the next poll, due at 08:44, while Reachability still said
"awake". A manual refresh at 08:15:36 worked first time.

Now the controls keep the last status the car gave and stay usable, until
the car is flagged unreachable (two failed polls in a row, #238).
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from test_charging_controls_dropout import MODS, _stub
import test_setup_and_config_flow

LOGIC = sys.modules["mg_saic.logic"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"


class _Entity:
    def __init__(self, *args, **kwargs):
        pass


_stub("homeassistant.components.lock", LockEntity=_Entity)
LOCK = test_setup_and_config_flow._load("mg_saic.lock", PKG_DIR / "lock.py", force=True)


def _status_0806():
    # Parked, locked, nothing on.
    return NS(basicVehicleStatus=NS(
        lockStatus=1, frontLeftSeatHeatLevel=0, remoteClimateStatus=0,
        interiorTemperature=23,
    ))


def _coordinator(*, status=None, last=None, unreachable=False):
    return NS(
        data={"status": status, "charging": None},
        last_status_data=last,
        car_unreachable=unreachable,
        last_update_success=True,
    )


def _lock(coordinator):
    entity = LOCK.SAICMGLockEntity.__new__(LOCK.SAICMGLockEntity)
    entity.coordinator = coordinator
    return entity


def _seat(coordinator):
    cls = MODS["switch"].SAICMGHeatedSeatSwitch
    entity = cls.__new__(cls)
    entity.coordinator = coordinator
    entity._status_attr = "frontLeftSeatHeatLevel"
    return entity


class HelperTests(unittest.TestCase):
    def test_latest_status_first(self):
        latest, older = _status_0806(), _status_0806()
        c = _coordinator(status=latest, last=older)
        self.assertIs(LOGIC.status_for_controls(c), latest)

    def test_last_status_after_a_failed_poll(self):
        older = _status_0806()
        self.assertIs(LOGIC.status_for_controls(_coordinator(last=older)), older)

    def test_nothing_once_the_car_is_unreachable(self):
        c = _coordinator(last=_status_0806(), unreachable=True)
        self.assertIsNone(LOGIC.status_for_controls(c))

    def test_nothing_without_a_reachability_answer(self):
        c = NS(data={"status": None}, last_status_data=_status_0806())
        self.assertIsNone(LOGIC.status_for_controls(c))

    def test_nothing_ever(self):
        self.assertIsNone(LOGIC.status_for_controls(_coordinator()))
        self.assertIsNone(LOGIC.status_for_controls(NS(data=None)))


class The0814PollTests(unittest.TestCase):
    def setUp(self):
        self.c = _coordinator(last=_status_0806())

    def test_lock_stays_usable_and_locked(self):
        lock = _lock(self.c)
        self.assertTrue(lock.available)
        self.assertTrue(lock.is_locked)

    def test_heated_seat_stays_usable_and_off(self):
        seat = _seat(self.c)
        self.assertTrue(seat.available)
        self.assertFalse(seat.is_on)

    def test_after_several_failed_polls_they_go_unavailable(self):
        self.c.car_unreachable = True
        self.assertFalse(_lock(self.c).available)
        self.assertFalse(_seat(self.c).available)

    def test_the_next_good_poll_shows_the_new_status(self):
        self.c.data["status"] = NS(basicVehicleStatus=NS(lockStatus=0))
        self.assertFalse(_lock(self.c).is_locked)

    def test_never_any_status_is_still_unavailable(self):
        c = _coordinator()
        self.assertFalse(_lock(c).available)
        self.assertFalse(_seat(c).available)


class CoordinatorTests(unittest.TestCase):
    def test_unreachable_flag(self):
        c = COORD_CLS.__new__(COORD_CLS)
        self.assertFalse(c.car_unreachable)
        c._last_command_unreachable = True
        self.assertTrue(c.car_unreachable)

    def test_update_state_remembers_the_status(self):
        source = (PKG_DIR / "coordinator.py").read_text()
        block = source.split("def _update_state(self, data):", 1)[1][:600]
        self.assertIn("self.last_status_data = status_data", block)

    def test_platforms_read_it_through_the_helper(self):
        for name in ("lock.py", "climate.py", "switch.py", "select.py"):
            source = (PKG_DIR / name).read_text()
            self.assertNotIn('self.coordinator.data.get("status")', source, name)


if __name__ == "__main__":
    unittest.main()
