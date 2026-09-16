"""Regression coverage for #374 (@stfvrg): SAIC return code 8 is ambiguous.

SAIC uses return code 8 for two unrelated rejections:
  * the real remote-command limit ("too frequent" / "maximum number of
    remote commands") -- the vehicle needs a physical key start to reset.
  * a command rejected because the vehicle isn't locked ("Vehicle not
    locked. Please lock it and try again.") -- the same command succeeds
    immediately once the vehicle is locked, no key start involved.

Before this fix, api.py mapped every "return code: 8" straight to
CommandsLimitReachedException regardless of the accompanying message, so
every "vehicle not locked" rejection was reported to the user as a command
limit with the wrong fix. This covers the new message-based classification
in api.py and the matching Command-Errors event/reason text in event.py.
"""

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_command_error_test"
LOADED_MODULE_NAMES = (
    "saic_ismart_client_ng",
    "saic_ismart_client_ng.model",
    "saic_ismart_client_ng.api",
    "saic_ismart_client_ng.api.vehicle_charging",
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.event",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.logic",
    f"{PACKAGE}.api",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.event",
)


def _module(name, **attributes):
    if name in sys.modules:
        module = sys.modules[name]
    else:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _stub_if_missing(name):
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = MagicMock()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_modules():
    previous_modules = {
        name: sys.modules[name] for name in LOADED_MODULE_NAMES if name in sys.modules
    }
    try:
        for name in (
            "saic_ismart_client_ng",
            "saic_ismart_client_ng.model",
            "saic_ismart_client_ng.api",
            "saic_ismart_client_ng.api.vehicle_charging",
        ):
            _stub_if_missing(name)

        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        components = _module("homeassistant.components")
        components.__path__ = []
        helpers = _module("homeassistant.helpers")
        helpers.__path__ = []

        class _EventEntity:
            pass

        class _CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

        _module("homeassistant.components.event", EventEntity=_EventEntity)
        _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=_CoordinatorEntity,
        )

        package = _module(PACKAGE)
        package.__path__ = [str(PKG_DIR)]

        const = _load(f"{PACKAGE}.const", PKG_DIR / "const.py")
        _load(f"{PACKAGE}.logic", PKG_DIR / "logic.py")
        api = _load(f"{PACKAGE}.api", PKG_DIR / "api.py")
        _module(f"{PACKAGE}.utils", create_device_info=lambda *_args: {})
        event = _load(f"{PACKAGE}.event", PKG_DIR / "event.py")

        return api, event, const
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


API, EVENT, CONST = _load_modules()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _client():
    """A SAICMGAPIClient with a fake, already-"logged in" saic_api, so
    _make_api_call's re-login path is never exercised -- only the exception
    classification in the except block is under test here."""
    client = API.SAICMGAPIClient("user@example.com", "hunter2")
    client.saic_api = MagicMock(is_logged_in=True)
    return client


class ApiCallClassificationTests(unittest.TestCase):
    def test_vehicle_not_locked_message_raises_its_own_exception(self):
        client = _client()

        async def boom(*_a, **_kw):
            raise Exception(
                "return code: 8, message: Vehicle not locked. "
                "Please lock it and try again.(2)"
            )

        with self.assertRaises(API.VehicleNotLockedException):
            _run(client._make_api_call(boom))

    def test_vehicle_not_locked_is_not_also_a_command_limit(self):
        client = _client()

        async def boom(*_a, **_kw):
            raise Exception(
                "return code: 8, message: Vehicle not locked. "
                "Please lock it and try again.(2)"
            )

        try:
            _run(client._make_api_call(boom))
            self.fail("expected VehicleNotLockedException")
        except API.CommandsLimitReachedException:
            self.fail(
                "vehicle-not-locked was misclassified as the command limit"
            )
        except API.VehicleNotLockedException:
            pass

    def test_generic_return_code_8_still_reads_as_the_command_limit(self):
        client = _client()

        async def boom(*_a, **_kw):
            raise Exception("return code: 8, message: too frequent")

        with self.assertRaises(API.CommandsLimitReachedException):
            _run(client._make_api_call(boom))

    def test_return_code_8_with_no_specific_message_defaults_to_command_limit(
        self,
    ):
        # We don't know every possible code-8 message SAIC can send; only
        # "vehicle not locked" is carved out, everything else keeps the
        # pre-#374 fail-safe behaviour of assuming the limit.
        client = _client()

        async def boom(*_a, **_kw):
            raise Exception("return code: 8, message: unexpected server text")

        with self.assertRaises(API.CommandsLimitReachedException):
            _run(client._make_api_call(boom))

    def test_unrelated_errors_are_unaffected(self):
        client = _client()

        async def boom(*_a, **_kw):
            raise Exception("return code: 4, message: The remote control instruction failed")

        with self.assertRaises(Exception) as ctx:
            _run(client._make_api_call(boom))
        self.assertNotIsInstance(ctx.exception, API.VehicleNotLockedException)
        self.assertNotIsInstance(ctx.exception, API.CommandsLimitReachedException)


class CommandErrorHumanizerVehicleNotLockedTests(unittest.TestCase):
    """event.py's _humanize_command_error must not conflate the two return-8
    meanings either, independent of which exception type raised it (e.g. if
    the raw SAIC string reaches record_command_error via some other path)."""

    def test_vehicle_not_locked_text_is_distinguished_from_command_limit(self):
        a = EVENT._humanize_command_error(
            "Error starting AC with settings",
            "return code: 8, message: Vehicle not locked. Please lock it "
            "and try again.(2)",
        )
        self.assertEqual(a["code"], 8)
        self.assertIn("not locked", a["reason"])
        self.assertNotIn("remote-command limit", a["reason"])

    def test_generic_code_8_without_the_specific_message_is_unaffected(self):
        a = EVENT._humanize_command_error("climate", "return code: 8, message: unknown")
        self.assertEqual(a["code"], 8)
        self.assertIn("remote-command limit", a["reason"])

    def test_too_frequent_still_maps_to_the_command_limit(self):
        # Pre-existing behaviour (#294-era), unaffected by this change.
        a = EVENT._humanize_command_error("climate", "operation too frequent")
        self.assertEqual(a["code"], 8)
        self.assertIn("remote-command limit", a["reason"])

    def test_notify_vehicle_not_locked_message_is_self_consistent(self):
        # The exact string notify_vehicle_not_locked (coordinator.py) passes
        # to record_command_error must itself be recognised by the
        # humanizer, the same way the other notify_* helpers' strings are.
        a = EVENT._humanize_command_error(
            "vehicle_lock_required",
            "Vehicle not locked: command rejected, lock the vehicle and "
            "try again",
        )
        self.assertEqual(a["code"], 8)
        self.assertIn("not locked", a["reason"])


class VehicleNotLockedEventTests(unittest.TestCase):
    """The Command-Errors event entity gets its own vehicle_not_locked type,
    distinct from both command_error and command_limit_reached, so it's
    flagged separately in the Logbook (per #374 request)."""

    def test_vehicle_not_locked_is_its_own_event_type(self):
        self.assertIn(EVENT.EVENT_TYPE_VEHICLE_NOT_LOCKED, EVENT.EVENT_TYPES)
        self.assertNotEqual(
            EVENT.EVENT_TYPE_VEHICLE_NOT_LOCKED, EVENT.EVENT_TYPE_COMMAND_ERROR
        )
        self.assertNotEqual(
            EVENT.EVENT_TYPE_VEHICLE_NOT_LOCKED,
            EVENT.EVENT_TYPE_COMMAND_LIMIT_REACHED,
        )

    def test_record_vehicle_not_locked_fires_the_dedicated_event_type(self):
        vin_info = MagicMock(vin="VIN1", brandName="MG", modelName="Test Vehicle")
        coordinator = MagicMock(vin_info=vin_info)
        entry = MagicMock(entry_id="entry-1")
        entity = EVENT.SAICMGCommandErrorEvent(
            coordinator, MagicMock(), entry, vin_info, "VIN1"
        )
        entity._trigger_event = MagicMock()
        entity.async_write_ha_state = MagicMock()

        entity.record_vehicle_not_locked("climate.set_hvac_mode")

        entity._trigger_event.assert_called_once()
        event_type, payload = entity._trigger_event.call_args.args
        self.assertEqual(event_type, EVENT.EVENT_TYPE_VEHICLE_NOT_LOCKED)
        self.assertEqual(payload["code"], 8)
        self.assertEqual(payload["source"], "climate.set_hvac_mode")
        self.assertIn("not locked", payload["reason"].lower())
        entity.async_write_ha_state.assert_called_once()


class StopAcVerifyAfterFailureTests(unittest.TestCase):
    """stop_ac's error can be spurious: SAIC's server can report a failure
    for a command that genuinely reached the vehicle and took effect --
    confirmed directly from a user's log (#262, Harry), where the identical
    error appeared on every attempt yet remoteClimateStatus reliably
    transitioned to 0 (off) a short while later regardless. Scoped to
    stop_ac specifically, the only command this has been observed on."""

    def _client_with(self, *, stop_ac_error, status_after):
        """A client whose underlying saic_api.stop_ac always raises
        stop_ac_error, and whose get_vehicle_status (used for the
        verification check) returns a status with remoteClimateStatus set
        to status_after (or raises, if status_after is an Exception)."""
        client = _client()

        async def boom(*_a, **_kw):
            raise stop_ac_error

        client.saic_api.stop_ac = boom

        if isinstance(status_after, Exception):
            async def get_status(*_a, **_kw):
                raise status_after

            client.saic_api.get_vehicle_status = get_status
        else:
            from types import SimpleNamespace

            async def get_status(*_a, **_kw):
                return SimpleNamespace(
                    basicVehicleStatus=SimpleNamespace(
                        remoteClimateStatus=status_after
                    )
                )

            client.saic_api.get_vehicle_status = get_status

        return client

    def _run_stop_ac_without_the_real_delay(self, client, vin="VIN1"):
        # STOP_AC_VERIFY_DELAY_SECONDS is a real 10s wait in production;
        # patching asyncio.sleep on the loaded api module keeps these tests
        # fast without changing what's actually being exercised.
        from unittest.mock import AsyncMock, patch

        with patch.object(API.asyncio, "sleep", AsyncMock()):
            return _run(client.stop_ac(vin))

    def test_error_followed_by_confirmed_off_status_is_treated_as_success(self):
        client = self._client_with(
            stop_ac_error=Exception(
                "return code: 500, message: API call POST /vehicle/control "
                "failed unexpectedly"
            ),
            status_after=0,
        )
        # Must NOT raise -- this is the exact bug (#262).
        self._run_stop_ac_without_the_real_delay(client)

    def test_error_followed_by_still_running_status_still_raises(self):
        client = self._client_with(
            stop_ac_error=Exception("return code: 500, message: unexpected"),
            status_after=2,  # car is still genuinely running -- a real failure
        )
        with self.assertRaises(Exception):
            self._run_stop_ac_without_the_real_delay(client)

    def test_verification_check_itself_failing_still_raises_the_original_error(self):
        client = self._client_with(
            stop_ac_error=Exception("return code: 500, message: unexpected"),
            status_after=Exception("network error during verification"),
        )
        with self.assertRaises(Exception) as ctx:
            self._run_stop_ac_without_the_real_delay(client)
        self.assertIn("return code: 500", str(ctx.exception))

    def test_command_limit_is_not_second_guessed_by_verification(self):
        # A genuine command-limit rejection has its own clear meaning and
        # fix (physical key start) -- must not be silently reinterpreted via
        # a status check, and must not even attempt one.
        client = self._client_with(
            stop_ac_error=Exception("return code: 8, message: too frequent"),
            status_after=0,
        )
        client.saic_api.get_vehicle_status = None  # would blow up if ever called
        with self.assertRaises(API.CommandsLimitReachedException):
            self._run_stop_ac_without_the_real_delay(client)

    def test_vehicle_not_locked_is_not_second_guessed_by_verification(self):
        client = self._client_with(
            stop_ac_error=Exception(
                "return code: 8, message: Vehicle not locked. "
                "Please lock it and try again.(2)"
            ),
            status_after=0,
        )
        client.saic_api.get_vehicle_status = None  # would blow up if ever called
        with self.assertRaises(API.VehicleNotLockedException):
            self._run_stop_ac_without_the_real_delay(client)

    def test_success_on_the_first_attempt_never_triggers_verification(self):
        client = _client()

        async def ok(*_a, **_kw):
            return None

        client.saic_api.stop_ac = ok
        client.saic_api.get_vehicle_status = None  # would blow up if ever called
        self._run_stop_ac_without_the_real_delay(client)  # must not raise


if __name__ == "__main__":
    unittest.main()
