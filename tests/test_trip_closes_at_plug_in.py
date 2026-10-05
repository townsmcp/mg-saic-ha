"""A trip ends when the driving does, not when the car switches off (#407).

@hoffeck's MG4, 5 Oct 2026: the car stays "on" for a few minutes after
parking, so the cable went in and charging began before the power-off that
closes a trip was seen. Twice in one day:

    trip           charging seen   power-off seen   Last Trip Efficiency
    12:34-13:08    13:06:17 (AC)   13:08:26         unknown
    14:58-15:43    15:41:43 (DC)   15:43:52         unknown

The closing reading had the battery higher than the driving left it -- on the
DC charger higher than at the start of the trip -- so the energy used came
out at nothing or less. His DC readings: 24.9 % at 15:37:26, 24.9 % and
"Connecting" at 15:39:34, 28.7 % and charging at 15:41:43.

Also from his report: two trips of 1 km gave 2.7 and 9.43 km/kWh, because the
odometer moves in whole kilometres.

On the maintainer's MGS6 the same evening the cable was already in at the
closing reading (18:32:36) and charging showed in the next one, 68 seconds
later; its efficiency was right only because the level had not risen yet.
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_charge_stats import ts as TRIP
from test_erac_fallback import SENSOR

CAPACITY = 52.8
KW = dict(capacity_kwh=CAPACITY, tank_litres=None, is_electric=True, is_combustion=False)


def _snap(time, odometer, soc):
    return TRIP.TripSnapshot(
        ts=f"2026-10-05T{time}+00:00", odometer_km=odometer, soc_pct=soc
    )


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


class DCChargeTests(unittest.TestCase):
    """14:58-15:43, 11 km. The cable is seen at 15:39:34, before any charge."""

    def _drive(self):
        manager = _manager()
        manager.open(_snap("14:58:00", 1000.0, 27.4))
        manager.note_trip_reading(_snap("15:20:00", 1006.0, 26.0))
        manager.note_trip_reading(_snap("15:37:26", 1011.0, 24.9))
        return manager

    def test_closed_when_the_cable_is_seen(self):
        manager = self._drive()
        trip = manager.close(_snap("15:39:34", 1011.0, 24.9), **KW, at_plug_in=True)
        self.assertEqual(trip["distance_km"], 11.0)
        self.assertEqual(trip["soc_used_pct"], 2.5)
        self.assertEqual(trip["energy_kWh"], 1.32)
        self.assertEqual(trip["efficiency_km_per_kWh"], 8.33)
        self.assertTrue(trip["closed_at_plug_in"])
        self.assertNotIn("end_soc_before_charging", trip)
        self.assertIsNone(manager.open_snapshot)

    def test_what_happened_before_the_fix(self):
        # Closed at power-off, 15:43:52, with the battery by then above where
        # the trip started: nothing to show.
        manager = self._drive()
        trip = manager.close(_snap("15:43:52", 1011.0, 33.0), **KW)
        self.assertEqual(trip["soc_used_pct"], -5.6)
        self.assertIsNone(trip["efficiency_km_per_kWh"])

    def test_power_off_seen_first_but_already_charging(self):
        # No reading caught the cable going in: the first sign is a closing
        # reading that is already charging.
        manager = self._drive()
        trip = manager.close(_snap("15:43:52", 1011.0, 33.0), **KW, charging=True)
        self.assertEqual(trip["soc_used_pct"], 2.5)
        self.assertEqual(trip["efficiency_km_per_kWh"], 8.33)
        self.assertTrue(trip["end_soc_before_charging"])
        self.assertNotIn("closed_at_plug_in", trip)


class ACChargeTests(unittest.TestCase):
    """12:34-13:08, 9 km. Charging is already under way when the cable is
    first seen (13:06:17)."""

    def test_the_level_before_charging_is_used(self):
        manager = _manager()
        manager.open(_snap("12:34:00", 500.0, 20.7))
        manager.note_trip_reading(_snap("13:04:09", 509.0, 18.6))
        trip = manager.close(
            _snap("13:06:17", 509.0, 18.9), **KW, charging=True, at_plug_in=True
        )
        self.assertEqual(trip["soc_used_pct"], 2.1)
        self.assertEqual(trip["distance_km"], 9.0)
        self.assertEqual(trip["efficiency_km_per_kWh"], round(9.0 / (2.1 / 100 * 52.8), 2))
        self.assertTrue(trip["closed_at_plug_in"])
        self.assertTrue(trip["end_soc_before_charging"])

    def test_the_distance_is_this_readings_not_the_earlier_ones(self):
        # The last cable-out reading was mid-drive; the car has moved since.
        manager = _manager()
        manager.open(_snap("12:34:00", 500.0, 20.7))
        manager.note_trip_reading(_snap("12:50:00", 505.0, 19.5))
        trip = manager.close(
            _snap("13:06:17", 509.0, 19.8), **KW, charging=True, at_plug_in=True
        )
        self.assertEqual(trip["distance_km"], 9.0)
        self.assertEqual(trip["end_ts"], "2026-10-05T13:06:17+00:00")
        self.assertEqual(trip["soc_used_pct"], 1.2)


class WhenNotToAdjustTests(unittest.TestCase):
    def test_a_closing_reading_lower_than_the_last_one_is_kept(self):
        # Cable in, charging "started", but the level has not risen: the
        # closing reading is the better one (the drive went on after the
        # last cable-out reading).
        manager = _manager()
        manager.open(_snap("17:04:19", 5230.0, 68.2))
        manager.note_trip_reading(_snap("17:19:28", 5236.0, 66.7))
        trip = manager.close(_snap("17:32:36", 5241.0, 65.4), **KW, charging=True)
        self.assertEqual(trip["soc_used_pct"], 2.8)
        self.assertNotIn("end_soc_before_charging", trip)

    def test_a_rise_with_no_charging_is_left_alone(self):
        # A hybrid's engine, or a long descent, can raise the level on a
        # drive. Without charging there is nothing to correct.
        manager = _manager()
        manager.open(_snap("10:00:00", 100.0, 40.0))
        manager.note_trip_reading(_snap("10:10:00", 105.0, 39.0))
        trip = manager.close(_snap("10:20:00", 110.0, 41.0), **KW)
        self.assertEqual(trip["soc_used_pct"], -1.0)
        self.assertNotIn("end_soc_before_charging", trip)

    def test_no_reading_between_start_and_a_charging_close(self):
        # Nothing to go on but the start: no energy figure, and it says why.
        manager = _manager()
        manager.open(_snap("10:00:00", 100.0, 40.0))
        trip = manager.close(_snap("10:20:00", 108.0, 44.0), **KW, charging=True)
        self.assertEqual(trip["soc_used_pct"], 0.0)
        self.assertIsNone(trip["efficiency_km_per_kWh"])
        self.assertTrue(trip["end_soc_before_charging"])

    def test_the_next_trip_starts_from_the_real_reading(self):
        manager = _manager()
        manager.open(_snap("12:34:00", 500.0, 20.7))
        manager.note_trip_reading(_snap("13:04:09", 509.0, 18.6))
        closing = _snap("13:06:17", 509.0, 18.9)
        manager.close(closing, **KW, charging=True, at_plug_in=True)
        self.assertIs(manager.last_parked_snapshot, closing)
        self.assertIsNone(manager.open_trip_unplugged)

    def test_a_new_trip_does_not_inherit_the_last_ones_reading(self):
        manager = _manager()
        manager.open(_snap("12:34:00", 500.0, 20.7))
        manager.note_trip_reading(_snap("13:04:09", 509.0, 18.6))
        manager.close(_snap("13:08:00", 509.0, 18.6), **KW)
        manager.open(_snap("14:58:00", 509.0, 27.0))
        self.assertIsNone(manager.open_trip_unplugged)

    def test_readings_are_ignored_with_no_trip_open(self):
        manager = _manager()
        manager.note_trip_reading(_snap("13:04:09", 509.0, 18.6))
        self.assertIsNone(manager.open_trip_unplugged)


class ShortTripTests(unittest.TestCase):
    def _trip(self, km, soc_used):
        return TRIP.compute_completed_trip(
            _snap("10:00:00", 100.0, 50.0),
            _snap("10:10:00", 100.0 + km, 50.0 - soc_used),
            **KW,
        )

    def test_a_one_km_trip_has_no_headline_efficiency(self):
        trip = self._trip(1.0, 0.7)
        self.assertTrue(trip["short_trip"])
        for key in ("efficiency_km_per_kWh", "efficiency_mi_per_kWh",
                    "consumption_kWh_per_100km", "consumption_kWh_per_100mi"):
            self.assertIsNone(trip[key], key)

    def test_the_rest_of_it_is_still_there(self):
        trip = self._trip(1.0, 0.7)
        self.assertEqual(trip["distance_km"], 1.0)
        self.assertEqual(trip["soc_used_pct"], 0.7)
        self.assertEqual(trip["energy_kWh"], 0.37)
        # The figure he saw for one of his 1 km trips.
        self.assertEqual(trip["efficiency_km_per_kWh_soc"], 2.7)

    def test_the_threshold(self):
        self.assertEqual(TRIP.MIN_EFFICIENCY_TRIP_KM, 3.0)
        self.assertTrue(self._trip(2.0, 0.5)["short_trip"])
        at = self._trip(3.0, 0.8)
        self.assertNotIn("short_trip", at)
        self.assertIsNotNone(at["efficiency_km_per_kWh"])

    def test_his_twelve_km_trip_is_unchanged(self):
        # 10:26-10:54, 23.6 -> 20.7 %, which he checked by hand.
        trip = TRIP.compute_completed_trip(
            _snap("10:26:00", 200.0, 23.6), _snap("10:54:00", 212.0, 20.7), **KW
        )
        self.assertEqual(trip["efficiency_km_per_kWh"], 7.84)
        self.assertNotIn("short_trip", trip)


class StorageTests(unittest.TestCase):
    def _round_trip(self, manager):
        saved = {}

        class _Save:
            async def async_save(self, data):
                saved.update(data)

        manager._store = _Save()
        asyncio.run(manager.async_save())

        class _Load:
            async def async_load(self):
                return dict(saved)

        storage = sys.modules.get("homeassistant.helpers.storage")
        original = getattr(storage, "Store", None) if storage else None
        module = storage or NS()
        module.Store = lambda *_a, **_k: _Load()
        helpers = sys.modules.setdefault("homeassistant.helpers", NS())
        sys.modules.setdefault("homeassistant", NS(helpers=helpers))
        sys.modules["homeassistant.helpers.storage"] = module
        reloaded = _manager()
        try:
            asyncio.run(reloaded.async_load())
        finally:
            if storage is None:
                sys.modules.pop("homeassistant.helpers.storage", None)
            elif original is not None:
                storage.Store = original
        return saved, reloaded

    def test_survives_a_restart_mid_drive(self):
        manager = _manager()
        manager.open(_snap("12:34:00", 500.0, 20.7))
        manager.note_trip_reading(_snap("13:04:09", 509.0, 18.6))
        saved, reloaded = self._round_trip(manager)
        self.assertEqual(saved["open_trip_unplugged"]["soc_pct"], 18.6)
        trip = reloaded.close(_snap("13:08:26", 509.0, 19.4), **KW, charging=True)
        self.assertEqual(trip["soc_used_pct"], 2.1)

    def test_not_stored_or_restored_without_an_open_trip(self):
        manager = _manager()
        manager.open_trip_unplugged = _snap("13:04:09", 509.0, 18.6)
        saved, reloaded = self._round_trip(manager)
        self.assertIsNone(saved["open_trip_unplugged"])
        saved["open_trip_unplugged"] = _snap("13:04:09", 509.0, 18.6).to_dict()
        self.assertIsNone(reloaded.open_trip_unplugged)


class CoordinatorTests(unittest.TestCase):
    """coordinator._update_trip_state decides when to open and close."""

    def setUp(self):
        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        self.cls = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator

    def _charging(self, status, gun):
        return NS(
            chrgMgmtData=NS(bmsChrgSts=status),
            rvsChargeStatus=NS(chargingGunState=gun),
        )

    def _run(self, power_mode, charging_data, *, trip_open):
        c = self.cls.__new__(self.cls)
        c.vin = "VIN"
        c.vehicle_type = "BEV"
        c.fuel_tank_override = None
        c.known_fuel_tank_litres = None
        c.trip_stats = MagicMock()
        c.trip_stats.open_snapshot = "start" if trip_open else None
        c.trip_stats.note_since_charge.return_value = False
        c.trip_stats.force_close_if_stale.return_value = None
        c.trip_stats.note_soc_reset_baseline.return_value = False
        c.trip_stats.open.return_value = True
        c._extract_since_charge = MagicMock(return_value=(None, None))
        c._trip_snapshot = MagicMock(return_value=NS(soc_pct=50.0, odometer_km=1.0, ts="t"))
        c.resolve_battery_capacity_for = MagicMock(return_value=(CAPACITY, "override"))
        c._schedule_trip_save = MagicMock()
        self.cls._update_trip_state(c, power_mode, NS(), charging_data)
        return c.trip_stats

    def test_cable_state(self):
        state = self.cls._cable_state
        self.assertEqual(state(None), (False, False))
        self.assertEqual(state(self._charging(0, 0)), (False, False))
        # One signal alone is not enough.
        self.assertEqual(state(self._charging(0, 1)), (False, False))
        self.assertEqual(state(self._charging(5, 0)), (False, False))
        self.assertEqual(state(self._charging(5, 1)), (True, False))   # Connecting
        self.assertEqual(state(self._charging(8, 1)), (True, False))   # Stopped
        self.assertEqual(state(self._charging(2, 1)), (True, False))   # Finished
        self.assertEqual(state(self._charging(1, 1)), (True, True))    # AC
        self.assertEqual(state(self._charging(10, 1)), (True, True))   # DC
        # Charging says it all, whatever the gun field says.
        self.assertEqual(state(self._charging(1, 0)), (True, True))
        # Giving power back is not charging, but the cable is in.
        self.assertEqual(state(self._charging(13, 1)), (True, False))

    def test_on_and_plugged_in_closes_the_open_trip(self):
        stats = self._run(2, self._charging(5, 1), trip_open=True)
        kwargs = stats.close.call_args.kwargs
        self.assertTrue(kwargs["at_plug_in"])
        self.assertFalse(kwargs["charging"])
        stats.note_trip_reading.assert_not_called()

    def test_on_and_already_charging_says_so(self):
        stats = self._run(2, self._charging(10, 1), trip_open=True)
        self.assertTrue(stats.close.call_args.kwargs["charging"])

    def test_on_and_unplugged_remembers_the_reading(self):
        stats = self._run(2, self._charging(0, 0), trip_open=True)
        stats.close.assert_not_called()
        stats.note_trip_reading.assert_called_once()

    def test_on_with_no_charging_data_still_remembers_the_reading(self):
        stats = self._run(2, None, trip_open=True)
        stats.close.assert_not_called()
        stats.note_trip_reading.assert_called_once()

    def test_no_trip_opens_while_the_cable_is_in(self):
        stats = self._run(2, self._charging(1, 1), trip_open=False)
        stats.open.assert_not_called()

    def test_a_trip_still_opens_normally(self):
        stats = self._run(2, self._charging(0, 0), trip_open=False)
        stats.open.assert_called_once()

    def test_power_off_close_passes_the_charging_state(self):
        stats = self._run(0, self._charging(1, 1), trip_open=True)
        kwargs = stats.close.call_args.kwargs
        self.assertTrue(kwargs["charging"])
        self.assertNotIn("at_plug_in", kwargs)
        stats = self._run(0, self._charging(0, 0), trip_open=True)
        self.assertFalse(stats.close.call_args.kwargs["charging"])


class AttributeTests(unittest.TestCase):
    def test_the_new_flags_reach_the_sensors(self):
        for key in ("short_trip", "closed_at_plug_in", "end_soc_before_charging"):
            self.assertIn(key, SENSOR._TRIP_ATTR_KEYS)


if __name__ == "__main__":
    unittest.main()
