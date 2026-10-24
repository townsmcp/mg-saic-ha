"""Scheduled Charging Start / End follow the car again once sent (#452).

A time changed in Home Assistant is held until it is sent with the Scheduled
Charging Mode. It used to be held for good: after it had been sent, a change
made in the iSmart app never showed in Home Assistant until it restarted
(@RBDK95's MG4, 24 Oct 2026). Now it is let go once it has been sent and the
car has reported since.

    car says 01:30 -> set 00:15 in HA -> HA shows 00:15
    change the mode -> 00:15 sent -> car reports 00:15
    change it to 01:00 in the app -> HA shows 01:00 (it stayed on 00:15)
"""

import asyncio
import unittest
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_charging_controls_dropout import MODS, _charging
from test_erac_fallback import LOGIC

T0 = datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc)


def _car(start_hour, start_minute):
    charging = _charging()
    charging.chrgMgmtData.bmsReserStHourDspCmd = start_hour
    charging.chrgMgmtData.bmsReserStMintueDspCmd = start_minute
    return charging


def _coordinator():
    return NS(
        data={"status": NS(), "charging": _car(1, 30)},
        last_charging_data=None,
        last_update_success=True,
        scheduled_charging_pending_start=None,
        scheduled_charging_pending_end=None,
        scheduled_charging_sent_at=None,
        charging_freshness=NS(last_success=T0),
        after_action_delay=10,
        charging_current_long_interval=60,
        schedule_action_refresh=AsyncMock(),
        record_command_error=MagicMock(),
    )


def _start(c):
    cls = MODS["time"].SAICMGScheduledChargingTime
    entity = cls.__new__(cls)
    entity.coordinator = c
    entity._key = "start"
    entity._vin = "VIN1"
    entity._hour_attr = "bmsReserStHourDspCmd"
    entity._minute_attr = "bmsReserStMintueDspCmd"
    entity._default_value = time(0, 0)
    entity.async_write_ha_state = MagicMock()
    return entity


def _mode(c, client):
    cls = MODS["select"].SAICMGScheduledChargingModeSelect
    entity = cls.__new__(cls)
    entity.coordinator = c
    entity._client = client
    entity._vin = "VIN1"
    return entity


def _poll(c, charging, at):
    c.data["charging"] = charging
    c.charging_freshness.last_success = at


class FollowsTheCarTests(unittest.TestCase):
    def setUp(self):
        self.c = _coordinator()
        self.start = _start(self.c)
        self.client = NS(set_scheduled_charging=AsyncMock())
        self.mode = _mode(self.c, self.client)

    def test_the_example(self):
        self.assertEqual(self.start.native_value, time(1, 30))
        asyncio.run(self.start.async_set_value(time(0, 15)))
        self.assertEqual(self.start.native_value, time(0, 15))

        asyncio.run(self.mode.async_select_option("Until Scheduled Time"))
        sent_start = self.client.set_scheduled_charging.await_args.args[1]
        self.assertEqual(sent_start, time(0, 15))
        # Still shown until the car has reported.
        self.assertEqual(self.start.native_value, time(0, 15))

        _poll(self.c, _car(0, 15), datetime.now(timezone.utc) + timedelta(seconds=1))
        self.assertEqual(self.start.native_value, time(0, 15))
        self.assertIsNone(self.c.scheduled_charging_pending_start)

        # Changed in the iSmart app afterwards.
        _poll(self.c, _car(1, 0), datetime.now(timezone.utc) + timedelta(minutes=30))
        self.assertEqual(self.start.native_value, time(1, 0))

    def test_not_sent_yet_is_held_through_polls(self):
        asyncio.run(self.start.async_set_value(time(0, 15)))
        _poll(self.c, _car(1, 30), T0 + timedelta(hours=1))
        self.assertEqual(self.start.native_value, time(0, 15))

    def test_a_failed_send_keeps_it(self):
        self.client.set_scheduled_charging.side_effect = RuntimeError("return code 4")
        asyncio.run(self.start.async_set_value(time(0, 15)))
        asyncio.run(self.mode.async_select_option("Until Scheduled Time"))
        _poll(self.c, _car(1, 30), datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual(self.start.native_value, time(0, 15))

    def test_a_new_time_after_sending_is_held_until_sent(self):
        asyncio.run(self.start.async_set_value(time(0, 15)))
        asyncio.run(self.mode.async_select_option("Until Scheduled Time"))
        asyncio.run(self.start.async_set_value(time(2, 0)))
        _poll(self.c, _car(0, 15), datetime.now(timezone.utc) + timedelta(minutes=5))
        self.assertEqual(self.start.native_value, time(2, 0))


class HelperTests(unittest.TestCase):
    def test_let_go_only_after_a_newer_reading(self):
        c = NS(
            scheduled_charging_pending_start=time(0, 15),
            scheduled_charging_pending_end=time(5, 0),
            scheduled_charging_sent_at=None,
            charging_freshness=NS(last_success=T0),
        )
        LOGIC.note_scheduled_charging_sent(c, T0 + timedelta(minutes=1))
        self.assertEqual(LOGIC.scheduled_charging_pending(c, "end"), time(5, 0))
        c.charging_freshness.last_success = T0 + timedelta(minutes=2)
        self.assertIsNone(LOGIC.scheduled_charging_pending(c, "end"))
        self.assertIsNone(LOGIC.scheduled_charging_pending(c, "start"))

    def test_no_freshness_tracker(self):
        c = NS(scheduled_charging_pending_start=time(0, 15), scheduled_charging_sent_at=T0)
        self.assertEqual(LOGIC.scheduled_charging_pending(c, "start"), time(0, 15))


if __name__ == "__main__":
    unittest.main()
