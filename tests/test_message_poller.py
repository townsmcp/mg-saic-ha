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
        self.message_time = message_time


class FakeResponse:
    def __init__(self, messages):
        self.messages = messages


class FakeClient:
    """Serves one message on page 1, empty thereafter — a single-item queue."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.deleted_ids = []

    async def get_alarm_messages(self, page_num, page_size):
        if page_num == 1 and self._messages:
            return FakeResponse([self._messages[0]])
        return FakeResponse([])

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
        """
        started_at = datetime(2026, 8, 24, 20, 49, 39, tzinfo=timezone.utc)
        message_created = datetime(2026, 8, 25, 6, 18, 44, tzinfo=timezone.utc)

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
        started_at = datetime(2026, 8, 24, 20, 49, 39, tzinfo=timezone.utc)
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

    def test_message_with_no_create_time_defaults_to_fresh(self):
        """Unknown age must fail toward processing, not toward silence —
        a missed refresh is worse than an occasional extra one.
        """
        started_at = datetime(2026, 8, 24, 20, 49, 39, tzinfo=timezone.utc)
        msg = FakeMessage(222, create_time_ms=None)
        client = FakeClient([msg])
        coordinator = FakeCoordinator()
        poller = _make_poller(client, coordinator=coordinator)
        poller._started_at = started_at

        _run(poller._poll_once())

        self.assertEqual(len(coordinator.refresh_reasons), 1)
        self.assertIn(222, client.deleted_ids)

    def test_second_poll_processes_normally_regardless_of_classification(self):
        """The historical/fresh split only applies to the first poll. Once
        a stale message has been discarded and the watermark advanced, the
        very next poll must behave exactly as it always has.
        """
        started_at = datetime(2026, 8, 24, 20, 49, 39, tzinfo=timezone.utc)
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


if __name__ == "__main__":
    unittest.main()
