"""Regression coverage for MG India GPS position.

The India backend used to hand the shared entities a gpsPosition namespace
carrying nothing but a (never-populated) speed. Reading .wayPoint off it threw
AttributeError inside device_tracker.async_setup_entry, which swallows the
error, so the GPS Location entity was silently never created.
"""

import asyncio
import importlib.util
import logging
import sys
import unittest
from enum import IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_india_gps_test"
LOADED_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.device_tracker",
    "homeassistant.components.sensor",
    "homeassistant.const",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator",
    "aiohttp",
    "mg_ismart_india_client",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.api",
    f"{PACKAGE}.backends",
    f"{PACKAGE}.backends.india",
    f"{PACKAGE}.abrp",
    f"{PACKAGE}.device_tracker",
    f"{PACKAGE}.sensor",
)


class _GpsStatus(IntEnum):
    """Stand-in for mg_ismart_india_client.models.GpsStatus."""

    NO_SIGNAL = 0
    TIME_FIX = 1
    FIX_2D = 2
    FIX_3D = 3


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

        class _TrackerEntity:
            pass

        class _SensorEntity:
            pass

        class _ClientTimeout:
            def __init__(self, **_kwargs):
                pass

        class _Values:
            BATTERY = "battery"
            CURRENT = "current"
            DIAGNOSTIC = "diagnostic"
            DISTANCE = "distance"
            DURATION = "duration"
            ENERGY = "energy"
            ENERGY_DISTANCE = "energy_distance"
            ENUM = "enum"
            KILOMETERS = "km"
            KILOMETERS_PER_HOUR = "km/h"
            KILO_WATT = "kW"
            KILO_WATT_HOUR = "kWh"
            MINUTES = "min"
            PERCENTAGE = "%"
            POWER = "power"
            PRESSURE = "pressure"
            SPEED = "speed"
            TEMPERATURE = "temperature"
            TIMESTAMP = "timestamp"
            VOLT = "V"
            VOLTAGE = "voltage"

        _module(
            "homeassistant.components.device_tracker", TrackerEntity=_TrackerEntity
        )
        _module(
            "homeassistant.components.sensor",
            SensorDeviceClass=_Values,
            SensorEntity=_SensorEntity,
        )
        _module(
            "homeassistant.const",
            PERCENTAGE=_Values.PERCENTAGE,
            UnitOfElectricPotential=_Values,
            UnitOfEnergy=_Values,
            UnitOfLength=_Values,
            UnitOfPower=_Values,
            UnitOfPressure=_Values,
            UnitOfSpeed=_Values,
            UnitOfTemperature=_Values,
            UnitOfTime=_Values,
        )
        _module("homeassistant.helpers.entity", EntityCategory=_Values)
        _module(
            "homeassistant.helpers.update_coordinator",
            CoordinatorEntity=_CoordinatorEntity,
        )

        package = _module(PACKAGE)
        package.__path__ = [str(PKG_DIR)]
        _module(
            f"{PACKAGE}.const",
            DOMAIN="mg_saic",
            LOGGER=logging.getLogger(PACKAGE),
            ABRP_ME_URL="https://example.invalid/me",
            ABRP_SEND_URL="https://example.invalid/send",
            CHARGING_CURRENT_FACTOR=1,
            CHARGING_VOLTAGE_FACTOR=1,
            DATA_100_DECIMAL_CORRECTION=0.01,
            DATA_DECIMAL_CORRECTION=0.1,
            DATA_DECIMAL_CORRECTION_SOC=0.1,
            DATA_FRESHNESS_CACHED="cached",
            DATA_FRESHNESS_FAILED="failed",
            DATA_FRESHNESS_LIVE="live",
            MILEAGE_UINT16_SATURATION=65535,
            PRESSURE_TO_BAR=0.04,
            TEMP_SPIKE_BASE_TOLERANCE_C=5,
            TEMP_SPIKE_MAX_RATE_C_PER_S=1,
            VEHICLE_REACHABILITY_AWAKE="awake",
            VEHICLE_REACHABILITY_LIKELY_ASLEEP="likely_asleep",
            VEHICLE_REACHABILITY_UNREACHABLE="unreachable",
        )
        _module(f"{PACKAGE}.utils", create_device_info=lambda *_args: {})

        class _GlobalClient:
            def __init__(self, *_args, **_kwargs):
                pass

        _module(f"{PACKAGE}.api", SAICMGAPIClient=_GlobalClient)

        aiohttp = _module("aiohttp")
        aiohttp.ClientSession = object
        aiohttp.ClientTimeout = _ClientTimeout
        aiohttp.ClientError = Exception

        class _IndiaApiError(Exception):
            pass

        class _ChargingStatusUnavailable(_IndiaApiError):
            pass

        _module(
            "mg_ismart_india_client",
            ChargingStatusUnavailable=_ChargingStatusUnavailable,
            MgIndiaApiError=_IndiaApiError,
            MgIndiaClient=object,
            hash_control_pin=lambda pin: pin,
        )

        _load(
            f"{PACKAGE}.backends",
            PKG_DIR / "backends" / "__init__.py",
            package=True,
        )
        india = _load(
            f"{PACKAGE}.backends.india",
            PKG_DIR / "backends" / "india.py",
        )
        device_tracker = _load(
            f"{PACKAGE}.device_tracker", PKG_DIR / "device_tracker.py"
        )
        sensor = _load(f"{PACKAGE}.sensor", PKG_DIR / "sensor.py")
        abrp = _load(f"{PACKAGE}.abrp", PKG_DIR / "abrp.py")
        return india, device_tracker, sensor, abrp
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


INDIA, DEVICE_TRACKER, SENSOR, ABRP = _load_modules()


def _client_gps(**overrides):
    """A GpsPosition as mg-ismart-india-client hands it over: real units."""
    values = {
        "latitude": 12.971599,
        "longitude": 77.594566,
        "altitude_m": 920,
        "heading_deg": 180,
        "speed_kmh": 42.1,
        "hdop": 8,
        "satellites": 11,
        "gps_status": _GpsStatus.FIX_3D,
        "position_time": 1_755_230_000,
    }
    values.update(overrides)
    values.setdefault(
        "has_fix",
        values["gps_status"] in (_GpsStatus.FIX_2D, _GpsStatus.FIX_3D)
        and values["latitude"] is not None
        and values["longitude"] is not None,
    )
    return SimpleNamespace(**values)


def _india_status(backend, gps):
    return backend._map_status(
        SimpleNamespace(
            status_time=1_800_000_000,
            gps=gps,
            raw={"basicVehicleStatus": {}},
        )
    )


class IndiaGpsMappingTests(unittest.TestCase):
    def setUp(self):
        self.backend = INDIA.IndiaBackend("user", "password", vin="VIN1")

    def test_position_is_mapped_into_the_saic_shape_and_units(self):
        gps = _india_status(self.backend, _client_gps()).gpsPosition

        # Coordinates go back to micro-degrees, speed back to tenths of a km/h,
        # which is what the shared entities divide by.
        self.assertEqual(gps.wayPoint.position.latitude, 12_971_599)
        self.assertEqual(gps.wayPoint.position.longitude, 77_594_566)
        self.assertEqual(gps.wayPoint.position.altitude, 920)
        self.assertEqual(gps.wayPoint.heading, 180)
        self.assertEqual(gps.wayPoint.speed, 421)
        self.assertEqual(gps.wayPoint.hdop, 8)
        self.assertEqual(gps.wayPoint.satellites, 11)
        self.assertEqual(gps.timeStamp, 1_755_230_000)

    def test_gps_status_is_exposed_the_way_the_abrp_sender_reads_it(self):
        # abrp._gps_has_fix accepts either the decoded member's .name or its
        # .value, against {2, 3, "FIX_2D", "FIX_3d", "FIX_3D"}.
        gps = _india_status(self.backend, _client_gps()).gpsPosition

        self.assertEqual(gps.gpsStatus, 3)
        self.assertEqual(gps.gps_status_decoded.name, "FIX_3D")
        self.assertEqual(gps.gps_status_decoded.value, 3)

    def test_missing_position_yields_no_waypoint_rather_than_a_broken_one(self):
        for gps in (None, _client_gps(latitude=None, longitude=None)):
            with self.subTest(gps=gps):
                mapped = _india_status(self.backend, gps).gpsPosition
                self.assertIsNone(mapped.wayPoint)

    def test_no_fix_status_preserves_metadata_but_suppresses_coordinates(self):
        cases = (
            _client_gps(gps_status=_GpsStatus.NO_SIGNAL),
            _client_gps(
                gps_status=_GpsStatus.TIME_FIX, latitude=0.0, longitude=0.0
            ),
            _client_gps(has_fix=False),
        )
        for source in cases:
            with self.subTest(status=source.gps_status, has_fix=source.has_fix):
                mapped = _india_status(self.backend, source).gpsPosition
                self.assertIsNone(mapped.wayPoint)
                self.assertEqual(mapped.gpsStatus, source.gps_status.value)
                self.assertIs(mapped.gps_status_decoded, source.gps_status)

    def test_two_dimensional_fix_with_coordinates_is_usable(self):
        gps = _india_status(
            self.backend, _client_gps(gps_status=_GpsStatus.FIX_2D)
        ).gpsPosition

        self.assertEqual(gps.wayPoint.position.latitude, 12_971_599)
        self.assertEqual(gps.wayPoint.position.longitude, 77_594_566)

    def test_absent_speed_and_heading_remain_unknown(self):
        gps = _india_status(
            self.backend, _client_gps(speed_kmh=None, heading_deg=None)
        ).gpsPosition

        self.assertIsNone(gps.wayPoint.speed)
        self.assertIsNone(gps.wayPoint.heading)

    def test_zero_speed_and_heading_remain_numeric_zero(self):
        gps = _india_status(
            self.backend, _client_gps(speed_kmh=0, heading_deg=0)
        ).gpsPosition

        self.assertEqual(gps.wayPoint.speed, 0)
        self.assertEqual(gps.wayPoint.heading, 0)


class IndiaDeviceTrackerTests(unittest.TestCase):
    def _setup_tracker(self, status):
        vin_info = SimpleNamespace(
            vin="VIN1",
            brandName="MG",
            modelName="Test Vehicle",
            modelYear="2026",
        )
        coordinator = SimpleNamespace(
            data={"info": [vin_info], "status": status},
            vin_info=vin_info,
            last_update_success=True,
        )
        entry = SimpleNamespace(entry_id="entry-1")
        hass = SimpleNamespace(data={"mg_saic": {"entry-1_coordinator": coordinator}})
        entities = []

        asyncio.run(
            DEVICE_TRACKER.async_setup_entry(
                hass,
                entry,
                lambda added, update_before_add: entities.extend(added),
            )
        )
        return entities

    def test_tracker_reports_the_vehicle_position(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        (tracker,) = self._setup_tracker(_india_status(backend, _client_gps()))

        self.assertAlmostEqual(tracker.latitude, 12.971599)
        self.assertAlmostEqual(tracker.longitude, 77.594566)
        self.assertEqual(tracker.elevation, 920)
        self.assertEqual(tracker.heading, 180)
        self.assertEqual(tracker.source_type, "gps")
        self.assertEqual(
            tracker.extra_state_attributes,
            {
                "elevation": 920,
                "HDOP": 8,
                "satellites": 11,
                "heading": "S",
                "raw_heading": 180,
            },
        )

    def test_tracker_survives_a_status_with_no_position(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        (tracker,) = self._setup_tracker(_india_status(backend, None))

        self.assertIsNone(tracker.latitude)
        self.assertIsNone(tracker.longitude)
        self.assertIsNone(tracker.elevation)
        self.assertEqual(tracker.extra_state_attributes, {})

    def test_tracker_suppresses_no_fix_coordinates(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        status = _india_status(
            backend, _client_gps(gps_status=_GpsStatus.NO_SIGNAL)
        )
        (tracker,) = self._setup_tracker(status)

        self.assertIsNone(tracker.latitude)
        self.assertIsNone(tracker.longitude)
        self.assertEqual(tracker.extra_state_attributes, {})

    def test_tracker_does_not_invent_heading_when_motion_is_unknown(self):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        status = _india_status(
            backend, _client_gps(speed_kmh=None, heading_deg=None)
        )
        (tracker,) = self._setup_tracker(status)

        self.assertIsNone(tracker.heading)
        self.assertEqual(tracker.extra_state_attributes["heading"], "Unknown")
        self.assertIsNone(tracker.extra_state_attributes["raw_heading"])

    def test_tracker_retains_a_real_heading_when_motion_becomes_unknown(self):
        cases = (
            {"speed_kmh": None},
            {"heading_deg": None},
            {"speed_kmh": None, "heading_deg": None},
        )
        for missing_motion in cases:
            with self.subTest(missing_motion=missing_motion):
                backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
                (tracker,) = self._setup_tracker(
                    _india_status(backend, _client_gps())
                )
                self.assertEqual(tracker.heading, 180)

                tracker.coordinator.data["status"] = _india_status(
                    backend, _client_gps(**missing_motion)
                )

                self.assertEqual(tracker.heading, 180)
                self.assertEqual(tracker.extra_state_attributes["heading"], "S")


class IndiaSpeedSensorTests(unittest.TestCase):
    def _speed_sensor(self, gps):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        vin_info = SimpleNamespace(
            vin="VIN1", brandName="MG", modelName="Test Vehicle"
        )
        coordinator = SimpleNamespace(
            data={"status": _india_status(backend, gps)},
            vin_info=vin_info,
            last_update_success=True,
        )
        return SENSOR.SAICMGVehicleSpeedSensor(
            coordinator,
            SimpleNamespace(entry_id="entry-1"),
            "Speed",
            "speed",
            "gpsPosition",
            "speed",
            "km/h",
            "mdi:speedometer",
            "measurement",
            0.1,
            "status",
        )

    def test_speed_sensor_reads_a_valid_fix(self):
        self.assertEqual(self._speed_sensor(_client_gps()).native_value, 42.1)

    def test_speed_sensor_suppresses_no_fix_coordinates(self):
        sensor = self._speed_sensor(
            _client_gps(gps_status=_GpsStatus.NO_SIGNAL)
        )

        self.assertIsNone(sensor.native_value)

    def test_speed_sensor_reports_unknown_for_missing_speed(self):
        sensor = self._speed_sensor(_client_gps(speed_kmh=None))

        self.assertIsNone(sensor.native_value)

    def test_speed_sensor_preserves_zero_speed(self):
        self.assertEqual(
            self._speed_sensor(_client_gps(speed_kmh=0)).native_value, 0
        )


class IndiaAbrpGpsTests(unittest.TestCase):
    def setUp(self):
        self.backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        self.abrp = ABRP.AbrpApi(None, "api-key", "user-token")

    def _payload(self, gps):
        mapped = _india_status(self.backend, gps).gpsPosition
        return self.abrp._gps(mapped)

    def test_abrp_reads_a_valid_fix(self):
        payload = self._payload(_client_gps())

        self.assertEqual(payload["lat"], 12.971599)
        self.assertEqual(payload["lon"], 77.594566)
        self.assertEqual(payload["speed"], 42.1)
        self.assertEqual(payload["heading"], 180)

    def test_abrp_suppresses_no_signal_and_time_fix(self):
        for gps_status in (_GpsStatus.NO_SIGNAL, _GpsStatus.TIME_FIX):
            with self.subTest(gps_status=gps_status):
                self.assertEqual(
                    self._payload(_client_gps(gps_status=gps_status)), {}
                )

    def test_abrp_omits_missing_speed_and_heading(self):
        payload = self._payload(_client_gps(speed_kmh=None, heading_deg=None))

        self.assertNotIn("speed", payload)
        self.assertNotIn("heading", payload)
        self.assertEqual(payload["lat"], 12.971599)
        self.assertEqual(payload["lon"], 77.594566)

    def test_abrp_preserves_zero_speed_and_heading(self):
        payload = self._payload(_client_gps(speed_kmh=0, heading_deg=0))

        self.assertEqual(payload["speed"], 0.0)
        self.assertEqual(payload["heading"], 0)


if __name__ == "__main__":
    unittest.main()
