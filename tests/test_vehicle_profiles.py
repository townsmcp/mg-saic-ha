# File: tests/test_vehicle_profiles.py
#
# Unit tests for the per-series VEHICLE_PROFILES table in const.py, focused on
# the battery-capacity override that corrects the SAIC API's unreliable
# totalBatteryCapacity field.
#
# These tests run in plain CPython (no Home Assistant installed), matching the
# python-tests.yaml CI workflow.  const.py imports voluptuous and a Home
# Assistant helper at module level, so those are stubbed before it is loaded,
# and the mg_saic package is registered manually so importing const does NOT
# execute __init__.py (which needs a full Home Assistant runtime).  This mirrors
# tests/test_backends.py.
#
# Guarded invariants:
#   * The recurring bogus SAIC placeholder totalBatteryCapacity=725 (→ 72.5 kWh)
#     must be corrected for the IM6 (series S12L, Discussion #53): the profile
#     forces 100 kWh for the confirmed Platinum/Performance pack.
#   * Series matching is a substring test (VinInfo.series is e.g. 'S12L'), so
#     the profile must resolve for the real-world series string.
#   * The IM6 profile changes ONLY the battery capacity relative to the default
#     profile — every other (climate/feature) field must stay identical, so the
#     fix cannot regress the car's untested climate behaviour.

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


for _name in (
    "voluptuous",
    "homeassistant",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
):
    _stub(_name)

if "mg_saic" not in sys.modules:
    _pkg = types.ModuleType("mg_saic")
    _pkg.__path__ = [str(PKG_DIR)]
    sys.modules["mg_saic"] = _pkg

const = _load("mg_saic.const", PKG_DIR / "const.py")


def _resolve_profile(series):
    """Reproduce the coordinator's substring match against VEHICLE_PROFILES.

    Mirrors MGSAICDataUpdateCoordinator._process_vehicle_info: iterate the
    profiles in order and return the first whose key is a substring of the
    upper-cased series, else the default profile.
    """
    series = (series or "").upper()
    for key, profile in const.VEHICLE_PROFILES.items():
        if key in series:
            return key, profile
    return None, const.DEFAULT_VEHICLE_PROFILE


class TestIM6BatteryCapacity(unittest.TestCase):
    """IM6 (series S12L) — Discussion #53: correct the bogus 72.5 kWh reading."""

    def test_s12l_profile_exists(self):
        self.assertIn("S12L", const.VEHICLE_PROFILES)

    def test_s12l_overrides_capacity_to_100(self):
        # The API reports totalBatteryCapacity=725 (→ 72.5 kWh); the profile must
        # override it with the real 100 kWh Platinum/Performance pack.
        self.assertEqual(
            const.VEHICLE_PROFILES["S12L"]["battery_capacity_kwh"], 100.0
        )

    def test_s12l_does_not_reuse_the_bogus_value(self):
        # Guard against anyone "trusting the API" (None) or pinning the placeholder.
        capacity = const.VEHICLE_PROFILES["S12L"]["battery_capacity_kwh"]
        self.assertIsNotNone(capacity)
        self.assertNotAlmostEqual(capacity, 72.5, places=3)

    def test_real_world_series_string_resolves_to_the_profile(self):
        # VinInfo.series in the wild is exactly 'S12L' (see #53 log).
        key, profile = _resolve_profile("S12L")
        self.assertEqual(key, "S12L")
        self.assertEqual(profile["battery_capacity_kwh"], 100.0)

    def test_match_is_case_insensitive_substring(self):
        # The coordinator upper-cases the series before matching; make sure a
        # decorated/lower-case series string still resolves.
        key, profile = _resolve_profile("s12l l")
        self.assertEqual(key, "S12L")
        self.assertEqual(profile["battery_capacity_kwh"], 100.0)

    def test_only_battery_capacity_differs_from_default(self):
        # The fix must be surgical: relative to the default profile the IM6 used
        # while unprofiled, the ONLY field that may differ is battery_capacity_kwh.
        # Any other divergence would silently change the car's (untested) climate
        # or feature behaviour.
        s12l = const.VEHICLE_PROFILES["S12L"]
        default = const.DEFAULT_VEHICLE_PROFILE
        for field, default_value in default.items():
            if field == "battery_capacity_kwh":
                continue
            self.assertIn(
                field,
                s12l,
                msg=f"S12L is missing default field {field!r}",
            )
            self.assertEqual(
                s12l[field],
                default_value,
                msg=f"S12L unexpectedly changes {field!r} vs the default profile",
            )


class TestBatteryCapacityOverridesAreSane(unittest.TestCase):
    """Every declared battery override must be a plausible real capacity."""

    def test_no_profile_pins_the_bogus_placeholder(self):
        # 72.5 kWh is the ×0.1 decode of the SAIC placeholder 725 and must never
        # be hard-coded as a "known-good" capacity.
        for key, profile in const.VEHICLE_PROFILES.items():
            capacity = profile.get("battery_capacity_kwh")
            if capacity is not None:
                self.assertNotAlmostEqual(
                    capacity,
                    72.5,
                    places=3,
                    msg=f"{key} pins the bogus 72.5 kWh placeholder",
                )

    def test_declared_capacities_are_in_a_plausible_range(self):
        for key, profile in const.VEHICLE_PROFILES.items():
            capacity = profile.get("battery_capacity_kwh")
            if capacity is not None:
                self.assertGreater(capacity, 5.0, msg=f"{key} capacity too small")
                self.assertLess(capacity, 250.0, msg=f"{key} capacity too large")


if __name__ == "__main__":
    unittest.main()
