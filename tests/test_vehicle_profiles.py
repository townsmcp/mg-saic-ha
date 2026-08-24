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

    def test_mgs5_uses_usable_not_gross_capacity(self):
        # #301 (SteveMSJ): MGS5 EV Long Range is 64 kWh gross / 62.1 kWh usable.
        # The table's convention is usable, so it must be 62.1, not 64.0.
        self.assertEqual(const.VEHICLE_PROFILES["MZS3E"]["battery_capacity_kwh"], 62.1)


class TestFuelTankSizes(unittest.TestCase):
    """#301: per-model fuel-tank litres for the combustion (ICE/HEV/PHEV) profiles."""

    KNOWN = {
        "ZP22": 36.0,   # MG3 Hybrid+ (HEV)
        "IS31P": 65.0,  # MG S9 PHEV
        "AS33P": 37.0,  # MG HS PHEV (UK/EU default; AU Super Hybrid is 55 L)
    }

    def test_known_tanks_populated(self):
        for series, litres in self.KNOWN.items():
            self.assertEqual(
                const.VEHICLE_PROFILES[series].get("fuel_tank_litres"),
                litres,
                msg=f"{series} fuel_tank_litres should be {litres}",
            )

    def test_default_profile_has_no_tank(self):
        # DEFAULT covers many unprofiled models, so it must not assume a size.
        self.assertIsNone(const.DEFAULT_VEHICLE_PROFILE.get("fuel_tank_litres"))

    def test_any_declared_tank_is_plausible(self):
        for series, profile in const.VEHICLE_PROFILES.items():
            litres = profile.get("fuel_tank_litres")
            if litres is not None:
                self.assertGreater(litres, 15.0, msg=f"{series} tank too small")
                self.assertLess(litres, 120.0, msg=f"{series} tank too large")


class TestAS33PClimate(unittest.TestCase):
    """MG HS PHEV (AS33P) climate profile — decoded from iSmart traffic (#262)."""

    def setUp(self):
        self.p = const.VEHICLE_PROFILES["AS33P"]

    def test_temp_range_reaches_30(self):
        # App slider runs 16–30 °C; the old cap of 28 made 29/30 unreachable.
        self.assertEqual(self.p["min_temp"], 16)
        self.assertEqual(self.p["max_temp"], 30)

    def test_linear_temp_index_matches_captured_wire_values(self):
        # temp_offset + (temp - min_temp) must reproduce the decoded paramId 20
        # values: 16→2, 23→9, 29→15 (so 30→16).
        off, lo = self.p["temp_offset"], self.p["min_temp"]
        idx = lambda t: off + (t - lo)
        self.assertEqual(idx(16), 2)
        self.assertEqual(idx(23), 9)
        self.assertEqual(idx(29), 15)
        self.assertEqual(idx(30), 16)

    def test_fixed_auto_fan_and_airflow_flags(self):
        # No remote fan control: fixed AUTO fan of 2, and Fan Only = AC Airflow.
        self.assertEqual(self.p["climate_fan_auto"], 2)
        self.assertTrue(self.p["climate_fan_only_airflow"])

    def test_cool_status_blocks_airflow(self):
        # The AC-Airflow guard treats any status outside {off, fan-only} as
        # "AC on, airflow blocked". So the cooling status must NOT be in the
        # fan-only set, or a running AC would fail to block airflow.
        cool = self.p["climate_status_cool"]
        fan_only = self.p["climate_status_fan_only"]
        self.assertTrue(cool.isdisjoint(fan_only))
        self.assertNotIn(0, cool)  # 0 is "off", never a cooling status

    def test_usable_capacity_and_energy_correction(self):
        # HS PHEV pack: 24.7 kWh nominal / 23.2 kWh usable — display the usable.
        self.assertEqual(self.p["battery_capacity_kwh"], 23.2)
        # The API inflates energy fields (~3x); a correction must be present so
        # powerUsageSinceLastCharge / lastChargeEndingPower read as real kWh.
        correction = self.p["charging_capacity_correction"]
        self.assertIsNotNone(correction)
        # Sanity-check against the live report (#262/#301): the raw 41.9 kWh
        # Power Usage Since Last Charge must correct to roughly the real ~14 kWh
        # battery draw (57.9% of 24.7), not stay ~3x too high.
        self.assertAlmostEqual(41.9 * correction, 14.0, delta=0.6)


class TestBatteryCapacityOverride(unittest.TestCase):
    """User-supplied usable-capacity override (options flow)."""

    def test_parse_blank_and_none_mean_no_override(self):
        self.assertIsNone(const.parse_capacity_override(None))
        self.assertIsNone(const.parse_capacity_override(""))

    def test_parse_non_positive_means_no_override(self):
        self.assertIsNone(const.parse_capacity_override(0))
        self.assertIsNone(const.parse_capacity_override("0"))
        self.assertIsNone(const.parse_capacity_override(-5))

    def test_parse_non_numeric_means_no_override(self):
        self.assertIsNone(const.parse_capacity_override("abc"))

    def test_parse_valid_values(self):
        self.assertEqual(const.parse_capacity_override(61.7), 61.7)
        self.assertEqual(const.parse_capacity_override("62.1"), 62.1)

    def test_precedence_user_over_profile_over_api(self):
        # Mirror the coordinator's resolution: start from the profile value,
        # then let a user override win. API value only used when both are absent.
        def resolve(profile_val, user_override, api_val):
            known = profile_val  # profile (may be None -> API used downstream)
            override = const.parse_capacity_override(user_override)
            if override is not None:
                known = override
            return known if known is not None else api_val

        self.assertEqual(resolve(62.1, "61.7", 72.5), 61.7)   # user wins
        self.assertEqual(resolve(62.1, "", 72.5), 62.1)       # profile wins
        self.assertEqual(resolve(None, "", 72.5), 72.5)       # API fallback
        self.assertEqual(resolve(None, "61.7", 72.5), 61.7)   # user over API

    def test_recompute_on_options_save_without_reload(self):
        # Reproduces the reported bug (#301): a user set 61.7 for their MG4 but
        # Total Battery Capacity kept showing 72.5. Root cause was that
        # async_update_options (called on every options save) never re-read the
        # override or recomputed known_battery_capacity_kwh — only async_setup
        # (which runs once, at integration load) did. This mirrors the fixed
        # async_update_options formula and checks it responds to repeated saves
        # with no reload in between.
        profile_capacity = None  # e.g. EH32 (MG4) — capacity not profiled

        def recompute(raw_option):
            override = const.parse_capacity_override(raw_option)
            return override if override is not None else profile_capacity

        self.assertIsNone(recompute(""))            # nothing set yet
        self.assertEqual(recompute("61.7"), 61.7)    # user saves an override
        self.assertEqual(recompute("64"), 64.0)      # user changes it again
        self.assertIsNone(recompute(""))             # user clears it


if __name__ == "__main__":
    unittest.main()
