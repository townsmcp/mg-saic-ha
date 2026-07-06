# File: tests/test_backends.py
#
# Unit tests for the backend abstraction layer (custom_components/mg_saic/
# backends/) introduced for MG India support — see Discussion #169.
#
# These tests run in plain CPython (no Home Assistant installed), matching
# the python-tests.yaml CI workflow.  Modules that api.py/const.py import at
# module level (saic-ismart-client-ng, voluptuous, homeassistant helpers) are
# stubbed before the integration modules are loaded, and the mg_saic package
# is registered manually so importing it does NOT execute __init__.py (which
# requires a full Home Assistant runtime).
#
# Guarded invariants:
#   * The India PIN hash algorithm (md5(pin + "00"), uppercased) and its
#     input validation — a silent change here would break every India
#     user's commands.
#   * Backend selection: region "India" -> IndiaBackend, everything else ->
#     the untouched global SAICMGAPIClient.
#   * Capability sets: charging/alarm families must stay OUT of
#     INDIA_FEATURES until confirmed on a real car; confirmed features must
#     stay IN.
#   * Legacy fallback: clients that declare no feature set are treated as
#     fully featured (pre-split global behaviour).

import asyncio
import hashlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"


def _stub(name):
    """Register a MagicMock module stub if *name* is not importable."""
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = MagicMock()


def _load(name, path):
    """Load a module from *path* under *name* without touching __init__.py."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Stub third-party/runtime-only imports used at module level by the
# integration modules we load below.
for _name in (
    "saic_ismart_client_ng",
    "saic_ismart_client_ng.model",
    "saic_ismart_client_ng.api",
    "saic_ismart_client_ng.api.vehicle_charging",
    "voluptuous",
    "homeassistant",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
):
    _stub(_name)

# Register mg_saic as a package pointing at its directory WITHOUT executing
# custom_components/mg_saic/__init__.py (which imports Home Assistant).
if "mg_saic" not in sys.modules:
    _pkg = types.ModuleType("mg_saic")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["mg_saic"] = _pkg

_load("mg_saic.const", PKG_DIR / "const.py")
_load("mg_saic.logic", PKG_DIR / "logic.py")
_load("mg_saic.api", PKG_DIR / "api.py")
backends = _load("mg_saic.backends", PKG_DIR / "backends" / "__init__.py")
sys.modules["mg_saic.backends"].__path__ = [str(PKG_DIR / "backends")]
india = _load("mg_saic.backends.india", PKG_DIR / "backends" / "india.py")

Feature = backends.Feature


def _run(coro):
    """Run a coroutine to completion on a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestIndiaPinHash(unittest.TestCase):
    """The PIN hash algorithm agreed with John Lazarus (Discussion #169)."""

    def test_known_vector(self):
        # Cross-check vector shared with the mg-ismart-india-client tests.
        self.assertEqual(
            india.hash_india_pin("1234"),
            "3FA6E1540A9E5B94313C5907267A7331",
        )

    def test_algorithm_definition(self):
        for pin in ("0000", "1234", "9999", "0420"):
            expected = hashlib.md5(f"{pin}00".encode()).hexdigest().upper()
            self.assertEqual(india.hash_india_pin(pin), expected)

    def test_output_is_uppercase(self):
        digest = india.hash_india_pin("5678")
        self.assertEqual(digest, digest.upper())

    def test_whitespace_is_stripped(self):
        self.assertEqual(
            india.hash_india_pin(" 1234 "), india.hash_india_pin("1234")
        )

    def test_invalid_pins_rejected(self):
        for bad in ("123", "12345", "12a4", "", "abcd", "12.4"):
            with self.assertRaises(ValueError, msg=f"accepted {bad!r}"):
                india.hash_india_pin(bad)


class TestBackendSelection(unittest.TestCase):
    """create_backend() routes each config entry to exactly one backend."""

    def test_india_region_gets_india_backend(self):
        client = backends.create_backend(
            {
                "region": "India",
                "username": "user",
                "password": "pw",
                "vin": "VIN1",
                "india_pin_hash": "ABC",
            }
        )
        self.assertIsInstance(client, india.IndiaBackend)
        self.assertEqual(client.pin_hash, "ABC")
        self.assertEqual(client.supported_features, backends.INDIA_FEATURES)

    def test_other_regions_get_global_client(self):
        for region in ("EU", "Australia", "Brazil", "Israel", "Thailand"):
            client = backends.create_backend(
                {"region": region, "username": "u", "password": "p"}
            )
            self.assertEqual(
                type(client).__name__, "SAICMGAPIClient", f"region {region}"
            )
            self.assertEqual(
                client.supported_features,
                backends.GLOBAL_FEATURES,
                f"region {region}",
            )

    def test_custom_region_parameters_passed_through(self):
        client = backends.create_backend(
            {
                "region": "Custom",
                "username": "u",
                "password": "p",
                "custom_base_uri": "https://example.invalid/",
                "region_code": "th",
                "tenant_id": "t1",
            }
        )
        self.assertEqual(client.custom_base_uri, "https://example.invalid/")
        self.assertEqual(client.tenant_id, "t1")

    def test_email_login_inferred_from_missing_country_code(self):
        client = backends.create_backend(
            {"region": "EU", "username": "u@example.com", "password": "p"}
        )
        self.assertTrue(client.username_is_email)
        client = backends.create_backend(
            {
                "region": "EU",
                "username": "5550001111",
                "password": "p",
                "country_code": "44",
            }
        )
        self.assertFalse(client.username_is_email)


class TestCapabilitySets(unittest.TestCase):
    """Capability declarations — the 'never ship unconfirmed' safety rule."""

    # Confirmed on-car by John Lazarus for MG India (Discussion #169).
    INDIA_CONFIRMED = {
        Feature.STATUS,
        Feature.LOCK,
        Feature.TAILGATE,
        Feature.WINDOWS,
        Feature.SUNROOF,
        Feature.CLIMATE,
        Feature.HEATED_SEATS,
        Feature.FIND_MY_CAR,
    }

    # Must remain absent until decoded AND confirmed on a real India car.
    INDIA_FORBIDDEN = {
        Feature.CHARGING_DATA,
        Feature.CHARGING_CONTROL,
        Feature.CHARGING_PORT_LOCK,
        Feature.SCHEDULED_CHARGING,
        Feature.BATTERY_HEATING,
        Feature.TARGET_SOC,
        Feature.CURRENT_LIMIT,
        Feature.ALARM_MESSAGES,
    }

    def test_global_backend_is_fully_featured(self):
        self.assertEqual(backends.GLOBAL_FEATURES, frozenset(Feature))

    def test_india_confirmed_features_present(self):
        self.assertTrue(self.INDIA_CONFIRMED <= backends.INDIA_FEATURES)

    def test_india_unconfirmed_features_absent(self):
        leaked = self.INDIA_FORBIDDEN & backends.INDIA_FEATURES
        self.assertFalse(
            leaked,
            "Unconfirmed feature(s) leaked into INDIA_FEATURES — these must "
            f"not ship until confirmed on a real car: {sorted(f.value for f in leaked)}",
        )

    def test_backend_supports_matches_declarations(self):
        client = india.IndiaBackend(username="u", password="p")
        for feature in self.INDIA_CONFIRMED:
            self.assertTrue(backends.backend_supports(client, feature))
        for feature in self.INDIA_FORBIDDEN:
            self.assertFalse(backends.backend_supports(client, feature))

    def test_legacy_clients_treated_as_fully_featured(self):
        class LegacyClient:
            pass

        for feature in Feature:
            self.assertTrue(backends.backend_supports(LegacyClient(), feature))


class TestIndiaBackendStub(unittest.TestCase):
    """Until the TAP client is wired in, the stub must fail loudly & clearly.

    John's implementation replaces the method bodies; these tests then get
    superseded by real behaviour tests in his PR.
    """

    def setUp(self):
        self.backend = india.IndiaBackend(
            username="u", password="p", vin="VIN1", pin_hash="ABC"
        )

    def test_status_raises_not_ready(self):
        with self.assertRaises(india.IndiaBackendNotReadyError) as ctx:
            _run(self.backend.get_vehicle_status())
        self.assertIn("169", str(ctx.exception))

    def test_login_raises_not_ready(self):
        with self.assertRaises(india.IndiaBackendNotReadyError):
            _run(self.backend.login())

    def test_close_is_safe(self):
        # close() must never raise — it runs during teardown paths.
        _run(self.backend.close())


if __name__ == "__main__":
    unittest.main()
