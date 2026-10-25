"""Battery Capacity (Measured Over Time).

Asked for by an IM6 owner who wanted to know whether 80% still gets him
450 km, or whether his battery is fading -- with evidence he could take to
MG. Estimated Range After Charging can't tell him: it moves with driving,
weather and the target SOC. This works the usable capacity out from the
integration's own measurements on each big charge (energy measured in /
% added) and averages the last five.
"""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR
from test_charge_stats import ts as TRIP
from test_charge_reload import _loaded, _saved

ENTRY = NS(entry_id="entry1")


def _charge(energy, added, *, edges=0.5, samples=12, end="2026-10-18T04:18:50+00:00"):
    return {
        "energy_measured_kWh": energy,
        "energy_measured_estimated_kWh": edges,
        "energy_measured_samples": samples,
        "soc_added_pct": added,
        "charge_end_ts": end,
    }


class SampleTests(unittest.TestCase):
    def test_a_big_charge(self):
        # 25 kWh for 33% added: 75.8 kWh.
        sample = TRIP.capacity_sample(_charge(25.0, 33.0))
        self.assertEqual(sample["capacity_kWh"], 75.8)
        self.assertEqual(sample["soc_added_pct"], 33.0)

    def test_small_charges_do_not_count(self):
        self.assertIsNone(TRIP.capacity_sample(_charge(10.0, 29.9)))

    def test_mostly_filled_in_does_not_count(self):
        # More than a quarter of the energy filled in at the ends.
        self.assertIsNone(TRIP.capacity_sample(_charge(20.0, 40.0, edges=5.1)))

    def test_too_few_readings_do_not_count(self):
        self.assertIsNone(TRIP.capacity_sample(_charge(25.0, 33.0, samples=2)))

    def test_no_measured_energy(self):
        # A charge no poll saw (quiet hours): SOC-based energy only.
        self.assertIsNone(TRIP.capacity_sample({"energy_added_kWh": 20.0, "soc_added_pct": 40.0}))

    def test_nonsense_is_ignored(self):
        self.assertIsNone(TRIP.capacity_sample(_charge(400.0, 40.0)))
        self.assertIsNone(TRIP.capacity_sample(None))

    def test_average_of_the_last_five(self):
        samples = [{"capacity_kWh": v} for v in (80.0, 74.0, 75.0, 76.0, 74.0, 76.0)]
        self.assertEqual(TRIP.measured_capacity(samples), 75.0)
        self.assertIsNone(TRIP.measured_capacity([]))


class ManagerTests(unittest.TestCase):
    def _manager(self):
        return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")

    def test_keeps_the_last_five(self):
        m = self._manager()
        for i in range(7):
            m.note_capacity_sample(_charge(25.0, 33.0, end=f"2026-10-{10 + i}T04:00:00+00:00"))
        self.assertEqual(len(m.capacity_samples), 5)
        self.assertEqual(m.capacity_samples[-1]["at"], "2026-10-16T04:00:00+00:00")

    def test_same_charge_not_counted_twice(self):
        m = self._manager()
        self.assertTrue(m.note_capacity_sample(_charge(25.0, 33.0)))
        self.assertFalse(m.note_capacity_sample(_charge(25.0, 33.0)))
        self.assertEqual(len(m.capacity_samples), 1)

    def test_kept_through_a_restart(self):
        m = self._manager()
        m.note_capacity_sample(_charge(25.0, 33.0))
        self.assertEqual(_loaded(_saved(m)).capacity_samples, m.capacity_samples)

    def test_first_run_starts_from_the_last_charge(self):
        m = self._manager()
        m.last_charge = _charge(25.0, 33.0)
        reloaded = _loaded(_saved(m))
        self.assertEqual(reloaded.capacity_samples[0]["capacity_kWh"], 75.8)

    def test_wired_into_the_charge_close(self):
        source = Path(TRIP.__file__).read_text()
        self.assertIn(
            "        self.last_charge = charge\n        self.note_capacity_sample(charge)\n",
            source,
        )


class SensorTests(unittest.TestCase):
    def _sensor(self, samples, usable=74.3):
        coordinator = NS(
            vin_info=NS(vin="VIN1", brandName="MG", modelName="MGS6 EV"),
            trip_stats=NS(capacity_samples=samples),
            effective_battery_capacity_kwh=usable,
        )
        return SENSOR.SAICMGMeasuredBatteryCapacitySensor(coordinator, ENTRY)

    def test_name_and_id(self):
        s = self._sensor([])
        self.assertEqual(s.name, "MG MGS6 EV Battery Capacity (Measured Over Time)")
        self.assertEqual(s.unique_id, "entry1_VIN1_battery_capacity_measured")

    def test_unknown_until_the_first_big_charge(self):
        s = self._sensor([])
        self.assertIsNone(s.native_value)
        self.assertIsNone(s.extra_state_attributes)
        self.assertTrue(s.available)

    def test_value_and_attributes(self):
        samples = [
            {"capacity_kWh": 72.8, "energy_kWh": 24.0, "soc_added_pct": 33.0, "at": "a"},
            {"capacity_kWh": 73.6, "energy_kWh": 36.8, "soc_added_pct": 50.0, "at": "b"},
        ]
        s = self._sensor(samples)
        self.assertEqual(s.native_value, 73.2)
        attrs = s.extra_state_attributes
        self.assertEqual(attrs["charges_in_average"], 2)
        self.assertEqual(attrs["latest_charge_capacity_kWh"], 73.6)
        self.assertEqual(attrs["latest_charge_at"], "b")
        self.assertEqual(attrs["recent_charges_kWh"], [72.8, 73.6])
        self.assertEqual(attrs["usable_capacity_kWh"], 74.3)
        self.assertEqual(attrs["percent_of_usable"], 98.5)


if __name__ == "__main__":
    unittest.main()
