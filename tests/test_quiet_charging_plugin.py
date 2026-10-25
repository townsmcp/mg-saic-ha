"""@paulfcarney-ship-it's HS PHEV, 11-12 Oct 2026 (times BST), quiet charging
19:00-07:00, granny charger:

    18:52     last live poll: 49.3 %, not plugged in, locked
    19:00     quiet charging starts
    19:02:14  plugged in; the car writes SAIC's stored copy (flag 0, unlocked)
    19:02:23  charging starts (the car's own record)
    00:48:15  charge ends -- one unbroken charge
    07:00     end time, one live poll: 100 %

Quiet charging never polled at the start, because the last poll had seen the
car unplugged, so HA showed 49.3 % all night; and the charge was recorded
with no duration although the car's record covered all of it.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charge_stats import CSnap, ts as TRIP

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
QH = sys.modules["mg_saic.quiet_hours"]
CACHED = sys.modules["mg_saic.cached_status"]

UTC = timezone.utc
LAST_LIVE = int(datetime(2026, 10, 11, 17, 52, 5, tzinfo=UTC).timestamp())
COPY_AT_PLUG_IN = 1791741734      # 18:02:14 UTC
RECORD_START = 1791741743         # 18:02:23 UTC
RECORD_END = 1791762495           # 23:48:15 UTC
NOW = datetime(2026, 10, 11, 18, 15, tzinfo=UTC)


def _stored(lock, taken=COPY_AT_PLUG_IN, flag=0):
    return CACHED.CachedVehicleStatus(
        basicVehicleStatus={"extendedData1": 49, "extendedData2": flag, "lockStatus": lock},
        gpsPosition={"wayPoint": {}},
        statusTime=taken,
        onlineStatus=1,
    )


def _coordinator(copy_lock, *, live_lock=1, gun=0, soc_tenths=493):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.quiet_hours = True
    c.live_polling = False
    c.quiet_hours_start = datetime(2026, 1, 1, 19, 0).time()
    c.quiet_hours_end = datetime(2026, 1, 1, 7, 0).time()
    c._quiet_finish_check_at = None
    c._quiet_unsubs = []
    c.quiet_last_stored = None
    c.is_powered_on = False
    c.is_charging = False
    c._scheduled_refresh = True
    c._last_status_time = LAST_LIVE
    c._api_lock = None
    c._consecutive_update_failures = 0
    c._action_interval_active = False
    c.holiday_mode = False
    c.default_update_interval = MagicMock()
    c.data = {
        "status": NS(basicVehicleStatus=NS(lockStatus=live_lock, extendedData1=49)),
        "charging": NS(
            rvsChargeStatus=NS(chargingGunState=gun),
            chrgMgmtData=NS(bmsPackSOCDsp=soc_tenths, bmsOnBdChrgTrgtSOCDspCmd=None),
        ),
    }
    c.client = NS(get_cached_vehicle_status=AsyncMock(return_value=_stored(copy_lock)))
    c._run_update_cycle = AsyncMock(return_value={"status": "new", "charging": None})
    c._after_quiet_live_poll = MagicMock()
    c._adjust_update_interval = MagicMock()
    return c


def _update(c):
    with patch.object(COORD_MOD, "utcnow", lambda: NOW):
        return asyncio.run(c._async_update_data())


class PlugInGapTests(unittest.TestCase):
    def test_pauls_plug_in_gets_the_one_live_poll(self):
        # Last seen unplugged and locked; the copy says unlocked.
        c = _coordinator(copy_lock=0)
        self.assertEqual(_update(c)["status"], "new")
        c._run_update_cycle.assert_awaited_once()
        c._after_quiet_live_poll.assert_called_once()

    def test_a_routine_report_with_the_lock_unchanged_is_left_alone(self):
        c = _coordinator(copy_lock=1)
        _update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_a_full_car_is_left_alone_even_if_unlocked(self):
        c = _coordinator(copy_lock=0, soc_tenths=1000)
        _update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_unknown_lock_states_do_not_count(self):
        c = _coordinator(copy_lock=0, live_lock=None)
        _update(c)
        c._run_update_cycle.assert_not_awaited()

    def test_pure_check(self):
        args = dict(
            known_charging=False,
            plugged_in=False,
            last_live_status_epoch=LAST_LIVE,
            already_polled_epoch=None,
            soc_pct=49.3,
            target_pct=100.0,
        )
        self.assertFalse(QH.stored_rewrite_while_plugged(COPY_AT_PLUG_IN, **args))
        self.assertTrue(
            QH.stored_rewrite_while_plugged(COPY_AT_PLUG_IN, lock_changed=True, **args)
        )

    def test_stored_lock_status(self):
        self.assertEqual(QH.stored_lock_status({"fields": {"lockStatus": 0}}), 0)
        self.assertEqual(QH.stored_lock_status({"fields": {"lockStatus": 1}}), 1)
        self.assertIsNone(QH.stored_lock_status({"fields": {"lockStatus": 7}}))
        self.assertIsNone(QH.stored_lock_status(None))


def _snap(at, soc, start, end):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=153760.0, range_km=None,
                 record_start=start, record_end=end)


BEFORE = _snap(datetime(2026, 10, 11, 17, 52, 8, tzinfo=UTC), 49.300000000000004,
               1791653751, 1791684542)
AFTER = _snap(datetime(2026, 10, 12, 6, 0, 25, tzinfo=UTC), 100.0,
              RECORD_START, RECORD_END)


class DurationFromTheCarsRecordTests(unittest.TestCase):
    def setUp(self):
        self.charge = TRIP.reconstruct_unseen_charge(BEFORE, AFTER, capacity_kwh=23.2)

    def test_the_whole_charge_is_the_duration(self):
        self.assertEqual(self.charge["duration_s"], RECORD_END - RECORD_START)  # 5 h 46 min
        self.assertEqual(
            self.charge["charge_start_ts"],
            datetime(2026, 10, 11, 18, 2, 23, tzinfo=UTC).isoformat(),
        )

    def test_marked_as_possibly_short(self):
        self.assertEqual(self.charge["duration_source"], "car_partial")
        self.assertTrue(self.charge["reconstructed"])
        self.assertNotIn("average_power_kW", self.charge)

    def test_battery_figures_rounded(self):
        self.assertEqual(self.charge["soc_start_pct"], 49.3)
        self.assertEqual(self.charge["soc_added_pct"], 50.7)

    def test_harrys_last_stretch_is_not_taken_as_the_duration(self):
        # 6.7 kWh in a 4-minute record: about 100 kW.
        before = _snap(datetime(2026, 10, 10, 22, 40, 36, tzinfo=UTC), 71.3,
                       1791640497, 1791640698)
        after = _snap(datetime(2026, 10, 11, 5, 9, 47, tzinfo=UTC), 100.0,
                      1791681187, 1791681432)
        charge = TRIP.reconstruct_unseen_charge(before, after, capacity_kwh=23.2)
        self.assertNotIn("duration_s", charge)
        self.assertEqual(charge["duration_source"], "not_seen")

    def test_a_record_starting_before_the_first_reading_is_not_used(self):
        after = _snap(datetime(2026, 10, 12, 6, 0, 25, tzinfo=UTC), 100.0,
                      1791741128 - 600, RECORD_END)
        charge = TRIP.reconstruct_unseen_charge(BEFORE, after, capacity_kwh=23.2)
        self.assertNotIn("duration_s", charge)


class SocRoundingTests(unittest.TestCase):
    def test_tenths_come_out_as_tenths(self):
        chrg = NS(chrgMgmtData=NS(bmsPackSOCDsp=493))
        self.assertEqual(COORD_CLS._extract_soc_pct(None, chrg), 49.3)


if __name__ == "__main__":
    unittest.main()
