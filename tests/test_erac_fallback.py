"""Regression coverage for the Estimated Range After Charging sensor (#262).

Guards two bugs found from a live capture on an MGS6: the sensor sat on one
value for four hours, through three charge-stop/start cycles and a
76.8% -> 90.7% SOC change, because the invalidation check exited before the
projection fallback was ever attempted. The `source` attribute additionally
disagreed with what was displayed, because it re-derived its answer from the
current poll independently of what native_value had actually returned.
"""

import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_erac_test"
LOADED_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.const",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.logic",
    f"{PACKAGE}.trip_stats",
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
        # sensor.py imports Feature/REGION_INDIA from .backends for entity
        # gating this test suite never exercises — stub rather than load the
        # real backends package, which pulls in the external SAIC client.
        _module(f"{PACKAGE}.backends", Feature=_Values(), REGION_INDIA="in")

        logic = _load(f"{PACKAGE}.logic", PKG_DIR / "logic.py")
        _load(f"{PACKAGE}.trip_stats", PKG_DIR / "trip_stats.py")
        sensor = _load(f"{PACKAGE}.sensor", PKG_DIR / "sensor.py")
        return sensor, logic
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


SENSOR, LOGIC = _load_modules()


class ERACFallbackTests(unittest.TestCase):
    def _sensor(self, *, projected):
        """An ERAC sensor with a fake coordinator whose projection is fixed,
        so each test controls only the car's own reported figure."""
        vin_info = SimpleNamespace(vin="VIN1", brandName="MG", modelName="Test")
        coordinator = SimpleNamespace(
            data={"charging": None},
            vin_info=vin_info,
            projected_range_after_charging_km=lambda: projected,
        )
        entry = SimpleNamespace(entry_id="entry1")
        return SENSOR.SAICMGChargingSensor(
            coordinator,
            entry,
            "Estimated Range After Charging",
            "imcuChrgngEstdElecRng",
            None,
            None,
            "mdi:map-marker-distance",
            "measurement",
            1.0,
            "chrgMgmtData",
            "charging",
        )

    def _set_charging(self, entity, chrg_mgmt):
        entity.coordinator.data["charging"] = SimpleNamespace(chrgMgmtData=chrg_mgmt)

    def test_reported_value_is_used_and_labelled(self):
        entity = self._sensor(projected=999.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=410, imcuChrgngEstdElecRngV=0),
        )
        self.assertEqual(entity.native_value, 410.0)
        self.assertEqual(entity.extra_state_attributes, {"source": "reported"})

    def test_falls_back_to_projection_when_reported_is_zero(self):
        entity = self._sensor(projected=398.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=0, imcuChrgngEstdElecRngV=0),
        )
        self.assertEqual(entity.native_value, 398.0)
        self.assertEqual(entity.extra_state_attributes, {"source": "estimated"})

    def test_falls_back_to_projection_when_invalidated(self):
        """The bug: a non-zero validity flag used to exit before the
        fallback was ever attempted, however long that lasted for."""
        entity = self._sensor(projected=398.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=410, imcuChrgngEstdElecRngV=1),
        )
        self.assertEqual(entity.native_value, 398.0)
        self.assertEqual(entity.extra_state_attributes, {"source": "estimated"})

    def test_source_cannot_disagree_with_the_displayed_value(self):
        """The other bug: extra_state_attributes used to re-derive "source"
        from the current poll independently of what native_value returned,
        so a stale reported figure could be labelled "reported" as if fresh.
        """
        entity = self._sensor(projected=398.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=410, imcuChrgngEstdElecRngV=1),
        )
        value = entity.native_value
        attrs = entity.extra_state_attributes
        self.assertEqual(attrs["source"], "estimated")
        self.assertNotEqual(attrs["source"], "reported")
        self.assertEqual(value, 398.0)

    def test_never_stuck_on_a_stale_reported_value(self):
        """The live bug, reproduced: a valid reading is shown once; the flag
        then goes bad while the car and the projection both keep moving.
        The sensor must track the projection, not freeze on the old figure."""
        entity = self._sensor(projected=305.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=410, imcuChrgngEstdElecRngV=0),
        )
        self.assertEqual(entity.native_value, 410.0)

        entity.coordinator.projected_range_after_charging_km = lambda: 321.0
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=410, imcuChrgngEstdElecRngV=1),
        )
        self.assertEqual(entity.native_value, 321.0)
        self.assertNotEqual(entity.native_value, 410.0)

        entity.coordinator.projected_range_after_charging_km = lambda: 340.0
        self.assertEqual(entity.native_value, 340.0)

    def test_holds_last_value_only_when_nothing_at_all_is_available(self):
        entity = self._sensor(projected=250.0)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=250, imcuChrgngEstdElecRngV=0),
        )
        self.assertEqual(entity.native_value, 250.0)

        entity.coordinator.projected_range_after_charging_km = lambda: None
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=0, imcuChrgngEstdElecRngV=1),
        )
        self.assertEqual(entity.native_value, 250.0)
        self.assertEqual(entity.extra_state_attributes, {"source": "stale"})

    def test_unknown_before_anything_has_ever_been_seen(self):
        entity = self._sensor(projected=None)
        self._set_charging(
            entity,
            SimpleNamespace(imcuChrgngEstdElecRng=0, imcuChrgngEstdElecRngV=0),
        )
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)


if __name__ == "__main__":
    unittest.main()
