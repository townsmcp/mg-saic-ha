"""MG ZS Hybrid+ (2025, series 'ZS32 EU') profile (#441).

@Jarinox1's tests, 18 Oct 2026, on the default profile:

    iSmart app HIGH     remoteClimateStatus 2, engine started (heat)
    iSmart app LOW      remoteClimateStatus 2
    HA Cool / Heat      control_climate echoed as status 3, A/C light on,
                        but the fan never blew
    windows             only the driver window reported (app too)
    fuel tank           41 L

The same as the MG3 Hybrid+ (ZP22, #258), whose hybrid system it shares,
apart from the temperature scale. Decrypted iSmart traffic, 21 Oct 2026:

    LOW   paramId 20 = 1
    18 C  4   ...   32 C  18      (temp - 14)
    HIGH  19                      2 and 3 never sent
"""

import sys
import unittest

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

CONST = sys.modules["mg_saic.const"]
COORD_CLS = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
CLIMATE_SETTINGS = (
    "climate_status_cool",
    "climate_status_fan_only",
    "cool_uses_start_ac",
    "has_front_defrost",
    "has_front_passenger_window",
    "has_rear_windows",
    "supports_target_soc",
)


def _profiled(series):
    c = COORD_CLS.__new__(COORD_CLS)
    c.vehicle_series = series
    c.battery_capacity_override = None
    COORD_CLS._apply_vehicle_profile(c)
    return c


class ZsHybridProfileTests(unittest.TestCase):
    def test_the_car_gets_its_own_profile(self):
        matches = [key for key in CONST.VEHICLE_PROFILES if key in "ZS32 EU"]
        self.assertEqual(matches, ["ZS32"])

    def test_simple_ac_like_the_mg3_hybrid(self):
        zs = CONST.VEHICLE_PROFILES["ZS32"]
        mg3 = CONST.VEHICLE_PROFILES["ZP22"]
        for key in CLIMATE_SETTINGS:
            self.assertEqual(zs.get(key), mg3.get(key), key)

    def test_the_coordinator_picks_it_up(self):
        c = _profiled("ZS32 EU")
        self.assertTrue(c.cool_uses_start_ac)
        self.assertEqual(c.climate_status_cool, {2})
        self.assertEqual(c.climate_status_fan_only, set())
        self.assertFalse(c.has_front_defrost)
        self.assertFalse(c.has_front_passenger_window)
        self.assertFalse(c.has_rear_windows)
        self.assertFalse(c.supports_target_soc)

    def test_temperatures_as_the_app_sends_them(self):
        c = _profiled("ZS32 EU")
        c.temp_idx_inverted = False
        self.assertEqual((c.min_temp, c.max_temp), (17, 33))
        sent = {t: COORD_CLS.get_ac_temperature_idx(c, t) for t in range(17, 34)}
        self.assertEqual(sent[17], 1)  # LOW: Cool and the Low preset
        self.assertEqual(sent[18], 4)
        self.assertEqual(sent[23], 9)
        self.assertEqual(sent[32], 18)
        self.assertEqual(sent[33], 19)  # HIGH: Heat and the High preset
        self.assertNotIn(2, sent.values())
        self.assertNotIn(3, sent.values())
        # Out of range is clamped to LOW / HIGH.
        self.assertEqual(COORD_CLS.get_ac_temperature_idx(c, 10), 1)
        self.assertEqual(COORD_CLS.get_ac_temperature_idx(c, 40), 19)

    def test_fuel_tank(self):
        self.assertEqual(CONST.VEHICLE_PROFILES["ZS32"]["fuel_tank_litres"], 41.0)

    def test_the_mg3_hybrid_is_unchanged(self):
        c = _profiled("ZP22 EU")
        self.assertTrue(c.cool_uses_start_ac)
        self.assertEqual((c.min_temp, c.max_temp, c.temp_offset), (16, 30, 2))
        self.assertIsNone(CONST.VEHICLE_PROFILES["ZP22"].get("temp_index_map"))
        self.assertEqual(CONST.VEHICLE_PROFILES["ZP22"]["fuel_tank_litres"], 36.0)


if __name__ == "__main__":
    unittest.main()
