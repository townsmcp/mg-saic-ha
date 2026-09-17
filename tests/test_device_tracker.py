"""Regression coverage for the GPS device tracker (#375).

The SAIC API can report 0,0 coordinates on the very first poll of a new HA
session (or whenever the car has no GPS fix, e.g. parked in a garage). The
tracker already falls back to the last known-good position it has seen, but
that fallback lived only in memory -- a fresh restart started with nothing
to fall back to, so a 0,0 first poll went straight to the entity. This
verifies the entity now restores its last known-good position from Home
Assistant's restore-state cache before the first coordinator update lands.
"""

import asyncio
import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_device_tracker_test"
LOADED_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.device_tracker",
    "homeassistant.helpers",
    "homeassistant.helpers.restore_state",
    "homeassistant.helpers.update_coordinator",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.device_tracker",
)


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


class _RestoredState:
    def __init__(self, attributes):
        self.attributes = attributes


class _RestoreEntity:
    """Stand-in for homeassistant.helpers.restore_state.RestoreEntity.

    Tests set ``_test_last_state`` on the instance before calling
    ``async_added_to_hass`` to control what "last known state" looks like,
    mirroring how the real mixin surfaces the entity's last recorded state.
    """

    entity_id = None
    _test_last_state = None

    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return self._test_last_state


def _module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_device_tracker():
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
            "homeassistant.helpers.restore_state", RestoreEntity=_RestoreEntity
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

        return _load(f"{PACKAGE}.device_tracker", PKG_DIR / "device_tracker.py")
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


DEVICE_TRACKER = _load_device_tracker()


def _way_point(lat, lon, altitude=100, heading=90, speed=0, hdop=1, satellites=8):
    return SimpleNamespace(
        position=SimpleNamespace(
            latitude=int(lat * 1e6), longitude=int(lon * 1e6), altitude=altitude
        ),
        heading=heading,
        speed=speed,
        hdop=hdop,
        satellites=satellites,
    )


def _status(way_point):
    gps_position = SimpleNamespace(wayPoint=way_point) if way_point else None
    return SimpleNamespace(gpsPosition=gps_position)


def _make_tracker(status):
    vin_info = SimpleNamespace(vin="VIN1", brandName="MG", modelName="Test Vehicle")
    coordinator = SimpleNamespace(
        data={"status": status}, vin_info=vin_info, last_update_success=True
    )
    entry = SimpleNamespace(entry_id="entry-1")
    return DEVICE_TRACKER.SAICMGDeviceTracker(
        coordinator, entry, "gpsPosition", "GPS Location", data_type="status"
    )


class DeviceTrackerRestoreTests(unittest.TestCase):
    def test_restores_last_known_position_when_first_poll_is_zero_zero(self):
        # Simulate the exact #375 scenario: HA has just restarted, the
        # coordinator's first poll reports 0,0 (no fix yet), but the entity
        # previously recorded a real position before the restart.
        tracker = _make_tracker(_status(_way_point(0.0, 0.0)))
        tracker._test_last_state = _RestoredState(
            {"latitude": 51.5074, "longitude": -0.1278}
        )

        asyncio.run(tracker.async_added_to_hass())

        self.assertAlmostEqual(tracker.latitude, 51.5074)
        self.assertAlmostEqual(tracker.longitude, -0.1278)

    def test_fresh_fix_after_restore_overrides_the_restored_value(self):
        tracker = _make_tracker(_status(_way_point(48.8566, 2.3522)))
        tracker._test_last_state = _RestoredState(
            {"latitude": 51.5074, "longitude": -0.1278}
        )

        asyncio.run(tracker.async_added_to_hass())

        self.assertAlmostEqual(tracker.latitude, 48.8566)
        self.assertAlmostEqual(tracker.longitude, 2.3522)

    def test_no_previous_state_leaves_fallback_empty(self):
        # Pre-existing behaviour (unaffected by the restore fix): the
        # zero/zero -> last-known-good fallback only ever triggers once
        # BOTH lat and lon have a stored fallback value. With nothing
        # restored and no real fix seen yet this session, that never
        # happens, so the raw (0.0, 0.0) the API reported passes through.
        tracker = _make_tracker(_status(_way_point(0.0, 0.0)))
        tracker._test_last_state = None

        asyncio.run(tracker.async_added_to_hass())

        self.assertEqual(tracker.latitude, 0.0)
        self.assertEqual(tracker.longitude, 0.0)

    def test_restored_state_missing_coordinates_leaves_fallback_empty(self):
        tracker = _make_tracker(_status(_way_point(0.0, 0.0)))
        tracker._test_last_state = _RestoredState({"source_type": "gps"})

        asyncio.run(tracker.async_added_to_hass())

        self.assertEqual(tracker.latitude, 0.0)
        self.assertEqual(tracker.longitude, 0.0)

    def test_malformed_restored_coordinates_are_ignored_not_fatal(self):
        tracker = _make_tracker(_status(_way_point(0.0, 0.0)))
        tracker._test_last_state = _RestoredState(
            {"latitude": "unknown", "longitude": "unknown"}
        )

        # Should not raise, and should behave as if nothing was restored.
        asyncio.run(tracker.async_added_to_hass())

        self.assertEqual(tracker.latitude, 0.0)
        self.assertEqual(tracker.longitude, 0.0)

    def test_in_session_fallback_still_works_without_a_restart(self):
        # Pre-existing behaviour (unaffected by the restore fix): once a
        # real fix has been read for BOTH lat and lon this session, a
        # later 0,0 poll still falls back to it.
        tracker = _make_tracker(_status(_way_point(48.8566, 2.3522)))
        asyncio.run(tracker.async_added_to_hass())
        self.assertAlmostEqual(tracker.latitude, 48.8566)
        self.assertAlmostEqual(tracker.longitude, 2.3522)

        tracker.coordinator.data["status"] = _status(_way_point(0.0, 0.0))

        self.assertAlmostEqual(tracker.latitude, 48.8566)
        self.assertAlmostEqual(tracker.longitude, 2.3522)


if __name__ == "__main__":
    unittest.main()
