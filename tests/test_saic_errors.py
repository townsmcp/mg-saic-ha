"""Reading what went wrong from SAIC's return code, not from the error text.

mg-saic-client 0.9.6 keeps SAIC's return code and message as separate values
on its errors. errors.py reads those first, and only searches the text for
errors that don't carry them. These tests pin both halves, and that the
integration's call sites now behave correctly even when the text is reworded.
"""

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

API = sys.modules["mg_saic.api"]
ERRORS = sys.modules["mg_saic.errors"]
REPO_ROOT = Path(__file__).resolve().parent.parent


class LibraryError(Exception):
    """Shaped like mg-saic-client 0.9.6's SaicApiException."""

    def __init__(self, saic_message, return_code=None, text=None, logged_out=False):
        self.saic_message = saic_message
        self.return_code = return_code
        self.is_logged_out = logged_out
        # str() is whatever we like: with the values present it must not matter.
        super().__init__(text if text is not None else f"reworded: {saic_message}")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class ValuesBeatTextTests(unittest.TestCase):
    """With the library's values present, the wording is irrelevant."""

    def test_unreachable_from_the_code_whatever_the_text_says(self):
        error = LibraryError("Das Fahrzeug antwortet nicht", 4, text="oops")
        self.assertTrue(ERRORS.is_vehicle_unreachable(error))
        self.assertEqual(ERRORS.return_code_of(error), 4)

    def test_text_that_merely_mentions_a_code_is_ignored(self):
        # The library says code 6; the text happens to contain "return code: 4".
        error = LibraryError("busy", 6, text="return code: 4, message: busy")
        self.assertFalse(ERRORS.is_vehicle_unreachable(error))
        self.assertEqual(ERRORS.return_code_of(error), 6)

    def test_no_code_on_a_library_error_means_no_code(self):
        error = LibraryError("failed to parse", None, text="return code: 8")
        self.assertIsNone(ERRORS.return_code_of(error))

    def test_message_comes_back_clean(self):
        error = LibraryError("Vehicle not locked.", 8, text="x, event_id: 123")
        self.assertEqual(ERRORS.saic_message_of(error), "Vehicle not locked.")

    def test_rejected_and_not_locked(self):
        self.assertTrue(ERRORS.is_request_rejected(LibraryError("limit reached", 8)))
        self.assertTrue(ERRORS.is_vehicle_not_locked(LibraryError("Vehicle not locked.", 8)))
        self.assertFalse(ERRORS.is_vehicle_not_locked(LibraryError("limit reached", 8)))

    def test_event_id_containing_401_is_not_a_logout(self):
        # The old bare '"401" in text' check re-logged in on this.
        error = LibraryError(
            "The remote control instruction failed", 4,
            text="return code: 4, message: failed, event_id: 1118401795",
        )
        self.assertFalse(ERRORS.is_session_expired(error))

    def test_logged_out(self):
        self.assertTrue(ERRORS.is_session_expired(LibraryError("x", 401)))
        self.assertTrue(ERRORS.is_session_expired(LibraryError("x", 403)))
        self.assertTrue(ERRORS.is_session_expired(LibraryError("x", None, logged_out=True)))
        self.assertFalse(ERRORS.is_session_expired(LibraryError("x", 8)))


class TextFallbackTests(unittest.TestCase):
    """Errors without the library's values behave exactly as before."""

    def test_plain_exception_text(self):
        error = Exception("return code: 4, message: The remote control instruction failed")
        self.assertTrue(ERRORS.is_vehicle_unreachable(error))
        self.assertEqual(ERRORS.saic_message_of(error), "The remote control instruction failed")

    def test_plain_string(self):
        self.assertEqual(ERRORS.return_code_of("return code: 8, message: x"), 8)
        self.assertIsNone(ERRORS.return_code_of("something else"))

    def test_session_phrases_and_bare_401(self):
        for text in ("Invalid session", "token expired", "Not logged in", "HTTP 401"):
            self.assertTrue(ERRORS.is_session_expired(Exception(text)), text)
        self.assertFalse(ERRORS.is_session_expired(Exception("return code: 4")))

    def test_too_frequent_counts_as_rejected(self):
        self.assertTrue(ERRORS.is_request_rejected(Exception("Operation too frequent")))


class CauseChainTests(unittest.TestCase):
    def test_wrapped_error_leads_back_to_saics_reply(self):
        try:
            try:
                raise LibraryError("Vehicle not locked.", 8)
            except LibraryError as inner:
                raise API.VehicleNotLockedException("anything") from inner
        except API.VehicleNotLockedException as outer:
            self.assertEqual(ERRORS.return_code_of(outer), 8)
            self.assertEqual(ERRORS.saic_message_of(outer), "Vehicle not locked.")


class ApiCallTests(unittest.TestCase):
    """_make_api_call classifies by SAIC's values."""

    def _client(self, error):
        client = API.SAICMGAPIClient("user@example.com", "hunter2")
        client.saic_api = MagicMock(is_logged_in=True)

        async def boom(*_a, **_kw):
            raise error

        return client, boom

    def test_not_locked_with_reworded_text(self):
        client, boom = self._client(LibraryError("Vehicle not locked. Please lock it.", 8))
        with self.assertRaises(API.VehicleNotLockedException) as ctx:
            _run(client._make_api_call(boom))
        self.assertIsInstance(ctx.exception.__cause__, LibraryError)

    def test_rejection_keeps_saics_words_for_the_notification(self):
        client, boom = self._client(
            LibraryError("The number of remote commands has reached the maximum", 8)
        )
        with self.assertRaises(API.CommandsLimitReachedException):
            _run(client._make_api_call(boom))
        self.assertEqual(
            client.last_rejection_message,
            "The number of remote commands has reached the maximum",
        )

    def test_unreachable_is_passed_through_untouched(self):
        error = LibraryError("failed", 4)
        client, boom = self._client(error)
        with self.assertRaises(LibraryError) as ctx:
            _run(client._make_api_call(boom))
        self.assertIs(ctx.exception, error)


_REAL_LIBRARY = """
import importlib.util, json, sys
from importlib.metadata import version
from saic_ismart_client_ng.exceptions import (
    SaicApiException, SaicApiRetryException, SaicLogoutException)
spec = importlib.util.spec_from_file_location("errors", sys.argv[1])
errors = importlib.util.module_from_spec(spec); spec.loader.exec_module(errors)
unreachable = SaicApiRetryException(
    "The remote control instruction failed, please try again later.",
    event_id="1118401795", return_code=4)
not_locked = SaicApiException("Vehicle not locked. Please lock it and try again.", 8)
print(json.dumps({
    "version": version("mg-saic-client"),
    "unreachable": [errors.is_vehicle_unreachable(unreachable),
                    errors.is_session_expired(unreachable),
                    errors.saic_message_of(unreachable)],
    "not_locked": [errors.is_vehicle_not_locked(not_locked),
                   errors.is_request_rejected(not_locked),
                   errors.return_code_of(not_locked)],
    "logout": errors.is_session_expired(SaicLogoutException("bye")),
}))
"""


class RealLibraryTests(unittest.TestCase):
    """errors.py against the real pinned mg-saic-client's errors."""

    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            [sys.executable, "-c", _REAL_LIBRARY,
             str(REPO_ROOT / "custom_components/mg_saic/errors.py")],
            capture_output=True, text=True, timeout=60,
        )
        cls.out = json.loads(result.stdout) if result.returncode == 0 else None
        cls.err = result.stderr

    def setUp(self):
        pinned = next(
            r for r in json.loads(
                (REPO_ROOT / "custom_components/mg_saic/manifest.json").read_text()
            )["requirements"] if r.startswith("mg-saic-client")
        )
        if self.out is None:
            if "No module named 'saic_ismart_client_ng'" in self.err:
                self.skipTest("mg-saic-client not installed")
            self.fail(f"library check failed:\n{self.err}")
        if f"mg-saic-client=={self.out['version']}" != pinned:
            self.skipTest(f"installed {self.out['version']}, manifest pins {pinned}")

    def test_unreachable(self):
        self.assertEqual(
            self.out["unreachable"],
            [True, False, "The remote control instruction failed, please try again later."],
        )

    def test_not_locked(self):
        self.assertEqual(self.out["not_locked"], [True, True, 8])

    def test_logout(self):
        self.assertTrue(self.out["logout"])


if __name__ == "__main__":
    unittest.main()
