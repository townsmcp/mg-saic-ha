"""After a charge, with the charging request failing (#269).

@HarryFlatter's HS PHEV, 14 Oct 2026 (BST), Quiet Charging 00:00-06:00 with
his zappi's Plug Status as the charger entity:

    00:37  zappi says Charging; one live poll: charging, 6%, 15.75 A, 6.24 kW,
           status charging flag 1
    04:07  zappi stops; one live poll: status fine (100%, flag 0), but every
           charging request gets return code 4 -- still so at 08:53
    04:36  zappi says Charging for about a minute; one live poll
    04:39  zappi stopped; one more live poll

Charging Power and Current showed 6.24 kW and 15.75 A all morning, and the
04:36 and 04:39 polls were for a car that was already full.
"""

import asyncio
import sys
import unittest
from datetime import datetime, time, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR
from test_charging_controls_dropout import MODS

COORD_MOD = sys.modules["mg_saic.coordinator"]
COORD_CLS = COORD_MOD.SAICMGDataUpdateCoordinator
LOGIC = sys.modules["mg_saic.logic"]
QH = sys.modules["mg_saic.quiet_hours"]

UTC = timezone.utc
ENTRY = NS(entry_id="entry1")
ZAPPI = "sensor.myenergi_zappi_plug_status"
READING_0408 = int(datetime(2026, 10, 14, 3, 8, 10, tzinfo=UTC).timestamp())
AT_0436 = datetime(2026, 10, 14, 3, 36, 28, tzinfo=UTC)


def _status(flag, soc=100):
    return NS(basicVehicleStatus=NS(extendedData1=soc, extendedData2=flag, lockStatus=1))


def _charging_0037():
    # 15.75 A at 396.25 V: 6.24 kW.
    return NS(chrgMgmtData=NS(
        bmsChrgSts=1, bmsPackCrnt=19685, bmsPackVol=1585, chrgngRmnngTime=188,
        bmsPackSOCDsp=63, bmsOnBdChrgTrgtSOCDspCmd=0,
    ))


class FlagTests(unittest.TestCase):
    def test_flag_read(self):
        self.assertEqual(LOGIC.status_charging_flag(_status(1)), 1)
        self.assertEqual(LOGIC.status_charging_flag(_status(0)), 0)
        for odd in (None, 2, -1, True):
            self.assertIsNone(LOGIC.status_charging_flag(_status(odd)))
        self.assertIsNone(LOGIC.status_charging_flag(None))

    def test_only_believed_once_seen_during_a_charge(self):
        self.assertFalse(LOGIC.status_says_not_charging(_status(0), flag_trusted=False))
        self.assertTrue(LOGIC.status_says_not_charging(_status(0), flag_trusted=True))
        self.assertFalse(LOGIC.status_says_not_charging(_status(1), flag_trusted=True))
        self.assertFalse(LOGIC.status_says_not_charging(_status(None), flag_trusted=True))


def _coordinator():
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.is_charging = False
    c.data = {"status": None, "charging": None}
    c.vin_info = NS(vin="VIN1", brandName="MG", modelName="HS")
    c.last_update_success = True
    c.charging_capacity_correction = None
    return c


class LearningTests(unittest.TestCase):
    def test_the_0037_poll_confirms_the_flag(self):
        c = _coordinator()
        c.is_charging = True
        c._note_charging_flag(_status(1, soc=6), _charging_0037())
        self.assertTrue(c._charging_flag_seen)

    def test_a_flag_of_0_while_charging_teaches_nothing(self):
        # An MG4's first poll of a charge: charging, flag still 0.
        c = _coordinator()
        c.is_charging = True
        c._note_charging_flag(_status(0), _charging_0037())
        self.assertFalse(getattr(c, "_charging_flag_seen", False))

    def test_update_state_calls_it(self):
        from pathlib import Path

        source = (Path(COORD_MOD.__file__)).read_text()
        block = source.split("def _update_state(self, data):", 1)[1]
        block = block.split("\n    def ", 1)[0]
        self.assertIn("self._note_charging_flag(status_data, charging_data)", block)


class CoordinatorPropertyTests(unittest.TestCase):
    def test_after_the_charge(self):
        c = _coordinator()
        c._charging_flag_seen = True
        c.data = {"status": _status(0), "charging": None}
        self.assertTrue(c.charging_stopped_per_status)

    def test_not_while_the_charging_data_answers(self):
        c = _coordinator()
        c._charging_flag_seen = True
        c.data = {"status": _status(0), "charging": _charging_0037()}
        self.assertFalse(c.charging_stopped_per_status)

    def test_not_on_a_car_whose_flag_was_never_seen(self):
        c = _coordinator()
        c.data = {"status": _status(0), "charging": None}
        self.assertFalse(c.charging_stopped_per_status)

    def test_not_when_the_status_says_charging(self):
        # A mid-charge drop-out keeps the last figures, as before.
        c = _coordinator()
        c._charging_flag_seen = True
        c.data = {"status": _status(1), "charging": None}
        self.assertFalse(c.charging_stopped_per_status)


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.c = _coordinator()
        self.c._charging_flag_seen = True
        c = self.c
        self.sensors = {
            "power": SENSOR.SAICMGChargingPowerSensor(
                c, ENTRY, "Charging Power", None, None, None, None
            ),
            "current": SENSOR.SAICMGChargingCurrentSensor(
                c, ENTRY, "Charging Current", "bmsPackCrnt", None, None, None, None, 0.05
            ),
            "voltage": SENSOR.SAICMGChargingSensor(
                c, ENTRY, "Charging Voltage", "bmsPackVol", None, None, None, None, 0.25
            ),
            "remaining": SENSOR.SAICMGChargingSensor(
                c, ENTRY, "Remaining Charging Time", "chrgngRmnngTime",
                None, None, None, None, 1.0
            ),
            "status": SENSOR.SAICMGChargingSensor(
                c, ENTRY, "Charging Status", "bmsChrgSts", None, None, None, None, 1.0
            ),
        }

    def _read(self):
        return {name: s.native_value for name, s in self.sensors.items()}

    def test_harrys_morning(self):
        self.c.data = {"status": _status(1, soc=6), "charging": _charging_0037()}
        at_0037 = self._read()
        self.assertEqual(at_0037["power"], 6.24)
        self.assertEqual(at_0037["current"], 15.75)
        self.assertEqual(at_0037["status"], "Charging (AC)")

        # 04:08: status answers, charging gets code 4.
        self.c.data = {"status": _status(0), "charging": None}
        self.assertEqual(
            self._read(),
            {"power": 0, "current": 0, "voltage": 0, "remaining": 0,
             "status": "Not Charging"},
        )

    def test_a_mid_charge_drop_out_still_holds_the_figures(self):
        self.c.data = {"status": _status(1, soc=6), "charging": _charging_0037()}
        before = self._read()
        self.c.data = {"status": _status(1, soc=40), "charging": None}
        self.assertEqual(self._read(), before)

    def test_without_the_flag_confirmed_nothing_changes(self):
        self.c._charging_flag_seen = False
        self.c.data = {"status": _status(1, soc=6), "charging": _charging_0037()}
        before = self._read()
        self.c.data = {"status": _status(0), "charging": None}
        self.assertEqual(self._read(), before)


class ChargingSwitchTests(unittest.TestCase):
    def _switch(self, coordinator):
        cls = MODS["switch"].SAICMGChargingSwitch
        switch = cls.__new__(cls)
        switch.coordinator = coordinator
        return switch

    def test_off_after_the_charge(self):
        c = _coordinator()
        c._charging_flag_seen = True
        c.data = {"status": _status(0), "charging": None}
        self.assertIs(self._switch(c).is_on, False)

    def test_unknown_when_the_status_cannot_say(self):
        c = _coordinator()
        c.data = {"status": _status(0), "charging": None}
        self.assertIsNone(self._switch(c).is_on)


# ── The 04:36 blip ──────────────────────────────────────────────────────────


def _event(old, new):
    return NS(data={"old_state": NS(state=old), "new_state": NS(state=new)})


def _quiet_coordinator(soc, *, reading=READING_0408, charger="Charging"):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin = "TESTVIN"
    c.quiet_hours = True
    c.live_polling = False
    c.quiet_hours_start = time(0, 0)
    c.quiet_hours_end = time(6, 0)
    c.quiet_charger_entity = ZAPPI
    c._charger_unsub = None
    c._charger_stop_unsub = None
    c.is_powered_on = False
    c.is_charging = False
    c._last_status_time = reading
    c.last_charging_data = _charging_0037()
    c.hass = MagicMock()
    c.hass.states.get = lambda eid: NS(state=charger) if eid == ZAPPI else None
    c.async_request_refresh = AsyncMock()
    # The 04:08 poll: status only.
    c.data = {"status": _status(0, soc=soc), "charging": None}
    return c


def _at(moment, coro):
    with patch.object(COORD_MOD, "utcnow", lambda: moment):
        return asyncio.run(coro)


class FullCarBlipTests(unittest.TestCase):
    def test_no_poll_for_the_0436_blip(self):
        c = _quiet_coordinator(100)
        _at(AT_0436, c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_no_poll_for_the_stop_after_it(self):
        c = _quiet_coordinator(100, charger="EV Connected")
        _at(AT_0436, c._handle_charger_stopped())
        c.async_request_refresh.assert_not_awaited()

    def test_a_real_charge_start_still_polls(self):
        # 00:37: last reading well short of full.
        c = _quiet_coordinator(6)
        _at(AT_0436, c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_awaited_once()

    def test_a_real_charge_end_still_polls(self):
        # 04:07: the last reading was the 00:37 one, at 6%.
        c = _quiet_coordinator(6, charger="EV Connected")
        _at(AT_0436, c._handle_charger_stopped())
        c.async_request_refresh.assert_awaited_once()

    def test_a_full_reading_over_two_hours_old_does_not_count(self):
        # The car may have been driven since.
        c = _quiet_coordinator(100, reading=READING_0408 - 2 * 3600 - 60)
        _at(AT_0436, c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_awaited_once()

    def test_full_means_at_the_target(self):
        # An MG4 set to 80% and at 80%.
        c = _quiet_coordinator(80)
        c.last_charging_data = NS(chrgMgmtData=NS(bmsOnBdChrgTrgtSOCDspCmd=5))
        _at(AT_0436, c._handle_charger_change(_event("EV Connected", "Charging")))
        c.async_request_refresh.assert_not_awaited()

    def test_pure_check(self):
        now = AT_0436.timestamp()
        self.assertTrue(QH.full_at_recent_reading(100.0, None, READING_0408, now))
        self.assertFalse(QH.full_at_recent_reading(99.0, None, READING_0408, now))
        self.assertFalse(QH.full_at_recent_reading(None, None, READING_0408, now))
        self.assertFalse(QH.full_at_recent_reading(100.0, None, None, now))
        self.assertEqual(QH.FULL_READING_MAX_AGE.total_seconds(), 2 * 3600)


if __name__ == "__main__":
    unittest.main()
