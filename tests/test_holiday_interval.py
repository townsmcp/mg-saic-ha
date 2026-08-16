"""Regression guard for the holiday-mode interval unit (PR #290).

The holiday-mode update interval is configured in HOURS, so it must be parsed
with the `get_interval_hours` helper in ``async_update_options`` — not the
minutes helper `get_interval`. Using the minutes helper applied a custom value
as minutes, polling ~60x too often (see #290).

This is a source-level guard: it parses coordinator.py rather than importing
it, so it runs under plain `unittest` without Home Assistant installed.
"""

import ast
import pathlib
import unittest

COORDINATOR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "mg_saic"
    / "coordinator.py"
)

# The two interval helpers defined inside async_update_options. Assignments to
# holiday_update_interval from anything else (e.g. a timedelta default in
# __init__) are ignored — we only care which helper parses the *option*.
INTERVAL_HELPERS = {"get_interval", "get_interval_hours"}


class HolidayIntervalUnitGuard(unittest.TestCase):
    def test_holiday_interval_parsed_in_hours(self):
        tree = ast.parse(COORDINATOR.read_text(encoding="utf-8"))

        helpers_used = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "holiday_update_interval"
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id in INTERVAL_HELPERS
                ):
                    helpers_used.append(node.value.func.id)

        self.assertIn(
            "get_interval_hours",
            helpers_used,
            "holiday_update_interval should be parsed with get_interval_hours "
            "in async_update_options (found helpers: %r)" % helpers_used,
        )
        self.assertNotIn(
            "get_interval",
            helpers_used,
            "holiday_update_interval must NOT use the minutes helper "
            "get_interval — the holiday interval is configured in hours (#290)",
        )


if __name__ == "__main__":
    unittest.main()
