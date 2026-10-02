"""A rejected command is only called a "command limit" when SAIC says so.

2026-10-02, MGS6 EV. A climate session had just ended and the car was shutting
down. Two commands in a row were answered with return code 8 and

    Request failed. Please check the vehicle status and try again.(8)

and the next one, 15 seconds after the second rejection, was accepted. The
persistent notification already quoted SAIC and gave no key-start advice, but
the Command Errors event still fired ``command_limit_reached`` with "Vehicle
reached the maximum number of remote commands. Start the vehicle with the
physical key to reset." -- for a rejection that had nothing to do with a limit.

Such a rejection now fires ``command_rejected`` with SAIC's own words.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

EVENT = sys.modules["mg_saic.event"]
LOGIC = sys.modules["mg_saic.logic"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator

NOT_READY = "Request failed. Please check the vehicle status and try again.(8)"
LIMIT = "The number of remote commands has reached the maximum"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _entity():
    vin_info = MagicMock(vin="VIN1", brandName="MG", modelName="MGS6 EV")
    entity = EVENT.SAICMGCommandErrorEvent(
        MagicMock(vin_info=vin_info), MagicMock(), MagicMock(entry_id="e1"), vin_info, "VIN1"
    )
    entity._trigger_event = MagicMock()
    entity.async_write_ha_state = MagicMock()
    return entity


def _coordinator(saic_says, entity):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vin_info = SimpleNamespace(brandName="MG", modelName="MGS6 EV")
    c.client = SimpleNamespace(last_rejection_message=saic_says)
    c.hass = SimpleNamespace(services=SimpleNamespace(async_call=AsyncMock()))
    c._command_error_event_entity = entity
    return c


class IsLimitTests(unittest.TestCase):
    def test_only_a_message_about_a_limit_is_a_limit(self):
        self.assertTrue(LOGIC.command_rejection_is_limit(LIMIT))
        self.assertTrue(LOGIC.command_rejection_is_limit("Remote control limit reached"))
        for other in (NOT_READY, "Operation too frequent", "", None):
            self.assertFalse(LOGIC.command_rejection_is_limit(other), other)


class EventTypeTests(unittest.TestCase):
    def test_command_rejected_is_a_declared_event_type(self):
        # Home Assistant refuses to fire a type that isn't declared.
        self.assertIn(EVENT.EVENT_TYPE_COMMAND_REJECTED, EVENT.EVENT_TYPES)

    def test_record_command_rejected_quotes_saic(self):
        entity = _entity()
        entity.record_command_rejected("climate.set_preset_mode", NOT_READY)
        event_type, attrs = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_COMMAND_REJECTED)
        self.assertEqual(attrs["code"], 8)
        self.assertEqual(attrs["saic_message"], NOT_READY)
        self.assertIn(NOT_READY, attrs["reason"])
        self.assertIn("Try again in a minute", attrs["reason"])
        self.assertNotIn("key", attrs["reason"].lower())
        self.assertNotIn("maximum", attrs["reason"].lower())
        self.assertEqual(attrs["message"], attrs["reason"])
        entity.async_write_ha_state.assert_called_once()

    def test_record_command_rejected_without_a_message(self):
        entity = _entity()
        entity.record_command_rejected("lock", None)
        _type, attrs = entity._trigger_event.call_args.args
        self.assertNotIn("saic_message", attrs)
        self.assertIn("SAIC rejected the command.", attrs["reason"])
        self.assertNotIn("key", attrs["reason"].lower())


class NotifyTests(unittest.TestCase):
    """coordinator.notify_command_limit_reached picks the event by SAIC's words."""

    def test_vehicle_not_ready_fires_command_rejected(self):
        # 2026-10-02 07:06:31 and 07:07:17.
        entity = _entity()
        _run(_coordinator(NOT_READY, entity).notify_command_limit_reached("VIN1"))
        event_type, attrs = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_COMMAND_REJECTED)
        self.assertIn(NOT_READY, attrs["reason"])

    def test_a_real_limit_still_fires_command_limit_reached(self):
        entity = _entity()
        _run(_coordinator(LIMIT, entity).notify_command_limit_reached("VIN1"))
        event_type, attrs = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_COMMAND_LIMIT_REACHED)
        self.assertIn("physical key", attrs["reason"])

    def test_no_message_at_all_is_not_called_a_limit(self):
        entity = _entity()
        _run(_coordinator(None, entity).notify_command_limit_reached("VIN1"))
        event_type, _attrs = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_COMMAND_REJECTED)

    def test_event_and_notification_agree(self):
        entity = _entity()
        c = _coordinator(NOT_READY, entity)
        _run(c.notify_command_limit_reached("VIN1"))
        notification = c.hass.services.async_call.call_args.args[2]["message"]
        _type, attrs = entity._trigger_event.call_args.args
        self.assertIn(NOT_READY, notification)
        self.assertIn(LOGIC.command_rejection_advice(NOT_READY), notification)
        self.assertIn(LOGIC.command_rejection_advice(NOT_READY), attrs["reason"])


if __name__ == "__main__":
    unittest.main()
