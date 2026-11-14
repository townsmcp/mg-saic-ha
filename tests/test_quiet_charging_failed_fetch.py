"""Quiet Charging when the charging request fails (#269, @SecurityJon).

MG Cyberster, Quiet Charging 06:56 to 00:07, charging 00:05 to 06:55.
SAIC often answers the charging request with "no data" or code 4. The 00:07
poll got charging data (charging), the 04:11 one didn't, and when Quiet
Charging started at 06:56 the car counted as not charging, so no "has it
finished?" check was set. Nothing reached the car until 00:07 the next night,
and the charging sensors showed the 00:07 reading all day.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_quiet_hours import COORD_MOD, _coordinator, _run

QH = sys.modules["mg_saic.quiet_hours"]
NOW = datetime(2026, 11, 8, 6, 56, tzinfo=timezone.utc)
TAKEN = datetime(2026, 11, 8, 0, 7, tzinfo=timezone.utc)


def _charging(status=1, remaining=408):
    # 408 minutes from 00:07 is 06:55.
    return NS(
        chrgMgmtData=NS(
            bmsChrgSts=status,
            chrgngRmnngTime=remaining,
            chrgngRmnngTimeV=0,
            bmsPackSOCDsp=500,
            bmsOnBdChrgTrgtSOCDspCmd=None,
            bmsPackCrnt=19950,
            bmsPackVol=1600,
        )
    )


def _car(held=None, taken=TAKEN, latest=None):
    c = _coordinator(charging=latest is not None and latest.chrgMgmtData.bmsChrgSts == 1)
    c.live_polling = True
    c.last_charging_data = held
    c.charging_freshness = NS(last_success=taken)
    c.data = {"status": None, "charging": latest}
    c.resolve_battery_capacity_for = lambda _cd: (77.0, "test")
    return c


def _start(c):
    with patch.object(COORD_MOD, "utcnow", lambda: NOW):
        _run(c.async_set_live_polling(False, reason="start time"))


def _poll(c, data):
    with patch.object(COORD_MOD, "utcnow", lambda: NOW):
        c._after_quiet_live_poll(data)


class Issue269Tests(unittest.TestCase):
    def test_the_report(self):
        # 04:11's charging request failed; the last good reading said charging.
        c = _car(held=_charging())
        _start(c)
        self.assertIsNotNone(c._quiet_finish_check_at)
        # 408 minutes from 00:07 had run out by 06:56: checked in 30 minutes,
        # not 408 + margin from now.
        self.assertEqual(c._quiet_finish_check_at, NOW + timedelta(minutes=30))

    def test_reading_still_running(self):
        # Taken at 05:56 with 120 minutes left: about 60 left at 06:56.
        c = _car(held=_charging(remaining=120), taken=NOW - timedelta(hours=1))
        _start(c)
        self.assertEqual(c._quiet_finish_check_at, NOW + timedelta(minutes=90))

    def test_unknown_age_uses_the_fallback(self):
        c = _car(held=_charging(), taken=None)
        _start(c)
        self.assertEqual(c._quiet_finish_check_at, NOW + QH.FINISH_CHECK_FALLBACK)

    def test_last_reading_not_charging(self):
        c = _car(held=_charging(status=5))
        _start(c)
        self.assertIsNone(c._quiet_finish_check_at)

    def test_no_reading_at_all(self):
        c = _car(held=None)
        _start(c)
        self.assertIsNone(c._quiet_finish_check_at)


class RetryTests(unittest.TestCase):
    def test_check_answered_and_finished(self):
        c = _car(held=_charging())
        _start(c)
        _poll(c, {"status": None, "charging": _charging(status=5)})
        self.assertIsNone(c._quiet_finish_check_at)

    def test_check_answered_still_charging(self):
        c = _car(held=_charging())
        _start(c)
        c.is_charging = True
        _poll(c, {"status": None, "charging": _charging(remaining=20)})
        self.assertEqual(c._quiet_finish_check_at, NOW + timedelta(minutes=50))
        self.assertEqual(c._quiet_checks_from_old_reading, 0)

    def test_check_unanswered_retries_once_then_waits(self):
        c = _car(held=_charging())
        _start(c)
        _poll(c, {"status": None, "charging": None})
        self.assertIsNotNone(c._quiet_finish_check_at)  # the one retry
        _poll(c, {"status": None, "charging": None})
        self.assertIsNone(c._quiet_finish_check_at)  # then the end time

    def test_retry_count_resets_at_the_end_time(self):
        c = _car(held=_charging())
        _start(c)
        _poll(c, {"status": None, "charging": None})
        _poll(c, {"status": None, "charging": None})
        _run(c.async_set_live_polling(True, reason="end time"))
        self.assertEqual(c._quiet_checks_from_old_reading, 0)
        _start(c)
        self.assertIsNotNone(c._quiet_finish_check_at)


class UnchangedTests(unittest.TestCase):
    def test_charging_data_came_back_and_charging(self):
        c = _car(latest=_charging(remaining=60), held=_charging(remaining=60))
        c.is_charging = True
        _start(c)
        self.assertEqual(c._quiet_finish_check_at, NOW + timedelta(minutes=90))

    def test_charging_data_came_back_not_charging(self):
        # A good reading that says not charging wins over an older one.
        c = _car(latest=_charging(status=5), held=_charging())
        c.is_charging = False
        _start(c)
        self.assertIsNone(c._quiet_finish_check_at)


if __name__ == "__main__":
    unittest.main()
