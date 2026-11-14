"""Battery heating: the car's reply, and which field says it's on (#487).

@HarryFlatter, HS PHEV (AS33P), 14 Nov 2026, battery full, plugged in, charge
finished: Battery Heating was sent, the car replied ptcHeatResp 0,
ptcHeatReqDspCmd 2, and heating stayed off, but Home Assistant logged
"Battery heating started".

James's MGS6, the same evening, charging at 28%, 4 C outside: the reply was
ptcHeatResp 4, ptcHeatReqDspCmd 2. Asked the same from the iSmart app, the
app showed "Battery pack does not need to be heated".

ptcHeatResp / bmsPTCHeatResp is the stop reason (mg-saic-client's
HeatingStopReason, 4 = unnecessary), and on/off is bmsPTCHeatReqDspCmd
(1 = heating), as in the library and the MQTT gateway. The switch and the
Battery Heating Status sensor read on/off from bmsPTCHeatResp.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

from test_charging_controls_dropout import MODS, _entity

LOGIC = sys.modules["mg_saic.logic"]
EVENT = sys.modules["mg_saic.event"]
PKG = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"

MGS6_REPLY = NS(ptcHeatResp=4, ptcHeatReqDspCmd=2, rvcReqSts=3)
HARRY_REPLY = NS(ptcHeatResp=0, ptcHeatReqDspCmd=2, rvcReqSts=3)
HEATING_REPLY = NS(ptcHeatResp=0, ptcHeatReqDspCmd=1, rvcReqSts=3)


class RefusalTests(unittest.TestCase):
    def test_mgs6_not_needed(self):
        self.assertEqual(
            LOGIC.battery_heating_refusal(MGS6_REPLY),
            "The car says the battery pack doesn't need to be heated (reason 4)",
        )

    def test_harry_no_reason(self):
        self.assertEqual(
            LOGIC.battery_heating_refusal(HARRY_REPLY),
            "The car didn't start it and didn't say why (reason 0)",
        )

    def test_heating_started(self):
        self.assertIsNone(LOGIC.battery_heating_refusal(HEATING_REPLY))

    def test_reply_that_doesnt_say(self):
        self.assertIsNone(LOGIC.battery_heating_refusal(None))
        self.assertIsNone(LOGIC.battery_heating_refusal(NS(ptcHeatResp=4)))

    def test_other_reasons(self):
        self.assertIn("too low", LOGIC.battery_heating_reason(2))
        self.assertIn("fault", LOGIC.battery_heating_reason(7))
        self.assertIsNone(LOGIC.battery_heating_reason(0))


def _switch(reply=None, chrg=None):
    c = NS(
        data={"status": NS(), "charging": NS(chrgMgmtData=chrg) if chrg else None},
        after_action_delay=10,
        battery_heating_long_interval=60,
        schedule_action_refresh=AsyncMock(),
        record_command_error=MagicMock(),
    )
    switch = _entity(MODS["switch"].SAICMGBatteryHeatingSwitch, c)
    switch._client = NS(send_vehicle_charging_ptc_heat=AsyncMock(return_value=reply))
    switch._vin = "VIN1"
    return switch


class SwitchTests(unittest.TestCase):
    def test_declined_is_reported_not_raised(self):
        switch = _switch(MGS6_REPLY)
        asyncio.run(switch.async_turn_on())  # must not raise
        source, error = switch.coordinator.record_command_error.call_args.args
        self.assertEqual(source, "Error starting battery heating")
        self.assertIn("doesn't need to be heated", error)
        # Still refreshed, so the switch shows what the car is doing.
        switch.coordinator.schedule_action_refresh.assert_awaited_once()
        self.assertIn("doesn't need", switch.extra_state_attributes["last_refusal_reason"])
        self.assertIn("last_refusal_time", switch.extra_state_attributes)

    def test_started_is_not_an_error(self):
        switch = _switch(HEATING_REPLY)
        asyncio.run(switch.async_turn_on())
        switch.coordinator.record_command_error.assert_not_called()

    def test_reply_without_state_is_not_an_error(self):
        switch = _switch(None)
        asyncio.run(switch.async_turn_on())
        switch.coordinator.record_command_error.assert_not_called()

    def test_on_off_from_the_right_field(self):
        on = _switch(chrg=NS(bmsPTCHeatReqDspCmd=1, bmsPTCHeatResp=0))
        off = _switch(chrg=NS(bmsPTCHeatReqDspCmd=2, bmsPTCHeatResp=0))
        old_on = _switch(chrg=NS(bmsPTCHeatReqDspCmd=2, bmsPTCHeatResp=1))
        self.assertTrue(on.is_on)
        self.assertFalse(off.is_on)
        self.assertFalse(old_on.is_on)

    def test_stop_reason_attribute(self):
        switch = _switch(chrg=NS(bmsPTCHeatReqDspCmd=2, bmsPTCHeatResp=5))
        self.assertIn("stop time", switch.extra_state_attributes["stop_reason"])
        self.assertNotIn("stop_reason", _switch(chrg=NS(bmsPTCHeatReqDspCmd=2, bmsPTCHeatResp=0)).extra_state_attributes)


class EventTests(unittest.TestCase):
    def test_reason_is_the_cars(self):
        attrs = EVENT._humanize_command_error(
            "Error starting battery heating",
            "Battery heating declined: The car says the battery pack doesn't need to be heated (reason 4)",
        )
        self.assertEqual(
            attrs["reason"],
            "The car says the battery pack doesn't need to be heated (reason 4)",
        )


class StatusSensorTests(unittest.TestCase):
    def test_reads_on_off_from_req_dsp_cmd(self):
        source = (PKG / "sensor.py").read_text()
        self.assertIn('raw_value = getattr(charging_data, "bmsPTCHeatReqDspCmd", None)', source)
        self.assertNotIn('2: "Error",', source)

    def test_unique_id_unchanged(self):
        # The sensor keeps bmsPTCHeatResp as its field: it's in the unique id.
        source = (PKG / "sensor.py").read_text()
        self.assertIn('"Battery Heating Status",\n                    "bmsPTCHeatResp",', source)


if __name__ == "__main__":
    unittest.main()
