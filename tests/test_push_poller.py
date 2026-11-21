# File: tests/test_push_poller.py
"""The message poller's behaviour when real-time push is on.

With push on for a car, SAIC's push has normally already handled a start or a
charge end by the time the poller finds the same message in the account's
queue, so the poller must not act twice. A start found in the queue that no push
announced is the sign push isn't delivering, and the poller then still acts on
it as it always did.

Same stubbing technique as tests/test_message_poller.py.
"""

import asyncio
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone

from test_message_poller import FakeClient, FakeCoordinator, FakeMessage, _ms, _run, mp

from unittest.mock import MagicMock

# push_logic as the poller module imported it (a plain module, no HA needed)
PL = sys.modules[f"{mp.__name__.rsplit('.', 1)[0]}.push_logic"]


class PushCoordinator(FakeCoordinator):
    def __init__(self, push_enabled=True):
        super().__init__()
        self.push_enabled = push_enabled
        self.push_start_ledger = PL.PushLedger()
        self.push_charge_ledger = PL.PushLedger()


class FakeListener:
    def __init__(self, status=None):
        self.status = status or PL.PushStatus.PUSH
        self.unannounced = 0

    def note_unannounced_start(self):
        self.unannounced += 1


def _poller(coordinator, messages, listener=None, vin="LSJWX4091SN109647"):
    client = FakeClient(messages, supports_delete_all=True)
    poller = mp.SAICMGAccountPoller(
        hass=MagicMock(),
        client=client,
        account_key=("user@example.com", "EU"),
        api_lock=asyncio.Lock(),
    )
    poller.register_coordinator(vin, coordinator)
    poller._first_poll_done = True  # a running poller, not a fresh start
    if listener is not None:
        poller.set_push_listener(listener)
    return poller, client


def _start_message(message_id=1, minutes_ago=2):
    created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return FakeMessage(message_id, create_time_ms=_ms(created))


def _charge_message(message_id=2):
    created = datetime.now(timezone.utc) - timedelta(minutes=2)
    return FakeMessage(
        message_id,
        create_time_ms=_ms(created),
        message_type="328",
        title="Finish Charging",
        content="Your vehicle has finished charging",
    )


class StartAlreadyHandledByPush(unittest.TestCase):
    def test_push_start_is_not_hinted_or_refreshed_again(self):
        coordinator = PushCoordinator()
        coordinator.push_start_ledger.note_push(time.time() - 1500)
        listener = FakeListener()
        poller, client = _poller(coordinator, [_start_message()], listener)

        _run(poller._poll_once())

        self.assertEqual(coordinator.hints, [])
        self.assertEqual(coordinator.refresh_reasons, [])
        self.assertEqual(listener.unannounced, 0)

    def test_the_queue_is_still_cleared(self):
        coordinator = PushCoordinator()
        coordinator.push_start_ledger.note_push(time.time() - 1500)
        poller, client = _poller(coordinator, [_start_message()], FakeListener())

        _run(poller._poll_once())

        self.assertEqual(client.delete_all_calls, 1)

    def test_start_with_no_push_is_acted_on_and_reported(self):
        coordinator = PushCoordinator()
        listener = FakeListener()
        poller, _client = _poller(coordinator, [_start_message()], listener)

        _run(poller._poll_once())

        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertEqual(listener.unannounced, 1)

    def test_a_push_arriving_just_after_is_not_acted_on_twice(self):
        coordinator = PushCoordinator()
        poller, _client = _poller(coordinator, [_start_message()], FakeListener())

        _run(poller._poll_once())

        # the poller acted; a push for the same start now arrives
        self.assertFalse(coordinator.push_start_ledger.note_push(time.time() + 5))

    def test_car_with_push_off_behaves_as_before(self):
        coordinator = PushCoordinator(push_enabled=False)
        listener = FakeListener()
        poller, _client = _poller(coordinator, [_start_message()], listener)

        _run(poller._poll_once())

        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(listener.unannounced, 0)

    def test_no_listener_behaves_as_before(self):
        coordinator = PushCoordinator()
        poller, _client = _poller(coordinator, [_start_message()])

        _run(poller._poll_once())

        self.assertEqual(len(coordinator.hints), 1)


class ChargeFinishedHandledByPush(unittest.TestCase):
    def test_push_announced_charge_end_is_not_refreshed_again(self):
        coordinator = PushCoordinator()
        coordinator.push_charge_ledger.note_push(time.time() - 600)
        poller, _client = _poller(coordinator, [_charge_message()], FakeListener())

        _run(poller._poll_once())

        self.assertEqual(coordinator.refresh_reasons, [])

    def test_charge_end_with_no_push_still_refreshes(self):
        coordinator = PushCoordinator()
        listener = FakeListener()
        poller, _client = _poller(coordinator, [_charge_message()], listener)

        _run(poller._poll_once())

        self.assertEqual(len(coordinator.refresh_reasons), 1)
        # a missing charge push is not evidence against push: only starts count
        self.assertEqual(listener.unannounced, 0)


class PollInterval(unittest.TestCase):
    def _poller(self, listener, coordinators):
        poller = mp.SAICMGAccountPoller(
            hass=MagicMock(),
            client=FakeClient([]),
            account_key=("user@example.com", "EU"),
            api_lock=asyncio.Lock(),
        )
        for i, coordinator in enumerate(coordinators):
            poller.register_coordinator(f"VIN{i}", coordinator)
        if listener is not None:
            poller.set_push_listener(listener)
        return poller

    def test_a_minute_without_push(self):
        poller = self._poller(None, [PushCoordinator()])
        self.assertEqual(poller.current_interval(), 60)

    def test_thirty_minutes_while_push_works(self):
        poller = self._poller(FakeListener(), [PushCoordinator()])
        self.assertEqual(poller.current_interval(), 1800)

    def test_a_minute_when_push_drops(self):
        poller = self._poller(FakeListener(PL.PushStatus.POLLING), [PushCoordinator()])
        self.assertEqual(poller.current_interval(), 60)

    def test_a_minute_while_any_car_has_push_off(self):
        poller = self._poller(
            FakeListener(), [PushCoordinator(), PushCoordinator(push_enabled=False)]
        )
        self.assertEqual(poller.current_interval(), 60)

    def test_a_drop_wakes_a_long_sleep(self):
        listener = FakeListener()
        poller = self._poller(listener, [PushCoordinator()])
        poller._sleeping_for = 1800
        listener.status = PL.PushStatus.POLLING
        poller.notify_status_changed()
        self.assertTrue(poller._wake.is_set())

    def test_recovery_does_not_cut_a_short_sleep_short(self):
        listener = FakeListener(PL.PushStatus.POLLING)
        poller = self._poller(listener, [PushCoordinator()])
        poller._sleeping_for = 60
        listener.status = PL.PushStatus.PUSH
        poller.notify_status_changed()
        self.assertFalse(poller._wake.is_set())


if __name__ == "__main__":
    unittest.main()
