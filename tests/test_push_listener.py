# File: tests/test_push_listener.py
"""Tests for push_listener.py: how a push is acted on, and the status it reports.

Home Assistant and the SAIC library's push module are not needed: the listener
is driven with fake messages, coordinators and a fake hass, in plain CPython
like the rest of the suite.
"""

import asyncio
import importlib.util
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "mg_saic"
PACKAGE_NAME = "mg_saic_push_listener_under_test"


def _load():
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[PACKAGE_NAME] = package
    const = ModuleType(f"{PACKAGE_NAME}.const")
    const.LOGGER = MagicMock()
    const.EVENT_PUSH_RECEIVED = "mg_saic_push"
    sys.modules[f"{PACKAGE_NAME}.const"] = const
    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.push_listener", PKG_DIR / "push_listener.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE_NAME}.push_listener"] = module
    spec.loader.exec_module(module)
    return module


pls = _load()
PL = sys.modules[f"{PACKAGE_NAME}.push_logic"]

VIN = "LSJWX4091SN109647"


class FakeMessage:
    def __init__(self, message_type="323", vin=VIN, age=2.0, title="Vehicle Start"):
        self.message_type = message_type
        self.vin = vin
        self.title = title
        self.body = None
        self._age = age
        self.sent_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age) if age is not None else None
        )

    @property
    def is_vehicle_start(self):
        return self.message_type == "323"

    @property
    def is_charge_finished(self):
        return self.message_type == "328"

    def age_seconds(self, now=None):
        return self._age

    def as_event_data(self):
        return {"message_type": self.message_type, "vin": self.vin, "title": self.title}


class FakeCoordinator:
    def __init__(self):
        self.push_start_ledger = PL.PushLedger()
        self.push_charge_ledger = PL.PushLedger()
        self.started = []
        self.refreshes = []
        self.recorded = []
        self.listener_updates = 0

    async def async_handle_vehicle_start(self, started_at):
        self.started.append(started_at)

    async def async_trigger_refresh(self, reason):
        self.refreshes.append(reason)

    def record_push(self, message, *, stale=False):
        self.recorded.append((message.message_type, stale))

    def async_update_listeners(self):
        self.listener_updates += 1


class FakeBus:
    def __init__(self):
        self.events = []

    def async_fire(self, name, data):
        self.events.append((name, data))


class FakeHass:
    def __init__(self):
        self.bus = FakeBus()

    def async_create_background_task(self, coro, name):
        return asyncio.get_running_loop().create_task(coro)


class FakePush:
    def __init__(self, connected=False):
        self.connected = connected
        self.persistent_ids = []
        self.credentials = None  # nothing to save


def _listener(coordinator=None):
    hass = FakeHass()
    listener = pls.PushListener(hass, ("user@example.com", "EU"), asyncio.Lock())
    coordinator = coordinator or FakeCoordinator()
    listener.register_coordinator(VIN, coordinator)
    listener._push = FakePush()
    return listener, hass, coordinator


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class HandlingPushes(unittest.IsolatedAsyncioTestCase):
    async def test_a_start_push_hints_and_refreshes_the_car(self):
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("323"))
        await _settle()

        self.assertEqual(len(car.started), 1)
        self.assertEqual(hass.bus.events[0][0], "mg_saic_push")
        self.assertEqual(hass.bus.events[0][1]["action"], "refresh")
        self.assertFalse(hass.bus.events[0][1]["stale"])
        self.assertEqual(car.recorded, [("323", False)])

    async def test_the_start_time_is_not_in_the_future(self):
        listener, _hass, car = _listener()
        await listener._on_message(FakeMessage("323", age=-30.0))
        await _settle()
        self.assertLessEqual(car.started[0], datetime.now(timezone.utc))

    async def test_a_stale_push_fires_the_event_but_does_nothing_else(self):
        # SAIC queues pushes for 28 days; after downtime old ones replay.
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("323", age=3 * 86400))
        await _settle()

        self.assertEqual(car.started, [])
        self.assertEqual(hass.bus.events[0][1]["action"], "stale")
        self.assertTrue(hass.bus.events[0][1]["stale"])
        self.assertEqual(car.recorded, [("323", True)])

    async def test_the_same_start_delivered_twice_is_acted_on_once(self):
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("323"))
        await listener._on_message(FakeMessage("323"))
        await _settle()

        self.assertEqual(len(car.started), 1)
        self.assertEqual(hass.bus.events[1][1]["action"], "already_handled")

    async def test_a_charge_finished_push_refreshes_the_car(self):
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("328", title="Finish Charging"))
        await _settle()

        self.assertEqual(car.refreshes, ["charging finished (push)"])
        self.assertEqual(car.started, [])
        self.assertEqual(hass.bus.events[0][1]["message_type"], "328")

    async def test_other_message_types_only_fire_the_event(self):
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("999", title="Something else"))
        await _settle()

        self.assertEqual(car.started, [])
        self.assertEqual(car.refreshes, [])
        self.assertEqual(len(hass.bus.events), 1)

    async def test_a_car_not_using_push_is_ignored(self):
        listener, hass, car = _listener()
        await listener._on_message(FakeMessage("323", vin="LSJOTHERCAR000000"))
        await _settle()

        self.assertEqual(car.started, [])
        self.assertEqual(hass.bus.events, [])

    async def test_a_message_with_no_vin_goes_to_the_only_car(self):
        listener, _hass, car = _listener()
        await listener._on_message(FakeMessage("323", vin=None))
        await _settle()
        self.assertEqual(len(car.started), 1)

    async def test_a_failing_handler_does_not_break_the_connection(self):
        listener, _hass, car = _listener()

        async def boom(_started_at):
            raise RuntimeError("refresh failed")

        car.async_handle_vehicle_start = boom
        await listener._on_message(FakeMessage("323"))
        await _settle()  # must not raise

    async def test_a_fresh_start_push_clears_a_missed_push_suspicion(self):
        listener, _hass, _car = _listener()
        listener.health.suspect_miss(time.time(), 0.0)
        await listener._on_message(FakeMessage("323"))
        self.assertIsNone(listener.health.seconds_until_verdict(time.time()))


class ReportedStatus(unittest.IsolatedAsyncioTestCase):
    async def test_connecting_then_push(self):
        listener, _hass, car = _listener()
        self.assertIs(listener.status, PL.PushStatus.CONNECTING)

        listener._push.connected = True
        listener._on_connection_state(True)
        self.assertIs(listener.status, PL.PushStatus.PUSH)
        self.assertIsNone(listener.reason)
        self.assertGreaterEqual(car.listener_updates, 1)

    async def test_a_long_disconnect_means_polling(self):
        listener, _hass, _car = _listener()
        listener._disconnected_since = time.time() - PL.CONNECTING_GRACE_SECONDS - 5
        self.assertIs(listener.status, PL.PushStatus.POLLING)
        self.assertIs(listener.reason, PL.PushReason.DISCONNECTED)

    async def test_a_missed_push_means_polling_while_connected(self):
        listener, _hass, _car = _listener()
        listener._push.connected = True
        listener._on_connection_state(True)
        listener.health.suspect_miss(time.time() - 1000, 0.0)
        listener.health.confirm(time.time())
        self.assertIs(listener.status, PL.PushStatus.POLLING)
        self.assertIs(listener.reason, PL.PushReason.MISSED_PUSH)

    async def test_no_registration_means_polling_with_the_reason(self):
        listener = pls.PushListener(FakeHass(), ("u", "EU"), asyncio.Lock())
        listener._failure = PL.PushReason.NO_LIBRARY
        self.assertIs(listener.status, PL.PushStatus.POLLING)
        self.assertIs(listener.reason, PL.PushReason.NO_LIBRARY)

    async def test_a_status_change_wakes_the_poller_and_the_sensors(self):
        listener, _hass, car = _listener()
        poller = MagicMock()
        listener.attach(object(), poller)
        listener._push.connected = True
        listener._on_connection_state(True)
        poller.notify_status_changed.assert_called()
        self.assertGreaterEqual(car.listener_updates, 1)

    async def test_a_start_the_poller_found_during_a_fresh_reconnect_is_not_blamed(self):
        listener, _hass, _car = _listener()
        listener._push.connected = True
        listener._on_connection_state(True)  # connected just now
        listener.note_unannounced_start()
        self.assertIsNone(listener.health.seconds_until_verdict(time.time()))


class Maintenance(unittest.IsolatedAsyncioTestCase):
    async def test_a_confirmed_miss_renews_the_registration(self):
        listener, _hass, _car = _listener()
        listener._push.connected = True
        renewed = []

        async def fake_renew(why):
            renewed.append(why)

        listener.async_renew = fake_renew
        listener.health.suspect_miss(time.time() - 1000, 0.0)
        listener.health.suspected_at = time.time() - 1000
        listener._registered_at = time.time()
        await listener._maintain()
        await _settle()

        self.assertEqual(renewed, ["a start was missed"])

    async def test_an_old_registration_is_renewed(self):
        listener, _hass, _car = _listener()
        renewed = []

        async def fake_renew(why):
            renewed.append(why)

        listener.async_renew = fake_renew
        listener._registered_at = time.time() - PL.RENEW_AFTER_SECONDS - 10
        await listener._maintain()

        self.assertEqual(renewed, ["registration is a day old"])

    async def test_a_young_registration_is_left_alone(self):
        listener, _hass, _car = _listener()
        renewed = []

        async def fake_renew(why):
            renewed.append(why)

        listener.async_renew = fake_renew
        listener._registered_at = time.time() - 3600
        await listener._maintain()

        self.assertEqual(renewed, [])


if __name__ == "__main__":
    unittest.main()
