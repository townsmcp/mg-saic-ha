"""Regression coverage for coordinator.climate_mode_from_status.

This is the Climate Mode sensor's resolution logic -- a close sibling of the
climate entity's own hvac_mode property, sharing the same
coordinator.requested_hvac_mode value for disambiguating the shared
cool/heat status byte on mode_select cars. Loaded here as an isolated
function (extracted and exec'd from the real coordinator.py source) rather
than importing the whole module, since climate_mode_from_status has no
dependency on anything else in that file and the full module's import
surface (Home Assistant's DataUpdateCoordinator, aiohttp session helpers,
etc.) would need heavy stubbing for no benefit -- this way the exact,
unmodified method body from the real file is what gets tested.
"""

import re
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = REPO_ROOT / "custom_components" / "mg_saic" / "coordinator.py"


def _load_climate_mode_from_status():
    src = COORDINATOR_PATH.read_text()
    match = re.search(
        r"    def climate_mode_from_status\(self\):.*?\n(?=    @property\n    def vehicle_reachability)",
        src,
        re.S,
    )
    assert match, "climate_mode_from_status not found in coordinator.py -- did it move or get renamed?"
    method_src = textwrap.dedent(match.group(0))
    namespace = {"CLIMATE_STATUS_LOCAL_CONTROL": 6}
    exec(method_src, namespace)
    return namespace["climate_mode_from_status"]


CLIMATE_MODE_FROM_STATUS = _load_climate_mode_from_status()


def _coordinator(
    *,
    status,
    cool=2,
    heat=2,
    status_cool={3},
    status_heat={2},
    status_defrost={5},
    status_fan_only={1},
    requested_hvac_mode="off",
):
    return SimpleNamespace(
        current_remote_climate_status=status,
        climate_mode_cool=cool,
        climate_mode_heat=heat,
        climate_status_cool=status_cool,
        climate_status_heat=status_heat,
        climate_status_defrost=status_defrost,
        climate_status_fan_only=status_fan_only,
        requested_hvac_mode=requested_hvac_mode,
    )


class AmbiguousByteHeatCoolTests(unittest.TestCase):
    """The sensor-side counterpart of AmbiguousModeDisambiguationTests in
    tests/test_climate_presets_and_local_control.py. Before this fix,
    "heat_cool" was never checked here, so a genuine AC On selection fell
    through to the "cool" default the instant the car's status caught up --
    the identical bug already fixed on the climate entity itself, in the
    one other place that resolves this same ambiguity."""

    def test_cool_still_resolves_correctly(self):
        c = _coordinator(status=2, requested_hvac_mode="cool")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "cool")

    def test_heat_still_resolves_correctly(self):
        c = _coordinator(status=2, requested_hvac_mode="heat")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "heat")

    def test_heat_cool_resolves_correctly(self):
        c = _coordinator(status=2, requested_hvac_mode="heat_cool")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "heat_cool")

    def test_never_requested_defaults_to_cool(self):
        c = _coordinator(status=2, requested_hvac_mode="off")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "cool")

    def test_fan_only_is_not_swallowed_by_the_ambiguous_check(self):
        c = _coordinator(status=1, requested_hvac_mode="heat_cool")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "fan_only")

    def test_max_cool_is_not_swallowed_by_the_ambiguous_check(self):
        c = _coordinator(status=3, requested_hvac_mode="heat_cool")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "cool")

    def test_off_and_local_control_still_resolve_correctly(self):
        c = _coordinator(status=0, requested_hvac_mode="heat_cool")
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "off")
        c.current_remote_climate_status = 6
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "on_local")

    def test_non_ambiguous_car_is_unaffected(self):
        # e.g. IS31P: cool != heat, so the ambiguous-byte branch never
        # applies at all -- AC On resolves via ordinary set membership,
        # exactly like an explicit Cool request would (the underlying
        # command really is identical on this shape of car).
        c = _coordinator(
            status=2,
            cool=2,
            heat=4,
            status_cool={2, 3},
            status_heat={4},
            requested_hvac_mode="heat_cool",
        )
        self.assertEqual(CLIMATE_MODE_FROM_STATUS(c), "cool")

    def test_no_status_returns_none(self):
        c = _coordinator(status=None)
        self.assertIsNone(CLIMATE_MODE_FROM_STATUS(c))


class ClimateModeSensorOptionsTests(unittest.TestCase):
    """The sensor is a device_class=ENUM, whose _attr_options is a closed
    list Home Assistant validates the state against -- a value
    climate_mode_from_status can return but that isn't listed here would be
    rejected/ignored by Home Assistant, not just missing a translation."""

    @classmethod
    def setUpClass(cls):
        src = (
            REPO_ROOT / "custom_components" / "mg_saic" / "sensor.py"
        ).read_text()
        match = re.search(r'_attr_options = (\["off", "cool".*?\])', src)
        assert match, "SAICMGClimateModeSensor._attr_options not found"
        cls.options = eval(match.group(1))

    def test_heat_cool_is_a_valid_option(self):
        self.assertIn("heat_cool", self.options)

    def test_every_possible_return_value_is_listed(self):
        # Every string climate_mode_from_status can actually return, checked
        # against the sensor's own declared options -- catches exactly this
        # class of bug (a new return value added without updating the list).
        possible_returns = {
            "off",
            "cool",
            "heat",
            "heat_cool",
            "fan_only",
            "defrost",
            "on_local",
            "unknown",
        }
        self.assertEqual(possible_returns, set(self.options))

    def test_every_locale_translates_heat_cool(self):
        locales = sorted(
            (REPO_ROOT / "custom_components" / "mg_saic" / "translations").glob(
                "*.json"
            )
        )
        self.assertTrue(locales, "no translation files found")
        for path in locales:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            states = data.get("entity", {}).get("sensor", {}).get(
                "climate_mode", {}
            ).get("state", {})
            self.assertIn(
                "heat_cool", states, msg=f"{path.name} is missing heat_cool"
            )


if __name__ == "__main__":
    unittest.main()
