"""Battery Capacity from several smaller charges in one stay (mgevs.com thread).

An MG IM6 (~100 kWh) charging from a socket at ~2 kW, following its owner's
solar and avoiding the 4-8 pm peak. 30% is ~15 hours of charging, which only
happens across a weekend, in parts: the charge stops whenever the sun goes or a
pause passes 20 minutes, so each part was under 30% and none counted.

From 2.0.0-beta08 the parts since the car was last driven add up: their energy
and their own % added (so battery drain while paused isn't counted), counting
once the total reaches 30%.
"""

import asyncio
import sys
import types
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from test_charge_stats import ts as TRIP


def _charge(end, added, energy, *, odometer=6510.0, samples=6, estimated=0.0, **extra):
    return {
        "charge_end_ts": end,
        "soc_added_pct": added,
        "energy_measured_kWh": energy,
        "energy_measured_estimated_kWh": estimated,
        "energy_measured_samples": samples,
        "odometer_km": odometer,
        **extra,
    }


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


# A weekend: Saturday morning, Saturday afternoon, Sunday. 12 + 10 + 9 = 31%,
# 11.4 + 9.6 + 8.5 = 29.5 kWh -> 95.2 kWh.
SAT_AM = _charge("2026-11-28T12:00:00+00:00", 12.0, 11.4)
SAT_PM = _charge("2026-11-28T15:30:00+00:00", 10.0, 9.6)
SUN = _charge("2026-11-29T15:00:00+00:00", 9.0, 8.5)


class CombiningTests(unittest.TestCase):
    def test_a_weekend_of_solar_top_ups_counts(self):
        m = _manager()
        self.assertFalse(m.note_capacity_sample(SAT_AM))
        self.assertFalse(m.note_capacity_sample(SAT_PM))
        self.assertEqual(len(m.capacity_parts), 2)
        self.assertTrue(m.note_capacity_sample(SUN))
        sample = m.capacity_samples[-1]
        self.assertEqual(sample["soc_added_pct"], 31.0)
        self.assertEqual(sample["energy_kWh"], 29.5)
        self.assertEqual(sample["capacity_kWh"], 95.2)
        self.assertEqual(sample["charges"], 3)
        self.assertEqual(sample["at"], SUN["charge_end_ts"])
        self.assertEqual(m.capacity_parts, [])

    def test_driving_in_between_starts_again(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        m.note_capacity_sample(SAT_PM)
        driven = dict(SUN, odometer_km=6532.0, soc_added_pct=20.0)
        self.assertFalse(m.note_capacity_sample(driven))
        self.assertEqual(len(m.capacity_parts), 1)
        self.assertEqual(m.capacity_parts[0]["soc_added_pct"], 20.0)

    def test_too_long_ago_starts_again(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        five_days = dict(SUN, charge_end_ts="2026-12-03T12:00:01+00:00", soc_added_pct=25.0)
        self.assertFalse(m.note_capacity_sample(five_days))
        self.assertEqual(len(m.capacity_parts), 1)

    def test_a_badly_measured_part_is_left_out_entirely(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        guessed = _charge("2026-11-28T14:00:00+00:00", 8.0, 7.0, estimated=5.0)
        self.assertFalse(m.note_capacity_sample(guessed))
        self.assertEqual(len(m.capacity_parts), 1)
        m.note_capacity_sample(SAT_PM)
        self.assertTrue(m.note_capacity_sample(SUN))
        self.assertEqual(m.capacity_samples[-1]["soc_added_pct"], 31.0)

    def test_a_badly_measured_part_after_driving_starts_again(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        m.note_capacity_sample(_charge("2026-11-28T14:00:00+00:00", 8.0, 7.0, odometer=6540.0, samples=1))
        self.assertEqual(m.capacity_parts, [])

    def test_unseen_ends_or_one_reading_dont_count(self):
        for bad in (
            dict(SAT_PM, energy_measured_unseen_s=7200),
            dict(SAT_PM, energy_measured_samples=1),
            dict(SAT_PM, energy_measured_kWh=None),
            dict(SAT_PM, soc_added_pct=0.0),
        ):
            m = _manager()
            m.note_capacity_sample(SAT_AM)
            m.note_capacity_sample(bad)
            self.assertEqual(len(m.capacity_parts), 1, bad)

    def test_no_odometer_no_combining(self):
        m = _manager()
        m.note_capacity_sample(dict(SAT_AM, odometer_km=None))
        m.note_capacity_sample(dict(SAT_PM, odometer_km=None))
        self.assertEqual(len(m.capacity_parts), 1)

    def test_the_same_charge_twice_is_counted_once(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        self.assertFalse(m.note_capacity_sample(SAT_AM))
        self.assertEqual(len(m.capacity_parts), 1)

    def test_too_few_readings_in_total_waits(self):
        m = _manager()
        m.note_capacity_sample(_charge("2026-11-28T12:00:00+00:00", 31.0, 29.0, samples=2))
        # On its own: 31% but only 2 readings, so not a sample; as a part, 2 < 3.
        self.assertEqual(m.capacity_samples, [])
        self.assertEqual(len(m.capacity_parts), 1)


class UnchangedTests(unittest.TestCase):
    def test_a_big_charge_still_counts_by_itself(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        big = _charge("2026-11-29T15:00:00+00:00", 40.0, 38.0)
        self.assertTrue(m.note_capacity_sample(big))
        sample = m.capacity_samples[-1]
        self.assertEqual(sample["soc_added_pct"], 40.0)
        self.assertNotIn("charges", sample)
        self.assertEqual(m.capacity_parts, [])

    def test_a_single_small_charge_still_doesnt_count(self):
        m = _manager()
        self.assertFalse(m.note_capacity_sample(SAT_AM))
        self.assertEqual(m.capacity_samples, [])


def _load(data):
    """A fresh manager loaded from ``data``, as at a restart."""
    store = MagicMock(async_load=AsyncMock(return_value=data))
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = MagicMock(return_value=store)
    fresh = _manager()
    with patch.dict(
        sys.modules,
        {
            "homeassistant": sys.modules.get("homeassistant") or types.ModuleType("homeassistant"),
            "homeassistant.helpers": sys.modules.get("homeassistant.helpers")
            or types.ModuleType("homeassistant.helpers"),
            "homeassistant.helpers.storage": storage,
        },
    ):
        asyncio.run(fresh.async_load())
    return fresh


class StorageTests(unittest.TestCase):
    def test_kept_across_a_restart(self):
        m = _manager()
        m._store = MagicMock(async_save=AsyncMock())
        m.note_capacity_sample(SAT_AM)
        asyncio.run(m.async_save())
        saved = m._store.async_save.await_args.args[0]
        self.assertEqual(len(saved["capacity_parts"]), 1)

        fresh = _load(saved)
        self.assertEqual(fresh.capacity_parts, m.capacity_parts)
        fresh.note_capacity_sample(SAT_PM)
        self.assertTrue(fresh.note_capacity_sample(SUN))

    def test_old_stores_without_it_load(self):
        self.assertEqual(_load({}).capacity_parts, [])


class RemoveTests(unittest.TestCase):
    def test_a_combined_charge_can_be_removed(self):
        m = _manager()
        for c in (SAT_AM, SAT_PM, SUN):
            m.note_capacity_sample(c)
        self.assertIsNotNone(m.remove_capacity_sample(SUN["charge_end_ts"]))
        self.assertEqual(m.capacity_samples, [])


class SensorTests(unittest.TestCase):
    def _sensor(self, stats):
        from test_erac_fallback import SENSOR

        coordinator = NS(
            vin_info=NS(vin="VIN1", brandName="MG", modelName="IM6"),
            trip_stats=stats,
            effective_battery_capacity_kwh=None,
        )
        return SENSOR.SAICMGMeasuredBatteryCapacitySensor(coordinator, NS(entry_id="e1"))

    def test_adding_up_shown_before_the_first_sample(self):
        m = _manager()
        m.note_capacity_sample(SAT_AM)
        m.note_capacity_sample(SAT_PM)
        attrs = self._sensor(m).extra_state_attributes
        self.assertEqual(attrs, {"adding_up_charges": 2, "adding_up_soc_added_pct": 22.0})

    def test_a_combined_charge_says_how_many(self):
        m = _manager()
        for c in (SAT_AM, SAT_PM, SUN):
            m.note_capacity_sample(c)
        attrs = self._sensor(m).extra_state_attributes
        self.assertEqual(attrs["recent_charges"][-1]["charges"], 3)
        self.assertNotIn("adding_up_charges", attrs)


if __name__ == "__main__":
    unittest.main()
