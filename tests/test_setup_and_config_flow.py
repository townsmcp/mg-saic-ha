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
SETUP = _load("mg_saic.setup_module", PKG_DIR / "__init__.py")
CF = _load("mg_saic.config_flow", PKG_DIR / "config_flow.py", force=True)

DOMAIN = sys.modules["mg_saic.const"].DOMAIN


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeHass:
    def __init__(self):
        self.data = {}


class _FakeEntry:
    def __init__(self, data):
        self.data = data
        self.entry_id = "test_entry"
        self.options = {}


class _FailingBackend:
    """Backend whose login always fails; records whether close() ran."""

    def __init__(self):
        self.closed = False

    async def login(self):
        raise RuntimeError("bad credentials")

    async def close(self):
        self.closed = True


class TestLoginFailurePath(unittest.TestCase):
    """Issue #230: login failure must raise ConfigEntryNotReady cleanly."""

    def test_login_failure_raises_not_ready_and_cleans_up(self):
        hass = _FakeHass()
        entry = _FakeEntry(
            {
                "username": "user@example.com",
                "password": "wrong",
                "region": "EU",
                "vin": "TESTVIN123",
            }
        )
        backend = _FailingBackend()
        original = SETUP.create_backend
        SETUP.create_backend = lambda data: backend
        try:
            with self.assertRaises(_ConfigEntryNotReady) as ctx:
                _run(SETUP.async_setup_entry(hass, entry))
        finally:
            SETUP.create_backend = original

        # No secondary KeyError; the original cause is chained and readable.
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)
        self.assertIn("bad credentials", str(ctx.exception.__cause__))
        # The failed client was closed (no aiohttp session leak)…
        self.assertTrue(backend.closed)
        # …and nothing was retained in the shared client map.
        self.assertEqual(hass.data[DOMAIN].get("account_clients"), {})


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
        self.assertEqual(flow.vehicle_labels["VININDIA0001"], "MG Comet EV (…0001)")
        self.assertEqual(flow.vehicle_labels["VININDIA0002"], "MG Comet EV (…0002)")
        self.assertNotEqual(
            flow.vehicle_labels["VININDIA0001"],
            flow.vehicle_labels["VININDIA0002"],
        )

    def test_series_used_when_model_missing(self):
        flow, _ = self._flow_after_pin(
            [_FakeIndiaVehicle("VININDIA0003", series="Comet")]
        )
        self.assertEqual(flow.vehicle_labels["VININDIA0003"], "Comet (…0003)")

    def test_metadata_free_vehicle_falls_back_to_vin(self):
        flow, result = self._flow_after_pin([_FakeIndiaVehicle("VININDIA0004")])
        self.assertEqual(result["step_id"], "select_vehicle")
        self.assertEqual(flow.vehicle_labels["VININDIA0004"], "VININDIA0004")

    def test_plain_string_vins_still_accepted(self):
        # The backend contract tolerates plain VIN strings.
        flow, _ = self._flow_after_pin(["VININDIA0005"])
        self.assertEqual(flow.vehicles, ["VININDIA0005"])
        self.assertEqual(flow.vehicle_labels["VININDIA0005"], "VININDIA0005")


if __name__ == "__main__":
    unittest.main()
