"""Battery Health (Estimated).

Asked for on 2 Nov 2026. An IM6 owner's Battery Capacity (Measured Over Time)
read 97.8 then 99.2 kWh, and its percent_of_usable showed over 100% (108% on
an MGS6). That % compares two figures counted differently, so it can't show
health. This compares the car with itself: today's five-charge average as a %
of the average of its first five, or of an official health % (a dealer's or a
battery test's) the owner enters in Configure. Never above 100%.
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_erac_fallback import SENSOR
from test_charge_stats import ts as TRIP
from test_charge_reload import _loaded, _saved

CF = sys.modules["mg_saic.config_flow"]
CONST = sys.modules["mg_saic.const"]
COORD_MOD = sys.modules["mg_saic.coordinator"]
HEALTH = CONST.CONF_OFFICIAL_BATTERY_HEALTH
ENTRY = NS(entry_id="entry1")


def _charge(capacity, day):
    # energy / % added * 100 == capacity, on a 40% charge.
    return {
        "energy_measured_kWh": capacity * 0.4,
        "energy_measured_estimated_kWh": 0.5,
        "energy_measured_samples": 12,
        "soc_added_pct": 40.0,
        "charge_end_ts": f"2026-11-{day:02d}T04:00:00+00:00",
    }


def _manager(*capacities):
    m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
    for day, capacity in enumerate(capacities, start=1):
        m.note_capacity_sample(_charge(capacity, day))
    return m


class HealthTests(unittest.TestCase):
    def _samples(self, *capacities):
        return [{"capacity_kWh": c} for c in capacities]

    def test_nothing_to_go_on(self):
        self.assertIsNone(TRIP.battery_health([], None, None))
        self.assertIsNone(TRIP.battery_health(self._samples(97.8, 99.2), None, None))

    def test_against_the_first_charges(self):
        baseline = {"capacity_kWh": 98.0}
        health = TRIP.battery_health(self._samples(88.2) * 5, baseline, None)
        self.assertEqual(health["value"], 90)
        self.assertEqual(health["source"], "measured")
        self.assertEqual(health["current_kWh"], 88.2)

    def test_never_above_100(self):
        baseline = {"capacity_kWh": 98.0}
        health = TRIP.battery_health(self._samples(99.5) * 5, baseline, None)
        self.assertEqual(health["value"], 100)

    def test_official_reading_before_enough_charges(self):
        official = {"pct": 92.0, "capacity_kWh": None}
        health = TRIP.battery_health(self._samples(97.8), None, official)
        self.assertEqual(health["value"], 92)
        self.assertEqual(health["source"], "official")

    def test_official_reading_followed_from_there(self):
        official = {"pct": 92.0, "capacity_kWh": 70.0}
        health = TRIP.battery_health(self._samples(66.5) * 5, {"capacity_kWh": 75.0}, official)
        # 92% x 66.5 / 70.0 = 87.4
        self.assertEqual(health["value"], 87)
        self.assertEqual(health["reference_kWh"], 70.0)

    def test_official_reading_capped_too(self):
        official = {"pct": 97.0, "capacity_kWh": 70.0}
        health = TRIP.battery_health(self._samples(73.0) * 5, None, official)
        self.assertEqual(health["value"], 100)


class BaselineTests(unittest.TestCase):
    def test_set_by_the_fifth_charge(self):
        m = _manager(97.8, 99.2, 98.0, 97.0)
        self.assertIsNone(m.health_baseline)
        m.note_capacity_sample(_charge(98.0, 5))
        self.assertEqual(m.health_baseline["capacity_kWh"], 98.0)
        self.assertEqual(m.health_baseline["charges"], 5)
        self.assertEqual(m.health_baseline["set_at"], "2026-11-05T04:00:00+00:00")

    def test_kept_as_later_charges_come_in(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0, 93.0, 93.0)
        self.assertEqual(m.health_baseline["capacity_kWh"], 98.0)
        health = TRIP.battery_health(m.capacity_samples, m.health_baseline, None)
        # Average now 96.0: 98%.
        self.assertEqual(health["value"], 98)

    def test_existing_charges_become_the_baseline_on_upgrade(self):
        m = _manager(78.0, 78.5, 78.3, 78.4, 78.3)
        saved = _saved(m)
        saved.pop("health_baseline")
        reloaded = _loaded(saved)
        self.assertEqual(reloaded.health_baseline["capacity_kWh"], 78.3)

    def test_kept_through_a_restart(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        m.set_official_health(94.0, "2026-11-06T10:00:00+00:00")
        reloaded = _loaded(_saved(m))
        self.assertEqual(reloaded.health_baseline, m.health_baseline)
        self.assertEqual(reloaded.health_official, m.health_official)


class OfficialReadingTests(unittest.TestCase):
    def test_measured_against_the_charges_as_they_stand(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        self.assertTrue(m.set_official_health(94.0, "now"))
        self.assertEqual(m.health_official["capacity_kWh"], 98.0)
        self.assertEqual(m.health_official["entered_at"], "now")

    def test_waits_for_enough_charges(self):
        m = _manager(97.8, 99.2)
        m.set_official_health(94.0, "now")
        self.assertIsNone(m.health_official["capacity_kWh"])
        for day in (3, 4, 5):
            m.note_capacity_sample(_charge(98.0, day))
        self.assertEqual(m.health_official["capacity_kWh"], 98.2)

    def test_same_figure_keeps_its_reference(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        m.set_official_health(94.0, "first")
        for day in (6, 7):
            m.note_capacity_sample(_charge(90.0, day))
        self.assertFalse(m.set_official_health(94.0, "second"))
        self.assertEqual(m.health_official["entered_at"], "first")
        self.assertEqual(m.health_official["capacity_kWh"], 98.0)

    def test_new_figure_measured_afresh(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        m.set_official_health(94.0, "first")
        for day in (6, 7, 8, 9, 10):
            m.note_capacity_sample(_charge(90.0, day))
        self.assertTrue(m.set_official_health(88.0, "second"))
        self.assertEqual(m.health_official["capacity_kWh"], 90.0)

    def test_cleared_goes_back_to_the_first_charges(self):
        m = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        m.set_official_health(94.0, "now")
        self.assertTrue(m.set_official_health(None))
        self.assertIsNone(m.health_official)
        self.assertFalse(m.set_official_health(None))
        self.assertEqual(m.health_baseline["capacity_kWh"], 98.0)


class SensorTests(unittest.TestCase):
    def _sensor(self, stats):
        coordinator = NS(
            vin_info=NS(vin="VIN1", brandName="MG", modelName="IM6"),
            trip_stats=stats,
        )
        return SENSOR.SAICMGBatteryHealthSensor(coordinator, ENTRY)

    def test_name_and_id(self):
        s = self._sensor(_manager())
        self.assertEqual(s.name, "MG IM6 Battery Health (Estimated)")
        self.assertEqual(s.unique_id, "entry1_VIN1_battery_health")
        self.assertEqual(s._attr_native_unit_of_measurement, SENSOR.PERCENTAGE)

    def test_unknown_until_five_charges(self):
        s = self._sensor(_manager(97.8, 99.2))
        self.assertIsNone(s.native_value)
        self.assertTrue(s.available)
        self.assertEqual(
            s.extra_state_attributes,
            {"estimate": True, "source": None, "charges_until_baseline": 3},
        )

    def test_measured(self):
        s = self._sensor(_manager(98.0, 98.0, 98.0, 98.0, 98.0, 93.0, 93.0))
        self.assertEqual(s.native_value, 98)
        attrs = s.extra_state_attributes
        self.assertEqual(attrs["source"], "measured")
        self.assertEqual(attrs["current_capacity_kWh"], 96.0)
        self.assertEqual(attrs["reference_capacity_kWh"], 98.0)
        self.assertEqual(attrs["baseline_set_at"], "2026-11-05T04:00:00+00:00")
        self.assertTrue(attrs["estimate"])

    def test_official_waiting_for_charges(self):
        m = _manager(97.8)
        m.set_official_health(92.0, "2026-11-02T08:00:00+00:00")
        s = self._sensor(m)
        self.assertEqual(s.native_value, 92)
        attrs = s.extra_state_attributes
        self.assertEqual(attrs["source"], "official")
        self.assertEqual(attrs["official_health_pct"], 92.0)
        self.assertEqual(attrs["official_entered_at"], "2026-11-02T08:00:00+00:00")
        self.assertEqual(attrs["charges_until_baseline"], 4)

    def test_registered_next_to_the_capacity_sensor(self):
        source = Path(SENSOR.__file__).read_text()
        self.assertIn(
            "SAICMGMeasuredBatteryCapacitySensor(coordinator, entry))\n"
            "                # That capacity as a % of the car's own early figure (or of an\n"
            "                # official health reading), to watch for battery fade.\n"
            "                sensors.append(SAICMGBatteryHealthSensor(coordinator, entry))",
            source,
        )


class OptionTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(CONST.parse_official_health("92"), 92.0)
        self.assertEqual(CONST.parse_official_health(88.5), 88.5)
        for raw in (None, "", "abc", 0, 101):
            self.assertIsNone(CONST.parse_official_health(raw), raw)

    def _submit(self, value):
        flow = CF.SAICMGOptionsFlowHandler.__new__(CF.SAICMGOptionsFlowHandler)
        flow.config_entry = NS(options={}, data={})
        flow._validate_abrp = AsyncMock(return_value={})
        flow.async_create_entry = lambda title, data: {"data": data}
        flow.async_show_form = lambda **kw: kw
        return asyncio.run(flow.async_step_init({HEALTH: value}))

    def test_saved_as_a_number(self):
        self.assertEqual(self._submit("92")["data"][HEALTH], 92.0)
        self.assertEqual(self._submit("88.5%")["data"][HEALTH], 88.5)

    def test_blank_clears_it(self):
        self.assertNotIn(HEALTH, self._submit("")["data"])

    def test_bad_values(self):
        self.assertEqual(self._submit("lots")["errors"], {HEALTH: "health_invalid"})
        self.assertEqual(self._submit("120")["errors"], {HEALTH: "health_out_of_range"})

    def test_text_in_every_language(self):
        folder = Path(CF.__file__).parent / "translations"
        for path in sorted(folder.glob("*.json")):
            options = json.loads(path.read_text())["options"]
            init = options["step"]["init"]
            self.assertIn(HEALTH, init["data"], path.name)
            self.assertIn(HEALTH, init["data_description"], path.name)
            self.assertIn("health_invalid", options["error"], path.name)
            self.assertIn("health_out_of_range", options["error"], path.name)


class CoordinatorTests(unittest.TestCase):
    def _coordinator(self, stats):
        c = COORD_MOD.SAICMGDataUpdateCoordinator.__new__(COORD_MOD.SAICMGDataUpdateCoordinator)
        c.vin = "VIN1"
        c.trip_stats = stats
        c._schedule_trip_save = MagicMock()
        return c

    def test_option_handed_to_the_stats_and_saved(self):
        stats = _manager(98.0, 98.0, 98.0, 98.0, 98.0)
        c = self._coordinator(stats)
        c._apply_official_battery_health({HEALTH: 94.0})
        self.assertEqual(stats.health_official["pct"], 94.0)
        c._schedule_trip_save.assert_called_once()
        # Saving Configure again without changing it changes nothing.
        c._apply_official_battery_health({HEALTH: 94.0})
        c._schedule_trip_save.assert_called_once()

    def test_no_stats_is_harmless(self):
        self._coordinator(None)._apply_official_battery_health({HEALTH: 94.0})

    def test_called_at_startup_and_on_options_save(self):
        source = Path(COORD_MOD.__file__).read_text()
        self.assertIn("self._apply_official_battery_health(self.config_entry.options)", source)
        self.assertIn("self._apply_official_battery_health(options)", source)


if __name__ == "__main__":
    unittest.main()
