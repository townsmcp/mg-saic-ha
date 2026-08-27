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
            TEMP_SPIKE_BASE_TOLERANCE_C=3.0,
            TEMP_SPIKE_MAX_RATE_C_PER_S=0.1,
            MILEAGE_UINT16_SATURATION=65535,
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
    def _setup_entities(
        self,
        backend,
        vehicle_type,
        status,
        charging=None,
        *,
        region=None,
        series=None,
        colour=None,
    ):
        vin_info = SimpleNamespace(
            vin="VIN1",
            brandName="MG",
            modelName="Test Vehicle",
            modelYear="2026",
            series=series,
            colorName=colour,
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
        entry = SimpleNamespace(entry_id="entry-1", data={"region": region})
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

    def test_vehicle_metadata_entities_require_india_region_and_values(self):
        india_backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        global_backend = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
        cases = (
            (india_backend, "India", "EQ100", "Clay Beige", {"series", "colorName"}),
            (global_backend, "EU", "EQ100", "Clay Beige", set()),
            (india_backend, "India", "EQ100", "", {"series"}),
            (india_backend, "India", None, None, set()),
        )

        for backend, region, series, colour, expected in cases:
            with self.subTest(region=region, series=series, colour=colour):
                entities = self._setup_entities(
                    backend,
                    "UNKNOWN",
                    None,
                    region=region,
                    series=series,
                    colour=colour,
                )
                fields = {
                    entity._field
                    for entity in entities
                    if isinstance(entity, SENSOR.SAICMGVehicleDetailSensor)
                    and entity._field in {"series", "colorName"}
                }
                self.assertEqual(fields, expected)

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
        # Total Battery Capacity is gated on CHARGING_DATA, which India now
        # advertises, so the entity is created — but the India charging frame
        # leaves totalBatteryCapacity absent in every capture seen so far, so
        # it reads unknown rather than a fabricated number.
        capacity = [
            entity
            for entity in entities
            if isinstance(entity, SENSOR.SAICMGChargingSensor)
            and entity._name == "Total Battery Capacity"
        ]
        self.assertEqual(len(capacity), 1)
        self.assertIsNone(capacity[0].native_value)

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

    def test_global_soc_falls_back_to_extended_data_when_charging_soc_is_zero(self):
        backend = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
        status = SimpleNamespace(
            basicVehicleStatus=SimpleNamespace(extendedData1=61)
        )
        charging = SimpleNamespace(
            chrgMgmtData=SimpleNamespace(bmsPackSOCDsp=0),
            rvsChargeStatus=SimpleNamespace(totalBatteryCapacity=300),
        )

        entities = self._setup_entities(backend, "PHEV", status, charging)
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )

        # bmsPackSOCDsp=0 is treated as a stale/unpopulated reading, not a
        # real 0% SoC, so the sensor should fall back to extendedData1.
        self.assertEqual(soc.native_value, 61)
        self.assertTrue(soc.available)

    def test_global_soc_falls_back_to_extended_data_when_charging_data_missing(self):
        backend = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
        status = SimpleNamespace(
            basicVehicleStatus=SimpleNamespace(extendedData1=61)
        )
        charging = SimpleNamespace(
            chrgMgmtData=SimpleNamespace(bmsPackSOCDsp=None),
            rvsChargeStatus=SimpleNamespace(totalBatteryCapacity=300),
        )

        entities = self._setup_entities(backend, "PHEV", status, charging)
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )

        self.assertEqual(soc.native_value, 61)
        self.assertTrue(soc.available)

    def test_global_hev_gets_soc_sensor_from_extended_data(self):
        # MG3 Hybrid+ (issue #318): a self-charging HEV with no charge port.
        # No charging data is present, but basicVehicleStatus.extendedData1
        # independently tracks HV battery SoC and should populate the sensor.
        backend = SimpleNamespace(supported_features=BACKENDS.GLOBAL_FEATURES)
        status = SimpleNamespace(
            basicVehicleStatus=SimpleNamespace(extendedData1=73)
        )

        entities = self._setup_entities(backend, "HEV", status, charging=None)
        soc = next(
            entity for entity in entities if isinstance(entity, SENSOR.SAICMGSOCSensor)
        )

        self.assertEqual(soc.native_value, 73)
        self.assertTrue(soc.available)

    def test_india_hev_gets_no_soc_sensor(self):
        # India's extendedData1 is repurposed to carry fuel_level, not
        # battery SoC, and INDIA_FEATURES has no CHARGING_DATA — so HEV
        # must NOT gain a SOC sensor there, unlike the global backend above.
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        status = self._india_status(backend, 47)

        entities = self._setup_entities(backend, "HEV", status)

        self.assertFalse(
            any(isinstance(entity, SENSOR.SAICMGSOCSensor) for entity in entities)
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
