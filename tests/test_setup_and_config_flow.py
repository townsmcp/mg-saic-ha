# File: tests/test_setup_and_config_flow.py
#
# Regression tests for issue #230 (login-failure exception path in
# async_setup_entry) and issue #229 (India vehicle selector labels).
#
# Uses the same technique as tests/test_backends.py — Home Assistant and
# third-party modules are stubbed so the integration modules load in plain
# CPython — but with functional stub classes (rather than MagicMocks) where
# the code under test subclasses or raises them.  Stubs are installed
# unconditionally because tests/test_backends.py may already have installed
# MagicMock versions in the same process; ours must win for these tests.

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"


# ── Functional Home Assistant stubs ──────────────────────────────────────────

class _ConfigEntryNotReady(Exception):
    """Stub of homeassistant.config_entries.ConfigEntryNotReady."""


class _ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        pass

    def async_show_form(self, step_id=None, data_schema=None, errors=None, **kw):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors or {},
        }

    def async_abort(self, reason=None):
        return {"type": "abort", "reason": reason}

    def async_create_entry(self, title=None, data=None, options=None):
        return {"type": "create_entry", "title": title, "data": data}


class _OptionsFlow:
    def __init_subclass__(cls, **kwargs):
        pass


class _DataUpdateCoordinator:
    def __init_subclass__(cls, **kwargs):
        pass

    def __init__(self, *args, **kwargs):
        pass


def _install_stubs():
    for name in (
        "saic_ismart_client_ng",
        "saic_ismart_client_ng.model",
        "saic_ismart_client_ng.api",
        "saic_ismart_client_ng.api.vehicle_charging",
        "homeassistant.helpers",
        "homeassistant.helpers.config_validation",
        "homeassistant.helpers.event",
        "homeassistant.util",
        "homeassistant.util.dt",
        "homeassistant.exceptions",
    ):
        sys.modules[name] = MagicMock()

    ha = types.ModuleType("homeassistant")
    ha.__path__ = []
    sys.modules["homeassistant"] = ha

    ce = types.ModuleType("homeassistant.config_entries")
    ce.ConfigFlow = _ConfigFlow
    ce.OptionsFlow = _OptionsFlow
    ce.ConfigEntry = object
    ce.ConfigEntryNotReady = _ConfigEntryNotReady
    sys.modules["homeassistant.config_entries"] = ce

    core = types.ModuleType("homeassistant.core")
    core.callback = lambda f: f
    core.HomeAssistant = object
    core.ServiceCall = object
    sys.modules["homeassistant.core"] = core

    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    uc.DataUpdateCoordinator = _DataUpdateCoordinator
    uc.UpdateFailed = type("UpdateFailed", (Exception,), {})
    uc.CoordinatorEntity = _DataUpdateCoordinator
    sys.modules["homeassistant.helpers.update_coordinator"] = uc

    # Minimal voluptuous stand-in: enough for schema construction at import
    # and step execution time.
    vol = types.ModuleType("voluptuous")

    class _Marker(str):
        def __new__(cls, key, **kw):
            return super().__new__(cls, key)

    vol.Required = _Marker
    vol.Optional = _Marker
    vol.Schema = lambda d, **kw: d
    vol.In = lambda x: x
    vol.All = lambda *a: a
    vol.Any = lambda *a: a
    vol.Coerce = lambda t: t
    vol.Range = lambda **kw: kw
    sys.modules["voluptuous"] = vol


def _load(name, path, force=False):
    if name in sys.modules and not force:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()

if "mg_saic" not in sys.modules or not hasattr(sys.modules["mg_saic"], "__path__"):
    _pkg = types.ModuleType("mg_saic")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["mg_saic"] = _pkg

_load("mg_saic.const", PKG_DIR / "const.py")
_load("mg_saic.logic", PKG_DIR / "logic.py")
_load("mg_saic.api", PKG_DIR / "api.py")
_load("mg_saic.backends", PKG_DIR / "backends" / "__init__.py")
sys.modules["mg_saic.backends"].__path__ = [str(PKG_DIR / "backends")]
_load("mg_saic.backends.india", PKG_DIR / "backends" / "india.py")
_load("mg_saic.utils", PKG_DIR / "utils.py")
_load("mg_saic.coordinator", PKG_DIR / "coordinator.py", force=True)
_load("mg_saic.message_poller", PKG_DIR / "message_poller.py", force=True)
_load("mg_saic.services", PKG_DIR / "services.py", force=True)
CF = _load("mg_saic.config_flow", PKG_DIR / "config_flow.py", force=True)
SETUP = _load("mg_saic.setup_module", PKG_DIR / "__init__.py")



class _FakeHass:
    def __init__(self):
        self.data = {}


class _FakeEntry:
    def __init__(self, data):
        self.data = data
        self.entry_id = "test_entry"
        self.options = {}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()




class _FakeIndiaVehicle:
    def __init__(self, vin, model=None, series=None):
        self.vin = vin
        if model is not None:
            self.modelName = model
        if series is not None:
            self.series = series


class _FakeIndiaBackend:
    def __init__(self, vehicles):
        self._vehicles = vehicles

    async def login(self):
        return True

    async def get_vehicle_info(self):
        return self._vehicles

    async def close(self):
        pass


class TestIndiaVehicleLabels(unittest.TestCase):
    """Issue #229: the India selector shows model labels, stores VINs."""

    def _flow_after_pin(self, vehicles):
        flow = CF.SAICMGConfigFlow()
        flow.login_type = "email"
        flow.username = "user@example.com"
        flow.password = "pw"
        flow.region = "India"
        flow.hass = MagicMock()
        flow.hass.config_entries.async_entries.return_value = []
        original = CF.create_backend
        CF.create_backend = lambda data: _FakeIndiaBackend(vehicles)
        try:
            result = _run(flow.async_step_india_pin({"pin": "1234"}))
        finally:
            CF.create_backend = original
        return flow, result

    def test_model_labels_shown_and_vins_stored(self):
        flow, result = self._flow_after_pin(
            [
                _FakeIndiaVehicle("VININDIA0001", model="MG Comet EV"),
                _FakeIndiaVehicle("VININDIA0002", model="MG Comet EV"),
            ]
        )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["step_id"], "select_vehicle")
        # Option values are still the VINs…
        self.assertEqual(flow.vehicles, ["VININDIA0001", "VININDIA0002"])
        # …and both labels carry the model plus a masked, distinguishing
        # VIN suffix (no full VIN in the label).
        self.assertEqual(flow.vehicle_options["VININDIA0001"], "MG Comet EV (…A0001)")
        self.assertEqual(flow.vehicle_options["VININDIA0002"], "MG Comet EV (…A0002)")
        self.assertNotEqual(
            flow.vehicle_options["VININDIA0001"],
            flow.vehicle_options["VININDIA0002"],
        )

    def test_series_used_when_model_missing(self):
        flow, _ = self._flow_after_pin(
            [_FakeIndiaVehicle("VININDIA0003", series="Comet")]
        )
        self.assertEqual(flow.vehicle_options["VININDIA0003"], "Comet (…A0003)")

    def test_metadata_free_vehicle_falls_back_to_vin(self):
        flow, result = self._flow_after_pin([_FakeIndiaVehicle("VININDIA0004")])
        self.assertEqual(result["step_id"], "select_vehicle")
        self.assertEqual(flow.vehicle_options["VININDIA0004"], "VININDIA0004")

    def test_plain_string_vins_still_accepted(self):
        # The backend contract tolerates plain VIN strings.
        flow, _ = self._flow_after_pin(["VININDIA0005"])
        self.assertEqual(flow.vehicles, ["VININDIA0005"])
        self.assertEqual(flow.vehicle_options["VININDIA0005"], "VININDIA0005")


if __name__ == "__main__":
    unittest.main()


# ── Issue #233 / #234 regression tests ───────────────────────────────────────

import logging as _logging

from mg_saic.api import SAICMGAPIClient


class _FakeHttpxClient:
    """Stand-in for httpx.AsyncClient with the attributes close() inspects."""

    def __init__(self):
        self.is_closed = False

    async def aclose(self):
        self.is_closed = True


class _FakeInnerApiClient:
    """Stand-in for SaicApiClient; owns the httpx client under the mangled name."""

    def __init__(self, http_client):
        self._SaicApiClient__client = http_client


class _FakeSaicApi:
    """Stand-in for SaicApi: no public close(), private transport reachable."""

    def __init__(self, http_client):
        self._AbstractSaicApi__api_client = _FakeInnerApiClient(http_client)


class TestGlobalClientClose(unittest.TestCase):
    """Issue #233: close() must actually close the underlying httpx transport."""

    def _make_client(self, http_client):
        client = SAICMGAPIClient.__new__(SAICMGAPIClient)
        client.saic_api = _FakeSaicApi(http_client)
        return client

    def test_close_closes_private_transport(self):
        http = _FakeHttpxClient()
        client = self._make_client(http)
        _run(client.close())
        # The real transport is closed, not merely "close() was called".
        self.assertTrue(http.is_closed)
        # Reference is dropped so a second close() is a safe no-op.
        self.assertIsNone(client.saic_api)

    def test_close_is_idempotent(self):
        http = _FakeHttpxClient()
        client = self._make_client(http)
        _run(client.close())
        _run(client.close())  # must not raise
        self.assertTrue(http.is_closed)

    def test_close_prefers_public_method_when_present(self):
        calls = []

        class _WithPublicClose:
            async def close(self):
                calls.append("public")

        client = SAICMGAPIClient.__new__(SAICMGAPIClient)
        client.saic_api = _WithPublicClose()
        _run(client.close())
        self.assertEqual(calls, ["public"])
        self.assertIsNone(client.saic_api)

    def test_none_api_is_safe(self):
        client = SAICMGAPIClient.__new__(SAICMGAPIClient)
        client.saic_api = None
        _run(client.close())  # must not raise


class TestLoginFailureLogPrivacy(unittest.TestCase):
    """Issue #234: identifiers must not appear in the error-level log line."""

    def test_mask_helpers(self):
        self.assertEqual(SETUP._mask_vin("LSJW00000000A1234"), "…1234")
        self.assertEqual(SETUP._mask_vin("abc"), "****")
        self.assertEqual(SETUP._mask_vin(None), "****")
        self.assertEqual(
            SETUP._mask_account(("driver@example.com", "India")),
            "(d***@example.com, India)",
        )
        # No full username or VIN survives masking.
        masked = SETUP._mask_account(("john.smith@gmail.com", "EU"))
        self.assertNotIn("john.smith", masked)

    def test_error_log_excludes_identifiers_and_raw_exception(self):
        hass = _FakeHass()
        entry = _FakeEntry(
            {
                "username": "secretuser@example.com",
                "password": "wrong",
                "region": "EU",
                "vin": "LSJSECRETVIN12345",
            }
        )

        class _FailingClient:
            async def login(self):
                raise RuntimeError("server said: token=SUPERSECRET123")

            async def close(self):
                pass

        original = SETUP.create_backend
        SETUP.create_backend = lambda data: _FailingClient()
        records = []
        handler = _RecordingHandler(records)
        # Attach to the exact LOGGER object the module logs through, rather
        # than guessing its name (it is getLogger(__package__), which differs
        # under the test harness).
        setup_logger = SETUP.LOGGER
        setup_logger.addHandler(handler)
        setup_logger.setLevel(_logging.DEBUG)
        try:
            with self.assertRaises(_ConfigEntryNotReady):
                _run(SETUP.async_setup_entry(hass, entry))
        finally:
            SETUP.create_backend = original
            setup_logger.removeHandler(handler)

        error_text = " ".join(
            r.getMessage() for r in records if r.levelno >= _logging.ERROR
        )
        self.assertIn("Failed to log in", error_text)
        self.assertNotIn("secretuser@example.com", error_text)
        self.assertNotIn("LSJSECRETVIN12345", error_text)
        self.assertNotIn("SUPERSECRET123", error_text)  # raw exception text
        self.assertNotIn("server said", error_text)


class _RecordingHandler(_logging.Handler):
    def __init__(self, sink):
        super().__init__()
        self._sink = sink

    def emit(self, record):
        self._sink.append(record)


class TestUnreachableCode4Propagation(unittest.TestCase):
    """Regression tests for issue #238.

    The Vehicle Reachability sensor stayed on 'Awake' after a failed status
    poll because api.get_vehicle_status / get_charging_info caught the SAIC
    'return code: 4' (car unreachable) exception and returned None. The
    coordinator then only ever saw a generic "status is None" error and never
    recognised the unreachable condition, so it never flagged reachability.

    These tests pin the corrected contract: a return-code-4 failure must
    propagate (so the coordinator's retry handler can flag it), while any
    other failure still degrades to None as before.
    """

    def setUp(self):
        api = sys.modules["mg_saic.api"]
        self.client = api.SAICMGAPIClient("user@example.com", "pw", vin="VINTEST123")
        # saic_api is referenced when building the call arguments; a bare mock
        # is enough since _make_api_call is replaced in each test.
        self.client.saic_api = MagicMock()

    @staticmethod
    def _run(coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def _patch_make_api_call(self, exc):
        async def _boom(*args, **kwargs):
            raise exc
        self.client._make_api_call = _boom

    def test_status_propagates_code_4(self):
        self._patch_make_api_call(
            Exception("return code: 4, message: The remote control instruction failed")
        )
        with self.assertRaises(Exception) as ctx:
            self._run(self.client.get_vehicle_status("VINTEST123"))
        self.assertIn("return code: 4", str(ctx.exception))

    def test_charging_propagates_code_4(self):
        self._patch_make_api_call(
            Exception("return code: 4, message: The remote control instruction failed")
        )
        with self.assertRaises(Exception) as ctx:
            self._run(self.client.get_charging_info("VINTEST123"))
        self.assertIn("return code: 4", str(ctx.exception))

    def test_status_other_error_returns_none(self):
        self._patch_make_api_call(Exception("some transient network blip"))
        self.assertIsNone(self._run(self.client.get_vehicle_status("VINTEST123")))

    def test_charging_other_error_returns_none(self):
        self._patch_make_api_call(Exception("some transient network blip"))
        self.assertIsNone(self._run(self.client.get_charging_info("VINTEST123")))


class TestClimateFanSpeedSafeValues(unittest.TestCase):
    """Regression tests for issue #243.

    On the SAIC climate protocol the fan-speed byte values 4 and 5 are not
    higher fan speeds — on MG-family cars they trigger heating / front-defrost.
    Sending 5 as "High" put an unprofiled MG4 EV URBAN (series AH4EM, which
    falls back to DEFAULT_VEHICLE_PROFILE) into front defrost and made it report
    remoteClimateStatus=5, which the integration read as defrost rather than
    cooling. The fan-speed slider for the fixed profiles must therefore stay
    within the safe 1/2/3 range.
    """

    SAFE = {1, 2, 3}

    def _fan_values(self, profile):
        return [
            profile[k]
            for k in ("fan_speed_low", "fan_speed_medium", "fan_speed_high")
            if k in profile
        ]

    def test_default_profile_fan_speeds_are_safe(self):
        const = sys.modules["mg_saic.const"]
        vals = self._fan_values(const.DEFAULT_VEHICLE_PROFILE)
        self.assertTrue(vals, "default profile should define fan speeds")
        for v in vals:
            self.assertIn(v, self.SAFE, f"unsafe fan byte {v} in DEFAULT profile")

    def test_eh32_profile_fan_speeds_are_safe(self):
        const = sys.modules["mg_saic.const"]
        vals = self._fan_values(const.VEHICLE_PROFILES["EH32"])
        self.assertTrue(vals, "EH32 profile should define fan speeds")
        for v in vals:
            self.assertIn(v, self.SAFE, f"unsafe fan byte {v} in EH32 profile")


class TestMG4UrbanProfile(unittest.TestCase):
    """Issue #243: MG4 EV URBAN (series AH4EM) profile.

    Confirmed by owner testing that this variant uses the mode_select scheme
    (the fan byte is a mode the car echoes back as remoteClimateStatus), with
    no heat mode. Pin the confirmed value maps so a future edit can't silently
    revert them.
    """

    def _profile(self):
        return sys.modules["mg_saic.const"].VEHICLE_PROFILES["AH4EM"]

    def test_uses_mode_select_scheme(self):
        self.assertEqual(self._profile()["climate_control_scheme"], "mode_select")

    def test_confirmed_status_maps(self):
        p = self._profile()
        self.assertEqual(p["climate_status_fan_only"], {1})
        # Only mode 3 is a confirmed cool value on this car; 2 is deliberately
        # excluded (unconfirmed, and heat on the sister MG4 — PR #173 / #243).
        self.assertEqual(p["climate_status_cool"], {3})
        self.assertEqual(p["climate_status_defrost"], {5})

    def test_confirmed_mode_values(self):
        p = self._profile()
        self.assertEqual(p["climate_mode_fan_only"], 1)
        self.assertEqual(p["climate_mode_cool"], 3)
        self.assertEqual(p["climate_mode_defrost"], 5)

    def test_does_not_send_unconfirmed_mode_2_for_cool(self):
        # Guard against regressing to the unconfirmed 2=cool assumption, which
        # could heat the cabin when the user asks for cool (see PR #173).
        p = self._profile()
        self.assertNotEqual(p["climate_mode_cool"], 2)
        self.assertNotIn(2, p["climate_status_cool"])

    def test_no_heat_mode(self):
        # No heat status means the climate entity must not offer HVAC heat.
        p = self._profile()
        self.assertNotIn("climate_status_heat", p)
        self.assertNotIn("climate_mode_heat", p)


class TestMG4HeatProfile(unittest.TestCase):
    """MG4 (EH32) PTC heat support — ported from PR #173 (kindel0).

    The standard MG4 heats via a PTC resistive heater, triggered with the
    compressor off and the AUTO fan value. remoteClimateStatus 2 = heat,
    3 = cool (confirmed from decrypted iSmart traffic + live telemetry).
    """

    def _profile(self):
        return sys.modules["mg_saic.const"].VEHICLE_PROFILES["EH32"]

    def test_status_map(self):
        p = self._profile()
        self.assertEqual(p["climate_status_heat"], {2})
        self.assertEqual(p["climate_status_cool"], {3})
        self.assertEqual(p["climate_status_fan_only"], {4})

    def test_is_fan_speed_scheme(self):
        # EH32 stays a fan-speed car (not mode_select like the URBAN); the
        # heat command lives in the fan_speed path.
        p = self._profile()
        self.assertNotEqual(p.get("climate_control_scheme", "fan_speed"), "mode_select")

    def test_fan_speeds_still_safe(self):
        # Heat uses fan byte 2 with the compressor off; the cooling slider must
        # still avoid the unsafe 4/5 values (#243).
        p = self._profile()
        for k in ("fan_speed_low", "fan_speed_medium", "fan_speed_high"):
            self.assertIn(p[k], {1, 2, 3})
