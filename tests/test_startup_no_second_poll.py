"""Home Assistant starting while the car is asleep: no second poll (#425).

James's MGS6, 15 Oct 2026, 1.4.0-beta11, car asleep:

    12:43:12  first refresh starts
    12:43:30  status didn't answer within 15 s; setup carries on (beta8)
    12:43:30  the sensor, binary sensor and device tracker platforms are
              added with update_before_add=True. For a CoordinatorEntity that
              asks the coordinator for a refresh -- a whole new poll, no
              longer under the startup rules, with 5 retries on code 4
    12:47:38  that poll gives up; setup finishes

Home Assistant's startup waited 4 minutes 8 seconds on it. Before beta8 a
sleeping car failed setup after 30 s instead, so this never showed; with
the car awake it cost a second poll at every start.

The entities read the coordinator's data, which the first refresh has
already fetched, so they're added without an update.
"""

import re
import unittest
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"


class NoUpdateBeforeAddTests(unittest.TestCase):
    def test_no_platform_refreshes_when_adding_entities(self):
        for path in sorted(PKG_DIR.glob("*.py")):
            source = path.read_text()
            for call in re.findall(r"async_add_entities\([^)]*\)", source):
                self.assertNotIn("update_before_add", call, path.name)
                self.assertNotRegex(call, r",\s*True\s*\)$", path.name)


if __name__ == "__main__":
    unittest.main()
