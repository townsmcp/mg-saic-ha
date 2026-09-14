"""Regression coverage for the Climate Mode select entity's setup gate.

Before this fix, the gate that decides whether to create the Climate Mode
select entity was `if not coordinator.cool_uses_start_ac`. That flag is used
for two unrelated purposes across profiles: on genuinely simple-AC-only cars
(e.g. the MG3 Hybrid, ZP22) it means "no real modes exist, the A/C switch
already covers Cool/Off"; on several mode_select cars (AH4EM, MIS3E, EP21,
P12L) it is reused purely to enable the ambiguous cool/heat-mode
disambiguation, and those cars DO have real, selectable modes. Gating on the
flag alone incorrectly skipped the select entity for every one of those
mode_select cars too -- on any Home Assistant restart it was simply never
created, leaving Home Assistant showing a stale "unavailable" restored state
permanently (confirmed live on a MGS6/MIS3E after that profile picked up
cool_uses_start_ac).
"""

import importlib.util
import logging
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_select_gate_test"
LOADED_MODULE_NAMES = (
    "saic_ismart_client_ng",
    "saic_ismart_client_ng.api",
    "saic_ismart_client_ng.api.vehicle_charging",
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.select",
    "homeassistant.components.climate",
    "homeassistant.helpers",
    "homeassistant.helpers.update_coordinator",
    PACKAGE,
    f"{PACKAGE}.const",
    f"{PACKAGE}.api",
    f"{PACKAGE}.backends",
    f"{PACKAGE}.utils",
    f"{PACKAGE}.select",
)


def _module(name, **attributes):
    if name in sys.modules:
        module = sys.modules[name]
    else:
        module = ModuleType(name)
        sys.modules[name] = module
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_select_module():
    previous_modules = {
        name: sys.modules[name] for name in LOADED_MODULE_NAMES if name in sys.modules
    }
    try:
        _module("saic_ismart_client_ng")
        _module("saic_ismart_client_ng.api")
        _module(
            "saic_ismart_client_ng.api.vehicle_charging",
            ScheduledChargingMode=MagicMock(),
            ChargeCurrentLimitCode=MagicMock(),
        )

        homeassistant = _module("homeassistant")
        homeassistant.__path__ = []
        components = _module("homeassistant.components")
        components.__path__ = []
        helpers = _module("homeassistant.helpers")
        helpers.__path__ = []

        class _SelectEntity:
            pass

        class _HVACMode:
            OFF = "off"
            COOL = "cool"
            HEAT = "heat"
            FAN_ONLY = "fan_only"
            HEAT_COOL = "heat_cool"

        class _CoordinatorEntity:
            def __init__(self, coordinator):
                self.coordinator = coordinator

        _module("homeassistant.components.select", SelectEntity=_SelectEntity)
        _module("homeassistant.components.climate", HVACMode=_HVACMode)
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
            SCHEDULED_CHARGING_MODE_LABELS={},
            ChargeCurrentLimitOption=MagicMock(),
            BatterySoc=MagicMock(),
        )

        class _CommandsLimitReachedException(Exception):
            pass

        class _VehicleNotLockedException(Exception):
            pass

        _module(
            f"{PACKAGE}.api",
            CommandsLimitReachedException=_CommandsLimitReachedException,
            VehicleNotLockedException=_VehicleNotLockedException,
        )
        _module(f"{PACKAGE}.backends", Feature=MagicMock())
        _module(f"{PACKAGE}.utils", create_device_info=lambda *_args: {})

        return _load(f"{PACKAGE}.select", PKG_DIR / "select.py")
    finally:
        for name in LOADED_MODULE_NAMES:
            if name in previous_modules:
                sys.modules[name] = previous_modules[name]
            else:
                sys.modules.pop(name, None)


SELECT = _load_select_module()


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_coordinator(
    *, cool_uses_start_ac, climate_control_scheme, climate_status_heat={2}
):
    """A coordinator with just enough set to reach (and pass) every gate in
    async_setup_entry other than the one under test, so every other select
    stays disabled and only the Climate Mode gate's outcome is observed."""
    vin_info = SimpleNamespace(vin="VIN1", brandName="MG", modelName="Test Vehicle")
    return SimpleNamespace(
        data={"info": object()},
        vin_info=vin_info,
        cool_uses_start_ac=cool_uses_start_ac,
        climate_control_scheme=climate_control_scheme,
        climate_status_heat=climate_status_heat,
        supports_charging_current_limit=False,
        vehicle_type="BEV",
        has_heated_seats=False,
        backend_supports=lambda *_a, **_kw: False,
    )


def _entities_created(coordinator):
    hass = SimpleNamespace(
        data={
            "mg_saic": {
                "entry-1_coordinator": coordinator,
                "entry-1": MagicMock(),
            }
        }
    )
    entry = SimpleNamespace(entry_id="entry-1")
    captured = {}

    def _capture(entities, update_before_add=False):
        captured["entities"] = entities

    _run(SELECT.async_setup_entry(hass, entry, _capture))
    return captured.get("entities", [])


class ClimateModeSelectGateTests(unittest.TestCase):
    def test_created_when_cool_uses_start_ac_is_false(self):
        # Baseline, pre-existing behaviour: unaffected by this fix.
        coordinator = _make_coordinator(
            cool_uses_start_ac=False, climate_control_scheme="fan_speed"
        )
        entities = _entities_created(coordinator)
        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], SELECT.SAICMGClimateModeSelect)

    def test_skipped_for_genuinely_simple_ac_only_car(self):
        # e.g. ZP22 (MG3 Hybrid): cool_uses_start_ac=True and NOT mode_select
        # -- no real modes exist, the A/C switch already covers Cool/Off.
        coordinator = _make_coordinator(
            cool_uses_start_ac=True, climate_control_scheme="fan_speed"
        )
        entities = _entities_created(coordinator)
        self.assertEqual(entities, [])

    def test_created_for_mode_select_car_with_cool_uses_start_ac(self):
        # This is the bug (#380-adjacent): AH4EM/MIS3E/EP21/P12L all set
        # cool_uses_start_ac=True purely for the ambiguous-mode
        # disambiguation, and all have real, selectable modes. The entity
        # must still be created for these.
        coordinator = _make_coordinator(
            cool_uses_start_ac=True, climate_control_scheme="mode_select"
        )
        entities = _entities_created(coordinator)
        self.assertEqual(len(entities), 1)
        self.assertIsInstance(entities[0], SELECT.SAICMGClimateModeSelect)

    def test_heat_option_still_gated_on_climate_status_heat(self):
        coordinator = _make_coordinator(
            cool_uses_start_ac=True,
            climate_control_scheme="mode_select",
            climate_status_heat=set(),
        )
        entities = _entities_created(coordinator)
        self.assertEqual(len(entities), 1)
        self.assertNotIn("Heat", entities[0]._attr_options)

    def test_ac_on_option_is_always_offered_and_maps_to_heat_cool(self):
        # The climate entity offers HVACMode.HEAT_COOL unconditionally on
        # every scheme (both an explicit "Heat/cool" request and the car
        # reporting its climate is on but direction-unknown, status 6 local
        # control) -- the select must be able to represent and set it too,
        # or it shows "unknown" any time the climate entity is legitimately
        # in that state. The option string matches Home Assistant's own
        # climate hvac_mode label verbatim, not an invented name.
        coordinator = _make_coordinator(
            cool_uses_start_ac=False, climate_control_scheme="fan_speed"
        )
        entities = _entities_created(coordinator)
        entity = entities[0]
        self.assertIn("Heat/cool", entity._attr_options)
        self.assertEqual(
            SELECT.CLIMATE_MODE_OPTION_TO_HVAC["Heat/cool"], SELECT.HVACMode.HEAT_COOL
        )


if __name__ == "__main__":
    unittest.main()
