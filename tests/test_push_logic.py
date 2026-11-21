# File: tests/test_push_logic.py
"""Tests for push_logic.py: the rules behind real-time push.

push_logic.py has no Home Assistant imports, so it loads directly.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "mg_saic"

spec = importlib.util.spec_from_file_location("push_logic_under_test", PKG_DIR / "push_logic.py")
pl = importlib.util.module_from_spec(spec)
sys.modules["push_logic_under_test"] = pl
spec.loader.exec_module(pl)


class PollIntervalTests(unittest.TestCase):
    def test_slow_only_when_push_works_for_every_car(self):
        self.assertEqual(
            pl.message_poll_interval(pl.PushStatus.PUSH, every_car_uses_push=True),
            pl.PUSH_POLL_INTERVAL_SECONDS,
        )

    def test_a_car_without_push_keeps_the_fast_poll(self):
        self.assertEqual(
            pl.message_poll_interval(pl.PushStatus.PUSH, every_car_uses_push=False),
            pl.FALLBACK_POLL_INTERVAL_SECONDS,
        )

    def test_fast_whenever_push_is_not_working(self):
        for status in (pl.PushStatus.CONNECTING, pl.PushStatus.POLLING, pl.PushStatus.OFF):
            self.assertEqual(
                pl.message_poll_interval(status, every_car_uses_push=True),
                pl.FALLBACK_POLL_INTERVAL_SECONDS,
            )

    def test_push_interval_is_thirty_minutes(self):
        self.assertEqual(pl.PUSH_POLL_INTERVAL_SECONDS, 1800)


class StaleTests(unittest.TestCase):
    def test_fresh_and_unknown_age_are_acted_on(self):
        self.assertFalse(pl.is_stale(0))
        self.assertFalse(pl.is_stale(None))
        self.assertFalse(pl.is_stale(pl.STALE_PUSH_SECONDS))

    def test_old_push_is_stale(self):
        self.assertTrue(pl.is_stale(pl.STALE_PUSH_SECONDS + 1))
        # SAIC's 28-day queue replaying after downtime
        self.assertTrue(pl.is_stale(3 * 86400))


class PlausibleStartTests(unittest.TestCase):
    def test_recent_send_time_is_used(self):
        self.assertEqual(pl.plausible_start_time(990.0, 1000.0), 990.0)

    def test_missing_send_time_falls_back_to_now(self):
        self.assertEqual(pl.plausible_start_time(None, 1000.0), 1000.0)

    def test_future_or_ancient_send_time_falls_back_to_now(self):
        self.assertEqual(pl.plausible_start_time(1000.0 + 600, 1000.0), 1000.0)
        self.assertEqual(pl.plausible_start_time(1000.0 - 7 * 3600, 1000.0), 1000.0)

    def test_never_later_than_now(self):
        # up to a minute of clock difference is tolerated, but never returned
        self.assertEqual(pl.plausible_start_time(1030.0, 1000.0), 1000.0)


class RenewalTests(unittest.TestCase):
    def test_not_due_when_young(self):
        self.assertFalse(pl.renewal_due(1000.0, 1000.0 + 3600))

    def test_due_after_the_renewal_age(self):
        self.assertTrue(pl.renewal_due(1000.0, 1000.0 + pl.RENEW_AFTER_SECONDS))

    def test_unknown_age_is_due(self):
        self.assertTrue(pl.renewal_due(None, 1000.0))


class LedgerTests(unittest.TestCase):
    def test_a_push_then_its_queue_message_is_one_event(self):
        ledger = pl.PushLedger()
        self.assertTrue(ledger.note_push(1000.0))
        # the poller later finds the one start in the queue: nothing unannounced
        self.assertEqual(ledger.reconcile(1, 1000.0 + 1500), 0)
        self.assertEqual(ledger.pending, 0)

    def test_a_queue_message_with_no_push_is_unannounced(self):
        ledger = pl.PushLedger()
        self.assertEqual(ledger.reconcile(1, 1000.0), 1)

    def test_counts_not_times(self):
        # Two starts, one push: one is announced, one is not.
        ledger = pl.PushLedger()
        ledger.note_push(1000.0)
        self.assertEqual(ledger.reconcile(2, 1100.0), 1)

    def test_nothing_in_the_queue_leaves_pushes_waiting(self):
        ledger = pl.PushLedger()
        ledger.note_push(1000.0)
        self.assertEqual(ledger.reconcile(0, 1100.0), 0)
        self.assertEqual(ledger.pending, 1)

    def test_waiting_pushes_expire(self):
        ledger = pl.PushLedger()
        ledger.note_push(1000.0)
        later = 1000.0 + pl.PUSH_START_MEMORY_SECONDS + 1
        self.assertEqual(ledger.reconcile(1, later), 1)

    def test_push_after_the_poller_already_acted_is_a_duplicate(self):
        # The queue was read in the seconds before the push arrived.
        ledger = pl.PushLedger()
        ledger.note_poller_handled(1000.0)
        self.assertFalse(ledger.note_push(1010.0))
        # ...and it leaves nothing pending to mis-match a later start
        self.assertEqual(ledger.pending, 0)

    def test_two_real_starts_apart_are_both_acted_on(self):
        ledger = pl.PushLedger()
        self.assertTrue(ledger.note_push(1000.0))
        self.assertTrue(ledger.note_push(1000.0 + 3600))
        self.assertEqual(ledger.total, 2)


class HealthTests(unittest.TestCase):
    LONG_AGO = 0.0

    def test_a_late_push_clears_the_suspicion(self):
        health = pl.PushHealth()
        now = 100000.0
        self.assertTrue(health.suspect_miss(now, self.LONG_AGO))
        health.push_start_seen()
        self.assertFalse(health.confirm(now + 1000))
        self.assertTrue(health.healthy(now + 1000))

    def test_no_push_in_time_confirms_the_miss(self):
        health = pl.PushHealth()
        now = 100000.0
        health.suspect_miss(now, self.LONG_AGO)
        self.assertFalse(health.confirm(now + pl.MISSED_PUSH_CONFIRM_SECONDS - 1))
        self.assertTrue(health.confirm(now + pl.MISSED_PUSH_CONFIRM_SECONDS))
        self.assertEqual(health.misses, 1)

    def test_a_miss_is_confirmed_once(self):
        health = pl.PushHealth()
        health.suspect_miss(100000.0, self.LONG_AGO)
        self.assertTrue(health.confirm(100000.0 + 200))
        self.assertFalse(health.confirm(100000.0 + 300))

    def test_unhealthy_for_a_while_then_healthy_again(self):
        health = pl.PushHealth()
        now = 100000.0
        health.suspect_miss(now, self.LONG_AGO)
        health.confirm(now + 100)
        self.assertFalse(health.healthy(now + 200))
        self.assertTrue(health.healthy(now + 100 + pl.UNHEALTHY_HOLD_SECONDS))

    def test_a_push_start_ends_the_unhealthy_spell(self):
        health = pl.PushHealth()
        health.suspect_miss(100000.0, self.LONG_AGO)
        health.confirm(100000.0 + 100)
        health.push_start_seen()
        self.assertTrue(health.healthy(100000.0 + 200))

    def test_a_recent_reconnect_is_not_blamed_on_push(self):
        health = pl.PushHealth()
        now = 100000.0
        recently = now - 60
        self.assertFalse(health.suspect_miss(now, recently))
        self.assertIsNone(health.seconds_until_verdict(now))

    def test_a_connection_that_is_down_is_not_blamed(self):
        self.assertFalse(pl.PushHealth().suspect_miss(100000.0, None))

    def test_the_wait_is_counted_from_the_first_suspicion(self):
        health = pl.PushHealth()
        health.suspect_miss(100000.0, self.LONG_AGO)
        health.suspect_miss(100050.0, self.LONG_AGO)
        self.assertEqual(health.seconds_until_verdict(100050.0), pl.MISSED_PUSH_CONFIRM_SECONDS - 50)


class PercentInTextTests(unittest.TestCase):
    def test_the_battery_level_in_a_finished_charging_push(self):
        text = "Your vehicle(***647) charging has finished, remaining power is 68%"
        self.assertEqual(pl.percent_in_text(text), 68)

    def test_a_space_before_the_sign_is_fine(self):
        self.assertEqual(pl.percent_in_text("remaining power is 80 %"), 80)

    def test_no_percentage_or_no_text(self):
        self.assertIsNone(pl.percent_in_text("The vehicle starts on 21-11-2026"))
        self.assertIsNone(pl.percent_in_text(None))
        self.assertIsNone(pl.percent_in_text(""))

    def test_not_a_battery_level(self):
        self.assertIsNone(pl.percent_in_text("up 250%"))


if __name__ == "__main__":
    unittest.main()
