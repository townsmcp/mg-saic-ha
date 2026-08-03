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
        self.assertEqual(india.hash_india_pin(" 1234 "), india.hash_india_pin("1234"))

    def test_invalid_pins_rejected(self):
        for bad in ("123", "12345", "123456", "12a4", "", "abcd", "12.4"):
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


class TestIndiaBackendAdapter(unittest.TestCase):
    def setUp(self):
        self.backend = india.IndiaBackend(
            username="9999999999", password="p", vin="VIN1", pin_hash="ABC"
        )
        self.fake = FakeIndiaClient()
        self.backend._client = self.fake

    def tearDown(self):
        _run(self.backend.close())

    def test_login_delegates_to_client(self):
        _run(self.backend.login())
        self.assertTrue(self.fake.logged_in)

    def test_vehicle_info_is_saic_shaped(self):
        vehicle = _run(self.backend.get_vehicle_info())[0]
        self.assertEqual(vehicle.vin, "VIN1")
        self.assertEqual(vehicle.brandName, "MG")
        self.assertEqual(vehicle.modelName, "Comet EV")
        configs = {
            item.itemCode: item.itemValue for item in vehicle.vehicleModelConfiguration
        }
        self.assertEqual(configs["LRD"], "1")
        self.assertEqual(configs["EV"], "1")
        self.assertEqual(configs["BType"], "1")

    def test_status_is_saic_shaped_and_validator_safe(self):
        status = _run(self.backend.get_vehicle_status("VIN1"))
        self.assertGreater(status.statusTime, 1_700_000_000)
        self.assertEqual(status.basicVehicleStatus.lockStatus, 1)
        self.assertEqual(status.basicVehicleStatus.driverDoor, 0)
        self.assertEqual(status.basicVehicleStatus.remoteClimateStatus, 2)
        self.assertEqual(status.basicVehicleStatus.mileage, 12345)
        self.assertEqual(status.basicVehicleStatus.fuelRangeElec, 850)
        self.assertEqual(status.basicVehicleStatus.batteryVoltage, 124)
        self.assertFalse(
            status.basicVehicleStatus.fuelRange == 0
            and status.basicVehicleStatus.fuelRangeElec == 0
            and status.basicVehicleStatus.mileage == 0
        )

    def test_controls_delegate_to_client(self):
        _run(self.backend.lock_vehicle("VIN1"))
        _run(self.backend.unlock_vehicle("VIN1"))
        _run(self.backend.open_tailgate("VIN1"))
        _run(self.backend.control_windows("VIN1", "open"))
        _run(self.backend.control_windows("VIN1", "close"))
        with self.assertRaisesRegex(Exception, "ventilate not confirmed"):
            _run(self.backend.control_windows("VIN1", "ventilate"))
        _run(self.backend.control_sunroof("VIN1", "open"))
        _run(self.backend.start_ac("VIN1"))
        _run(self.backend.stop_ac("VIN1"))
        _run(self.backend.get_vehicle_status("VIN1"))
        _run(self.backend.control_heated_seat("VIN1", "front_left", 2))
        _run(self.backend.control_heated_seat("VIN1", "front_right", 1))
        _run(self.backend.trigger_alarm("VIN1"))
        self.assertEqual(
            self.fake.calls,
            [
                ("lock", True),
                ("lock", False),
                ("tailgate",),
                ("windows", True, (9, 10, 11, 12)),
                ("windows", False, (9, 10, 11, 12)),
                ("sunroof", True),
                ("climate", True),
                ("climate", False),
                ("seats", 2, 3),
                ("seats", 2, 1),
                ("find",),
            ],
        )


class FakeIndiaClient:
    def __init__(self):
        self.logged_in = False
        self.vin = "VIN1"
        self.calls = []

    async def login(self):
        self.logged_in = True

    async def vehicles(self):
        return [
            types.SimpleNamespace(
                vin="VIN1",
                name="Comet EV",
                brand="MG",
                model_name="Comet EV",
                model_year="2026",
                raw={"configuration": {"LRD": "1", "EV": "1", "BType": "1"}},
            )
        ]

    async def status(self):
        return types.SimpleNamespace(
            status_time=1_800_000_000,
            locked=True,
            driver_door_open=False,
            passenger_door_open=False,
            rear_left_door_open=False,
            rear_right_door_open=False,
            boot_open=False,
            bonnet_open=False,
            driver_window_open=False,
            passenger_window_open=False,
            rear_left_window_open=False,
            rear_right_window_open=False,
            sunroof_open=False,
            climate_running=True,
            interior_temperature=24,
            exterior_temperature=32,
            fuel_level=None,
            range_km=85.0,
            odometer_km=1234.5,
            aux_battery_voltage=12.4,
            can_bus_active=True,
            last_can_activity=1_800_000_000,
            handbrake=False,
            raw={
                "basicVehicleStatus": {
                    "powerMode": 0,
                    "frontLeftSeatHeatLevel": 1,
                    "frontRightSeatHeatLevel": 3,
                }
            },
        )

    async def control_door_lock(self, lock):
        self.calls.append(("lock", lock))

    async def release_tailgate(self):
        self.calls.append(("tailgate",))

    async def control_windows(self, open_windows, ids):
        self.calls.append(("windows", open_windows, ids))

    async def control_sunroof(self, open_sunroof):
        self.calls.append(("sunroof", open_sunroof))

    async def control_climate(self, on):
        self.calls.append(("climate", on))

    async def control_heated_seats(self, driver, passenger):
        self.calls.append(("seats", driver, passenger))

    async def find_my_car(self):
        self.calls.append(("find",))


if __name__ == "__main__":
    unittest.main()


class TestCommandLimitGuard(unittest.TestCase):
    """Remote-command API methods re-raise the command limit (return code 8)
    without also logging it as a generic error — it's already logged as a
    WARNING in _make_api_call. Generic errors are still logged normally."""

    def _client(self):
        api = sys.modules["mg_saic.api"]
        client = api.SAICMGAPIClient.__new__(api.SAICMGAPIClient)
        client.saic_api = MagicMock()
        return client, api

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_limit_reraised_without_error_log(self):
        client, api = self._client()

        async def _raise(*a, **k):
            raise api.CommandsLimitReachedException("return code: 8")

        client._make_api_call = _raise
        errors = []
        orig = api.LOGGER.error
        api.LOGGER.error = lambda *a, **k: errors.append(a)
        try:
            with self.assertRaises(api.CommandsLimitReachedException):
                self._run(client.lock_vehicle("VIN"))
        finally:
            api.LOGGER.error = orig
        self.assertEqual(errors, [], "command limit must not be logged as an error")

    def test_generic_error_still_logged(self):
        client, api = self._client()

        async def _raise(*a, **k):
            raise RuntimeError("boom")

        client._make_api_call = _raise
        errors = []
        orig = api.LOGGER.error
        api.LOGGER.error = lambda *a, **k: errors.append(a)
        try:
            with self.assertRaises(RuntimeError):
                self._run(client.lock_vehicle("VIN"))
        finally:
            api.LOGGER.error = orig
        self.assertTrue(errors, "a genuine error should still be logged")
