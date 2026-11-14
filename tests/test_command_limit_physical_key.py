"""SAIC's "use the physical key" refusal is the command limit (#470).

@sharky-os, 7 Nov 2026 (MG4 Urban, 1.5.0-beta7/beta8): turning the A/C on was
refused with return code 8 and

    Too frequent operations. Please use the physical key to restart the
    vehicle, lock the car and turn off the engine, and then try again.(71)

It fired ``command_rejected`` with "Wait a minute and try again", but SAIC
says only a restart with the key resets it. A message telling you to restart
the car with the key is now the limit. "Too frequent" on its own still isn't:
an MGS6's bare "Operation too frequent" cleared by itself.
"""

import asyncio
import sys
import unittest

from test_command_rejected_event import EVENT, LOGIC, _coordinator, _entity

ERROR_71 = (
    "Too frequent operations. Please use the physical key to restart the "
    "vehicle, lock the car and turn off the engine, and then try again.(71)"
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ClassificationTests(unittest.TestCase):
    def test_error_71_is_the_limit(self):
        self.assertTrue(LOGIC.command_rejection_is_limit(ERROR_71))

    def test_bare_too_frequent_still_isnt(self):
        self.assertFalse(LOGIC.command_rejection_is_limit("Operation too frequent"))
        self.assertIn("Wait a minute", LOGIC.command_rejection_advice("Operation too frequent"))

    def test_advice_says_start_with_the_key_not_wait(self):
        advice = LOGIC.command_rejection_advice(ERROR_71)
        self.assertIn("started with the key", advice)
        self.assertIn("lock it", advice)
        self.assertIn("Waiting won't reset it", advice)
        self.assertNotIn("Wait a minute", advice)


class EventTests(unittest.TestCase):
    def test_fires_command_limit_reached_with_saics_words(self):
        entity = _entity()
        c = _coordinator(ERROR_71, entity)
        _run(c.notify_command_limit_reached("VIN1", "Error turning the A/C on"))
        event_type, attrs = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_COMMAND_LIMIT_REACHED)
        self.assertEqual(attrs["code"], 8)
        self.assertEqual(attrs["saic_message"], ERROR_71)
        self.assertEqual(attrs["action"], "Turning the A/C on")
        self.assertIn("physical key", attrs["reason"])

    def test_notification_quotes_saic_and_gives_key_advice(self):
        entity = _entity()
        c = _coordinator(ERROR_71, entity)
        _run(c.notify_command_limit_reached("VIN1", "Error turning the A/C on"))
        message = c.hass.services.async_call.call_args.args[2]["message"]
        self.assertIn(ERROR_71, message)
        self.assertIn("Waiting won't reset it", message)
        self.assertNotIn("Wait a minute", message)

    def test_limit_event_without_a_message(self):
        entity = _entity()
        entity.record_command_limit_reached("lock")
        _type, attrs = entity._trigger_event.call_args.args
        self.assertNotIn("saic_message", attrs)


if __name__ == "__main__":
    unittest.main()
