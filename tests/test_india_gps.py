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
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    "aiohttp",
    "mg_ismart_india_client",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.api",
    f"{PACKAGE}.backends",
    f"{PACKAGE}.backends.india",
    f"{PACKAGE}.device_tracker",
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

        _module(
            "homeassistant.components.device_tracker", TrackerEntity=_TrackerEntity
        )
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
        return india, device_tracker
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


INDIA, DEVICE_TRACKER = _load_modules()


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

    def test_absent_speed_and_heading_read_as_stationary(self):
        # The consumers compare speed numerically without a None guard.
        gps = _india_status(
            self.backend, _client_gps(speed_kmh=None, heading_deg=None)
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


if __name__ == "__main__":
    unittest.main()
