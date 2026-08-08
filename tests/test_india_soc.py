"""Regression coverage for MG India BEV state of charge."""

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_india_soc_test"
LOADED_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.const",
    "aiohttp",
    "mg_ismart_india_client",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.api",
    f"{PACKAGE}.backends",
    f"{PACKAGE}.backends.india",
    f"{PACKAGE}.sensor",
)


class _Values:
    def __getattr__(self, name):
        return name.lower()


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


def _module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path, *, package=False):
    kwargs = {"submodule_search_locations": [str(path.parent)]} if package else {}
    spec = importlib.util.spec_from_file_location(name, path, **kwargs)
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
        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        components = _module("homeassistant.components")
        components.__path__ = []
        helpers = _module("homeassistant.helpers")
        helpers.__path__ = []

        class _SensorEntity:
            pass

        _module(
            "homeassistant.components.sensor",
            SensorEntity=_SensorEntity,
            SensorDeviceClass=_Values(),
        )
        _module("homeassistant.helpers.entity", EntityCategory=_Values())
        _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=_CoordinatorEntity,
        )
        _module(
            "homeassistant.const",
            PERCENTAGE="%",
            UnitOfTemperature=_Values(),
            UnitOfElectricPotential=_Values(),
            UnitOfLength=_Values(),
            UnitOfPressure=_Values(),
            UnitOfEnergy=_Values(),
            UnitOfTime=_Values(),
            UnitOfPower=_Values(),
            UnitOfSpeed=_Values(),
        )

        package = _module(PACKAGE)
        package.__path__ = [str(PKG_DIR)]
        _module(
            f"{PACKAGE}.const",
            DOMAIN="mg_saic",
            LOGGER=logging.getLogger(PACKAGE),
            VEHICLE_REACHABILITY_AWAKE="awake",
            VEHICLE_REACHABILITY_LIKELY_ASLEEP="likely_asleep",
            VEHICLE_REACHABILITY_UNREACHABLE="unreachable",
            DATA_FRESHNESS_LIVE="live",
            DATA_FRESHNESS_CACHED="cached",
            DATA_FRESHNESS_FAILED="failed",
            PRESSURE_TO_BAR=0.1,
            DATA_DECIMAL_CORRECTION=0.1,
            DATA_DECIMAL_CORRECTION_SOC=0.1,
            CHARGING_CURRENT_FACTOR=0.05,
            CHARGING_VOLTAGE_FACTOR=0.25,
            DATA_100_DECIMAL_CORRECTION=0.01,
        )
        _module(f"{PACKAGE}.utils", create_device_info=lambda *_args: {})

        class _GlobalClient:
            def __init__(self, *_args, **_kwargs):
                pass

        _module(f"{PACKAGE}.api", SAICMGAPIClient=_GlobalClient)

        aiohttp = _module("aiohttp")
        aiohttp.ClientSession = object

        class _IndiaApiError(Exception):
            pass

        _module(
            "mg_ismart_india_client",
            MgIndiaApiError=_IndiaApiError,
            MgIndiaClient=object,
            hash_control_pin=lambda pin: pin,
        )

        backends = _load(
            f"{PACKAGE}.backends",
            PKG_DIR / "backends" / "__init__.py",
            package=True,
        )
        india = _load(
            f"{PACKAGE}.backends.india",
            PKG_DIR / "backends" / "india.py",
        )
        sensor = _load(f"{PACKAGE}.sensor", PKG_DIR / "sensor.py")
        return backends, india, sensor
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


BACKENDS, INDIA, SENSOR = _load_modules()


class IndiaBEVStateOfChargeTests(unittest.TestCase):
    def _setup_entities(self, backend, vehicle_type, status, charging=None):
        vin_info = SimpleNamespace(
            vin="VIN1",
            brandName="MG",
            modelName="Test Vehicle",
            modelYear="2026",
        )
        coordinator = SimpleNamespace(
            data={"info": [vin_info], "status": status, "charging": charging},
            vin_info=vin_info,
            vehicle_type=vehicle_type,
            client=backend,
            last_update_success=True,
            has_heated_seats=False,
            has_battery_heating=False,
            has_steering_wheel_heat=False,
            supports_charging_current_limit=False,
            supports_target_soc=False,
        )
        coordinator.backend_supports = lambda feature: BACKENDS.backend_supports(
            backend, feature
        )
        entry = SimpleNamespace(entry_id="entry-1")
        hass = SimpleNamespace(
            data={"mg_saic": {"entry-1_coordinator": coordinator}}
        )
        entities = []

        asyncio.run(
            SENSOR.async_setup_entry(
                hass,
                entry,
                lambda added, update_before_add: entities.extend(added),
            )
        )
        return entities

    @staticmethod
    def _india_status(backend, percentage):
        return backend._map_status(
            SimpleNamespace(
                status_time=1_800_000_000,
                fuel_level=percentage,
                raw={"basicVehicleStatus": {}},
            )
        )

    def test_status_soc_creates_only_soc_battery_entity(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        entities = self._setup_entities(
            backend,
            "BEV",
            self._india_status(backend, 62),
        )

        soc_entities = [
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        ]
        self.assertEqual(len(soc_entities), 1)
        self.assertEqual(soc_entities[0].native_value, 62)
        self.assertTrue(soc_entities[0].available)
        self.assertFalse(
            any(
                isinstance(entity, SENSOR.SAICMGChargingSensor)
                and entity._name == "Total Battery Capacity"
                for entity in entities
            )
        )

    def test_status_soc_accepts_initial_zero(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        entities = self._setup_entities(
            backend,
            "BEV",
            self._india_status(backend, 0),
        )
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )

        self.assertTrue(soc.available)
        self.assertEqual(soc.native_value, 0)

    def test_status_soc_updates_from_62_to_zero(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        entities = self._setup_entities(
            backend,
            "BEV",
            self._india_status(backend, 62),
        )
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )
        self.assertEqual(soc.native_value, 62)

        soc.coordinator.data["status"] = self._india_status(backend, 0)

        self.assertEqual(soc.native_value, 0)

    def test_status_soc_is_unavailable_without_a_valid_initial_value(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        statuses = {
            "missing": SimpleNamespace(basicVehicleStatus=SimpleNamespace()),
            "none": self._india_status(backend, None),
            "minus_one": self._india_status(backend, -1),
            "minus_128": self._india_status(backend, -128),
        }

        for case, status in statuses.items():
            with self.subTest(case=case):
                entities = self._setup_entities(backend, "BEV", status)
                soc = next(
                    entity
                    for entity in entities
                    if isinstance(entity, SENSOR.SAICMGSOCSensor)
                )

                self.assertFalse(soc.available)
                self.assertIsNone(soc.native_value)

    def test_status_soc_retains_last_valid_value_during_temporary_failure(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        entities = self._setup_entities(
            backend,
            "BEV",
            self._india_status(backend, 62),
        )
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )
        self.assertEqual(soc.native_value, 62)

        soc.coordinator.data["status"] = self._india_status(backend, -1)
        soc.coordinator.last_update_success = False

        self.assertEqual(soc.native_value, 62)
        self.assertTrue(soc.available)

    def test_module_loader_restores_imports(self):
        missing = object()
        before = {
            name: sys.modules.get(name, missing) for name in LOADED_MODULE_NAMES
        }

        _load_modules()

        for name in LOADED_MODULE_NAMES:
            with self.subTest(module=name):
                self.assertIs(sys.modules.get(name, missing), before[name])

    def test_global_phev_keeps_charging_soc_and_battery_capacity(self):
        backend = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
        status = SimpleNamespace(
            basicVehicleStatus=SimpleNamespace(extendedData1=61)
        )
        charging = SimpleNamespace(
            chrgMgmtData=SimpleNamespace(bmsPackSOCDsp=620),
            rvsChargeStatus=SimpleNamespace(totalBatteryCapacity=300),
        )

        entities = self._setup_entities(backend, "PHEV", status, charging)
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )

        self.assertEqual(soc.native_value, 62)
        self.assertTrue(
            any(
                isinstance(entity, SENSOR.SAICMGChargingSensor)
                and entity._name == "Total Battery Capacity"
                for entity in entities
            )
        )

    def test_india_non_bevs_keep_fuel_level_without_soc(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        status = self._india_status(backend, 47)

        for vehicle_type in ("PHEV", "HEV", "ICE"):
            with self.subTest(vehicle_type=vehicle_type):
                entities = self._setup_entities(backend, vehicle_type, status)
                fuel_level = next(
                    entity
                    for entity in entities
                    if isinstance(entity, SENSOR.SAICMGVehicleSensor)
                    and entity._name == "Fuel Level"
                )
                self.assertEqual(fuel_level.native_value, 47)
                self.assertFalse(
                    any(
                        isinstance(entity, SENSOR.SAICMGSOCSensor)
                        for entity in entities
                    )
                )


if __name__ == "__main__":
    unittest.main()
