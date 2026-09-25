# File: tests/test_message_poller.py
"""Unit tests for SAICMGAccountPoller's first-poll message classification.

Regression coverage for the bug where a message arriving after the account's
queue had been empty since the poller started up was discarded, unhinted and
undeleted, purely for being "the first message this poller instance has
seen" — even when it was a genuinely fresh, real-time event. Root-caused via
a live incident: the poller had run since the previous night with an empty
queue, and the next morning's vehicle-start message (the first message it
ever saw) was silently swallowed, delaying/mis-timestamping the resulting
sensor state by ~15 minutes.

Uses the same stubbing technique as tests/test_setup.py — Home Assistant
and third-party modules are stubbed so message_poller.py loads in plain
CPython, matching python-tests.yaml CI (no homeassistant/aiohttp install).
"""

import asyncio
import importlib.util
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE_NAME = "mg_saic_message_poller_under_test"


def _module(name, **attributes):
    """Register a small module stub with the supplied attributes."""
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_message_poller():
    """Load message_poller.py with only its `.const` import stubbed."""
    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[PACKAGE_NAME] = package

    _module(f"{PACKAGE_NAME}.const", LOGGER=MagicMock())

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE_NAME}.message_poller", PKG_DIR / "message_poller.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{PACKAGE_NAME}.message_poller"] = module
    spec.loader.exec_module(module)
    return module


mp = _load_message_poller()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Fakes ─────────────────────────────────────────────────────────────────


class FakeMessage:
    """Stand-in for saic_ismart_client_ng's MessageEntity."""

    def __init__(
        self,
        message_id,
        create_time_ms=None,
        message_type="323",
        title="Vehicle Start",
        content="",
        vin="LSJWX4091SN109647",
        message_time=None,
    ):
        self.messageId = message_id
        self.createTime = create_time_ms
        self.messageType = message_type
        self.title = title
        self.content = content
        self.vin = vin
        # Mirrors the library: the raw messageTime string plus the parsed
        # message_time. (The real property never returns None -- it falls
        # back to now() -- which is why the poller checks the raw string.)
        self.messageTime = (
            message_time.strftime("%Y-%m-%d %H:%M:%S") if message_time else None
        )
        self.message_time = message_time or datetime.now()


class FakeResponse:
    def __init__(self, messages):
        self.messages = messages


class FakeClient:
    """Serves one message on page 1, empty thereafter — a single-item queue.
    Without supports_delete_all it behaves like the old API wrapper (no
    delete_all_alarms), exercising the per-message fallback."""

    def __init__(self, messages, supports_delete_all=False):
        self._messages = list(messages)
        self.deleted_ids = []
        self.delete_all_calls = 0
        if supports_delete_all:
            self.delete_all_alarms = self._delete_all_alarms

    async def _delete_all_alarms(self):
        self.delete_all_calls += 1
        self._messages = []
        return True

    async def get_alarm_messages(self, page_num, page_size):
        if page_num == 1 and self._messages:
            return FakeResponse([self._messages[0]])
        return None  # as the real API: an empty queue is code 0 with no data

    async def delete_message(self, message_id):
        self.deleted_ids.append(message_id)

    async def login(self):
        pass


class FakeCoordinator:
    def __init__(self):
        self.hints = []
        self.refresh_reasons = []

    def hint_vehicle_started(self, started_at):
        self.hints.append(started_at)

    async def async_trigger_refresh(self, reason):
        self.refresh_reasons.append(reason)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _make_poller(client, vin="LSJWX4091SN109647", coordinator=None):
    poller = mp.SAICMGAccountPoller(
        hass=MagicMock(),
        client=client,
        account_key=("user@example.com", "EU"),
        api_lock=asyncio.Lock(),
    )
    poller.register_coordinator(vin, coordinator or FakeCoordinator())
    return poller


# ── Tests ────────────────────────────────────────────────────────────────


class TestMessageCreateTimeHelper(unittest.TestCase):
    def test_parses_valid_ms_timestamp(self):
        dt = datetime(2026, 8, 25, 6, 18, 44, tzinfo=timezone.utc)
        msg = FakeMessage(1, create_time_ms=_ms(dt))
        self.assertEqual(mp._message_create_time(msg), dt)

    def test_missing_create_time_returns_none(self):
        msg = FakeMessage(1, create_time_ms=None)
        self.assertIsNone(mp._message_create_time(msg))

    def test_non_numeric_create_time_returns_none_not_raise(self):
        msg = FakeMessage(1, create_time_ms="not-a-number")
        self.assertIsNone(mp._message_create_time(msg))


class TestFirstPollClassification(unittest.TestCase):
    """Core regression coverage for the swallow bug.

    Each test manually backdates `poller._started_at` to simulate "the
    poller has been running for a while with an empty queue" before a
    message shows up — mirroring the incident (poller up since the
    previous night, queue empty all night, then one message arrives).
    """

    def test_fresh_message_after_startup_is_processed_not_swallowed(self):
        """The actual incident, replayed: message created after poller
        startup must be hinted, trigger a refresh, and be deleted — even
        though it is the very first message this poller instance has seen.

        Timestamps are relative to "now" rather than fixed dates: the hint
        path separately treats a message's createTime as implausible if
        it's more than ~6h old relative to real wall-clock time, so a
        hardcoded historical date here would start failing a few hours
        after being written — independent of the classification logic
        this test is actually meant to cover.
        """
        now = datetime.now(timezone.utc)
        started_at = now - timedelta(hours=9, minutes=30)
        # createTime is a millisecond-precision integer on the real API, so
        # round-trip through the same truncation _message_create_time uses —
        # otherwise this assertion is comparing microsecond-precision values
        # against a value that's already lost sub-millisecond precision.
        message_created = (now - timedelta(minutes=5)).replace(microsecond=0)

        msg = FakeMessage(256747680, create_time_ms=_ms(message_created))
        client = FakeClient([msg])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = started_at

        self.assertFalse(poller._first_poll_done)
        _run(poller._poll_once())

        self.assertTrue(poller._first_poll_done)
        self.assertEqual(coordinator.hints, [message_created])
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertIn(256747680, client.deleted_ids)
        # Watermark must still advance so we never re-fetch this page.
        self.assertEqual(poller._last_seen_message_id, 256747680)

    def test_genuinely_stale_message_is_suppressed_but_still_cleaned_up(self):
        """A message that really does predate the poller must not trigger
        a hint/refresh (the original, correct intent of the first-poll
        guard) — but should now also be deleted rather than left to rot.
        """
        started_at = datetime.now(timezone.utc) - timedelta(hours=9, minutes=30)
        message_created = started_at - timedelta(days=3)

        msg = FakeMessage(111, create_time_ms=_ms(message_created))
        client = FakeClient([msg])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = started_at

        _run(poller._poll_once())

        self.assertTrue(poller._first_poll_done)
        self.assertEqual(coordinator.hints, [])
        self.assertEqual(coordinator.refresh_reasons, [])
        self.assertIn(111, client.deleted_ids)
        self.assertEqual(poller._last_seen_message_id, 111)

    def test_undated_message_after_an_empty_start_is_processed(self):
        """The overnight incident, replayed faithfully: the poller starts
        with an EMPTY queue, polls it (empty) overnight, then an undated
        message arrives. It must be processed as live.

        This used to be covered by treating every undated first-poll
        message as fresh, because an empty first poll never counted as a
        first poll -- so the next message, hours later, was still judged
        as backlog. An empty first read now proves there's no backlog.
        """
        client = FakeClient([])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = datetime.now(timezone.utc) - timedelta(hours=9)

        _run(poller._poll_once())  # empty queue at start-up
        self.assertTrue(poller._first_poll_done)

        client._messages = [FakeMessage(222, create_time_ms=None)]
        _run(poller._poll_once())

        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertIn(222, client.deleted_ids)

    def test_undated_message_already_queued_at_start_up_is_backlog(self):
        """The restart replay: with no saved bookmark, an undated "Vehicle
        Start" already in the queue on the first read is backlog -- no
        hint, no refresh -- and, being undated, isn't deleted either."""
        msg = FakeMessage(272337591, create_time_ms=None)
        client = FakeClient([msg])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)

        _run(poller._poll_once())

        self.assertTrue(poller._first_poll_done)
        self.assertEqual(coordinator.hints, [])
        self.assertEqual(coordinator.refresh_reasons, [])
        self.assertEqual(client.deleted_ids, [])
        self.assertEqual(poller._last_seen_message_id, 272337591)

    def test_harrys_morning_empty_queue_then_undated_start(self):
        """Regression (1.3.0-beta2): the real API answers an empty queue
        with no data, so the response is None -- which the poller took for a
        failed read. 13 hours of empty polls never counted as the first
        poll, and the 13:37 "Vehicle Start" was discarded as backlog."""
        client = FakeClient([])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = datetime.now(timezone.utc) - timedelta(hours=13)
        for _ in range(3):
            _run(poller._poll_once())  # empty queue: response None
        self.assertTrue(poller._first_poll_done)

        client._messages = [FakeMessage(275579510, create_time_ms=None)]
        _run(poller._poll_once())
        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(len(coordinator.refresh_reasons), 1)

    def test_failed_first_fetch_does_not_count_as_an_empty_queue(self):
        """A failed fetch and an empty queue both collect nothing, but only
        an empty queue proves there's no backlog."""
        class FailingClient(FakeClient):
            async def get_alarm_messages(self, page_num, page_size):
                raise RuntimeError("return code: 4")

        poller = _make_poller(FailingClient([]))
        _run(poller._poll_once())
        self.assertFalse(poller._first_poll_done)

    def test_second_poll_processes_normally_regardless_of_classification(self):
        """The historical/fresh split only applies to the first poll. Once
        a stale message has been discarded and the watermark advanced, the
        very next poll must behave exactly as it always has.
        """
        started_at = datetime.now(timezone.utc) - timedelta(hours=9, minutes=30)
        stale = FakeMessage(1, create_time_ms=_ms(started_at - timedelta(days=1)))
        client = FakeClient([stale])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = started_at

        _run(poller._poll_once())
        self.assertTrue(poller._first_poll_done)
        self.assertEqual(coordinator.hints, [])

        # A second, later message arrives — even though it postdates the
        # poller's startup, this is no longer the "first poll" path; it
        # must still be processed via the normal (non-first-poll) route.
        later = FakeMessage(2, create_time_ms=_ms(started_at + timedelta(hours=1)))
        client._messages = [later]
        _run(poller._poll_once())

        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertIn(2, client.deleted_ids)



# ── Persisted bookmark ───────────────────────────────────────────────────


class FakeStore:
    def __init__(self, data=None, fail_load=False):
        self.data = data
        self.saved = []
        self.fail_load = fail_load

    async def async_load(self):
        if self.fail_load:
            raise RuntimeError("storage unavailable")
        return self.data

    async def async_save(self, data):
        self.saved.append(data)
        self.data = data


def _poller_with_store(client, store, coordinator=None):
    poller = _make_poller(client, coordinator=coordinator)
    poller._create_bookmark_store = lambda: store
    return poller


class TestPersistedBookmark(unittest.TestCase):
    """A restarted poller carries on where the previous one stopped, instead
    of walking back through the queue and replaying old events as live
    ones -- e.g. an undated "Vehicle Start" from the last drive overwriting
    Last Powered On and triggering refreshes after an HA restart."""

    T_BOOKMARK = datetime(2026, 9, 23, 16, 38, 0)

    def test_restored_bookmark_means_no_first_poll(self):
        store = FakeStore({"message_id": 500, "message_time": self.T_BOOKMARK.isoformat()})
        poller = _poller_with_store(FakeClient([]), store)
        _run(poller._async_load_bookmark())
        self.assertTrue(poller._first_poll_done)
        self.assertEqual(poller._last_seen_message_id, 500)
        self.assertEqual(poller._last_seen_message_ts, self.T_BOOKMARK)

    def test_bookmarked_message_still_queued_is_not_replayed(self):
        store = FakeStore({"message_id": 500, "message_time": self.T_BOOKMARK.isoformat()})
        client = FakeClient([FakeMessage(500, message_time=self.T_BOOKMARK)])
        coordinator = FakeCoordinator()
        poller = _poller_with_store(client, store, coordinator)
        _run(poller._async_load_bookmark())
        _run(poller._poll_once())
        self.assertEqual(coordinator.hints, [])
        self.assertEqual(coordinator.refresh_reasons, [])

    def test_older_message_is_not_replayed_even_if_bookmark_was_deleted(self):
        """The bookmarked message may be gone from the queue; the saved
        messageTime still stops the walk-back at anything older."""
        store = FakeStore({"message_id": 500, "message_time": self.T_BOOKMARK.isoformat()})
        older = FakeMessage(400, message_time=self.T_BOOKMARK - timedelta(days=2))
        coordinator = FakeCoordinator()
        poller = _poller_with_store(FakeClient([older]), store, coordinator)
        _run(poller._async_load_bookmark())
        _run(poller._poll_once())
        self.assertEqual(coordinator.refresh_reasons, [])

    def test_new_undated_message_after_restart_is_processed_live(self):
        """With a bookmark, a genuinely new message -- even with no
        createTime, as on EU accounts -- is handled exactly as on any
        normal poll: hinted, refreshed and deleted."""
        store = FakeStore({"message_id": 500, "message_time": self.T_BOOKMARK.isoformat()})
        new = FakeMessage(600, create_time_ms=None,
                          message_time=self.T_BOOKMARK + timedelta(hours=2))
        client = FakeClient([new])
        coordinator = FakeCoordinator()
        poller = _poller_with_store(client, store, coordinator)
        _run(poller._async_load_bookmark())
        _run(poller._poll_once())
        self.assertEqual(len(coordinator.hints), 1)
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertIn(600, client.deleted_ids)

    def test_bookmark_is_saved_when_it_advances(self):
        store = FakeStore()
        t = datetime(2026, 9, 23, 20, 0, 0)
        poller = _poller_with_store(FakeClient([FakeMessage(700, message_time=t)]), store)
        _run(poller._async_load_bookmark())
        _run(poller._poll_once())
        self.assertEqual(store.saved[-1], {"message_id": 700, "message_time": t.isoformat()})

    def test_missing_message_time_saves_the_id_only(self):
        """The library substitutes now() for a missing messageTime; that
        must never be saved as if it were SAIC's timestamp."""
        store = FakeStore()
        poller = _poller_with_store(FakeClient([FakeMessage(701)]), store)
        _run(poller._async_load_bookmark())
        _run(poller._poll_once())
        self.assertEqual(store.saved[-1], {"message_id": 701, "message_time": None})

    def test_nothing_saved_yet_behaves_as_a_fresh_install(self):
        poller = _poller_with_store(FakeClient([]), FakeStore(None))
        _run(poller._async_load_bookmark())
        self.assertFalse(poller._first_poll_done)

    def test_storage_failure_is_harmless(self):
        poller = _poller_with_store(FakeClient([]), FakeStore(fail_load=True))
        _run(poller._async_load_bookmark())
        self.assertFalse(poller._first_poll_done)
        self.assertIsNone(poller._bookmark_store)
        _run(poller._async_save_bookmark())  # must not raise

    def test_store_key_does_not_contain_the_username(self):
        captured = {}

        class _Store:
            def __init__(self, hass, version, key):
                captured["key"] = key

        storage = _module("homeassistant.helpers.storage", Store=_Store)
        try:
            _make_poller(FakeClient([]))._create_bookmark_store()
        finally:
            sys.modules.pop(storage.__name__, None)
        self.assertTrue(captured["key"].startswith("mg_saic_message_bookmark_"))
        self.assertNotIn("user@example.com", captured["key"])



# ── Clearing the queue after a genuine start ─────────────────────────────


class QueueClient(FakeClient):
    """A real multi-message queue, newest first, paged one at a time."""

    def __init__(self, messages, **kwargs):
        super().__init__(messages, **kwargs)
        self.on_recheck = None  # hook: runs just before the pre-clear re-read
        self._pages_served = 0

    async def get_alarm_messages(self, page_num, page_size):
        self._pages_served += 1
        if self.on_recheck and page_num == 1 and self._pages_served > 1:
            hook, self.on_recheck = self.on_recheck, None
            hook(self)
        if page_num <= len(self._messages):
            return FakeResponse([self._messages[page_num - 1]])
        return None  # as the real API: an empty queue is code 0 with no data


def _live_poller(client, coordinator=None):
    """A poller past its first poll (as after a restored bookmark)."""
    poller = _make_poller(client, coordinator=coordinator)
    poller._first_poll_done = True
    poller._last_seen_message_id = 1
    return poller


def _stale(i, title="Vehicle shutdown"):
    return FakeMessage(i, message_type="999", title=title)


class TestClearQueueAfterStart(unittest.TestCase):
    """Only start messages were ever deleted, so every other alarm stayed in
    the SAIC queue indefinitely. A genuine start now clears the lot."""

    def test_genuine_start_clears_the_whole_queue_in_one_request(self):
        start = FakeMessage(10, create_time_ms=None)
        client = QueueClient([start, _stale(9), _stale(8, "Geofence alarm")],
                             supports_delete_all=True)
        coordinator = FakeCoordinator()
        poller = _live_poller(client, coordinator)
        _run(poller._poll_once())
        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertEqual(client.delete_all_calls, 1)
        self.assertEqual(client.deleted_ids, [], "no per-message deletes needed")
        self.assertEqual(client._messages, [])

    def test_message_arriving_during_the_refresh_is_not_wiped_unseen(self):
        start = FakeMessage(10, create_time_ms=None)
        client = QueueClient([start, _stale(9)], supports_delete_all=True)
        client.on_recheck = lambda c: c._messages.insert(0, _stale(11))
        poller = _live_poller(client)
        _run(poller._poll_once())
        self.assertEqual(client.delete_all_calls, 0)
        self.assertEqual(client.deleted_ids, [10], "start still cleaned up")
        self.assertEqual(client._messages[0].messageId, 11, "new message kept")

    def test_failed_clear_falls_back_to_deleting_the_start(self):
        client = QueueClient([FakeMessage(10), _stale(9)], supports_delete_all=True)

        async def _fails():
            client.delete_all_calls += 1
            return False

        client.delete_all_alarms = _fails
        _run(_live_poller(client)._poll_once())
        self.assertEqual(client.delete_all_calls, 1)
        self.assertEqual(client.deleted_ids, [10])

    def test_failed_recheck_does_not_clear(self):
        client = QueueClient([FakeMessage(10)], supports_delete_all=True)

        def _break(c):
            raise RuntimeError("return code: 4")  # the re-read itself fails

        client.on_recheck = _break
        _run(_live_poller(client)._poll_once())
        self.assertEqual(client.delete_all_calls, 0)
        self.assertEqual(client.deleted_ids, [10])

    def test_non_start_messages_alone_do_not_clear_anything(self):
        client = QueueClient([_stale(9)], supports_delete_all=True)
        _run(_live_poller(client)._poll_once())
        self.assertEqual(client.delete_all_calls, 0)
        self.assertEqual(client.deleted_ids, [])

    def test_backlog_start_on_a_fresh_install_does_not_clear(self):
        """Skipped backlog isn't a genuine start, so it can't trigger a
        clear; the next genuine start clears it along with the rest."""
        client = QueueClient([FakeMessage(10, create_time_ms=None), _stale(9)],
                             supports_delete_all=True)
        poller = _make_poller(client)  # fresh install: no bookmark
        _run(poller._poll_once())
        self.assertEqual(client.delete_all_calls, 0)
        self.assertEqual(client.deleted_ids, [])

    def test_one_clear_per_poll_across_vehicles(self):
        a, b = "VINA0000000000001", "VINB0000000000002"
        client = QueueClient([FakeMessage(11, vin=a), FakeMessage(10, vin=b)],
                             supports_delete_all=True)
        poller = _live_poller(client)
        poller.register_coordinator(a, FakeCoordinator())
        poller.register_coordinator(b, FakeCoordinator())
        _run(poller._poll_once())
        self.assertEqual(client.delete_all_calls, 1)


if __name__ == "__main__":
    unittest.main()
