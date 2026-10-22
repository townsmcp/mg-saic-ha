"""Efficiency Since Last Charge (Driving Only) and Parked Energy Since Last
Charge.

A MG IM6 owner, Oct 2026, after 18 hours parked: the car's own since-charge
counters said 6.2 kWh over 26 km (16 miles), 23.9 kWh/100km, because they
count the energy used while parked too. The drives added up leave that out.
"""

import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from test_charge_reload import _loaded, _saved
from test_charge_stats import ts as TRIP

CAPACITY = 66.0


def _snap(ts, odo, soc, km, kwh):
    return TRIP.TripSnapshot(
        ts=f"2026-10-15T{ts}:00+00:00", odometer_km=odo, soc_pct=soc,
        since_charge_km=km, since_charge_kwh=kwh,
    )


def _drive(manager, start, end):
    manager.open(start)
    return manager.close(
        end, capacity_kwh=CAPACITY, tank_litres=None,
        is_electric=True, is_combustion=False,
    )


def _michaels_day():
    """Charged, a 10 km drive, 18 hours parked, a 16 km drive."""
    m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
    _drive(m, _snap("07:00", 3910.0, 80.0, 0.0, 0.0),
           _snap("07:20", 3920.0, 77.6, 10.0, 1.6))
    # Parked: the counter goes from 1.6 to 3.3 kWh with no distance.
    _drive(m, _snap("13:00", 3920.0, 75.0, 10.0, 3.3),
           _snap("13:30", 3936.0, 70.6, 26.0, 6.2))
    return m


class MichaelsDayTests(unittest.TestCase):
    def setUp(self):
        self.m = _michaels_day()
        self.result = TRIP.compute_driving_since_charge(
            self.m.driving_since_charge, 26.0, 6.2
        )

    def test_driving_only(self):
        r = self.result
        self.assertEqual(r["trips"], 2)
        self.assertEqual(r["driving_distance_km"], 26.0)
        self.assertEqual(r["driving_energy_kWh"], 4.5)
        self.assertEqual(r["consumption_kWh_per_100km"], 17.31)
        self.assertEqual(r["efficiency_km_per_kWh"], 5.78)

    def test_parked(self):
        self.assertEqual(self.result["parked_energy_kWh"], 1.7)

    def test_against_driving_and_parked(self):
        # What Efficiency Since Last Charge (Driving and Parked) shows.
        both = TRIP.compute_since_charge_efficiency(26.0, 6.2)
        self.assertEqual(both["consumption_kWh_per_100km"], 23.85)
        self.assertEqual(self.result["energy_since_charge_kWh"], 6.2)

    def test_parked_more_overnight(self):
        r = TRIP.compute_driving_since_charge(self.m.driving_since_charge, 26.0, 6.6)
        self.assertEqual(r["parked_energy_kWh"], 2.1)
        self.assertEqual(r["consumption_kWh_per_100km"], 17.31)

    def test_last_trip_leaves_the_park_out(self):
        # Before beta13 the second drive's energy ran from the end of the
        # first, so it took in the 1.7 kWh parked: 4.6 kWh, 28.75 kWh/100km.
        trip = self.m.last_trip
        self.assertEqual(trip["energy_kWh"], 2.9)
        self.assertEqual(trip["parked_before_kWh"], 1.7)
        self.assertEqual(trip["consumption_kWh_per_100km"], 18.12)
        self.assertNotIn("energy_includes_parked", trip)

    def test_survives_a_restart(self):
        reloaded = _loaded(_saved(self.m))
        self.assertEqual(reloaded.driving_since_charge, self.m.driving_since_charge)

    def test_india_drive_keeps_counter_origins_through_restart(self):
        start = _snap("13:00", 3920.0, 75.0, 10.0, 3.3)
        end = _snap("13:30", 3936.0, 70.6, 26.0, 6.2)
        for snapshot in (start, end):
            snapshot.india_provenance = {
                field: {"source": "charge", "measurement_time": datetime.fromisoformat(snapshot.ts).timestamp()}
                for field in ("since_charge_km", "since_charge_kwh")
            }
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN", india=True)
        manager.note_since_charge(10.0, 1.6, india_provenance={
            "since_charge_km": {"source": "charge", "field": "mileageSinceLastCharge", "measurement_time": 1792069200},
            "since_charge_kwh": {"source": "charge", "field": "powerUsageSinceLastCharge", "measurement_time": 1792069200},
        })
        trip = _drive(manager, start, end)
        self.assertEqual(trip["energy_kWh"], 2.9)
        self.assertEqual(trip["parked_before_kWh"], 1.7)
        self.assertEqual(
            trip["india_provenance"]["calculations"]["energy_kWh"]["inputs"],
            ["end.since_charge_kwh", "start.since_charge_kwh"],
        )
        self.assertEqual(manager.driving_since_charge["india_provenance"]["latest_trip"],
                         trip["india_provenance"])
        with patch("test_charge_reload._manager", lambda: TRIP.TripStatsManager(
            MagicMock(), "entry", "VIN", india=True
        )):
            reloaded = _loaded(_saved(manager))
        self.assertEqual(reloaded.driving_since_charge, manager.driving_since_charge)
        result = TRIP.compute_driving_since_charge(reloaded.driving_since_charge, 26.0, 6.2)
        self.assertEqual(result["india_provenance"]["tally"],
                         manager.driving_since_charge["india_provenance"])


class LastTripTests(unittest.TestCase):
    def test_a_late_start_reading_keeps_the_old_figure(self):
        # The car had already done 3 km when the trip's first reading came:
        # the start of the drive is in the gap too, so nothing is left out.
        m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        _drive(m, _snap("07:00", 3910.0, 80.0, 0.0, 0.0),
               _snap("07:20", 3920.0, 77.6, 10.0, 1.6))
        trip = _drive(m, _snap("13:05", 3923.0, 74.5, 13.0, 3.8),
                      _snap("13:30", 3936.0, 70.6, 26.0, 6.2))
        self.assertTrue(trip["energy_includes_parked"])
        self.assertEqual(trip["energy_kWh"], 4.6)
        self.assertNotIn("parked_before_kWh", trip)
        self.assertEqual(m.driving_since_charge["approximate_trips"], 1)

    def test_charged_while_parked(self):
        m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        _drive(m, _snap("07:00", 3910.0, 80.0, 0.0, 0.0),
               _snap("07:20", 3920.0, 77.6, 10.0, 1.6))
        # Charged (the next poll sees the counters reset), then 0.3 kWh used
        # parked before the next drive.
        m.note_since_charge(0.0, 0.0)
        trip = _drive(m, _snap("18:00", 3920.0, 95.0, 0.0, 0.3),
                      _snap("18:20", 3930.0, 92.6, 10.0, 1.9))
        self.assertEqual(trip["energy_kWh"], 1.6)
        self.assertEqual(trip["parked_before_kWh"], 0.3)


class ChargeTests(unittest.TestCase):
    def test_a_charge_starts_again(self):
        m = _michaels_day()
        # Charged: the counters are back at 0.
        r = TRIP.compute_driving_since_charge(m.driving_since_charge, 0.0, 0.0)
        self.assertEqual(r["trips"], 0)
        self.assertEqual(r["parked_energy_kWh"], 0.0)
        self.assertIsNone(r["efficiency_km_per_kWh"])

    def test_the_first_drive_after_a_charge_starts_a_new_tally(self):
        m = _michaels_day()
        # Charged: the poll after it sees the counters back at 0.
        m.note_since_charge(0.0, 0.0)
        # A 40 km drive after charging: further than the 26 km before, so
        # only the start of the drive shows the charge happened.
        _drive(m, _snap("18:00", 3936.0, 95.0, 0.0, 0.2),
               _snap("19:00", 3976.0, 85.0, 40.0, 6.8))
        tally = m.driving_since_charge
        self.assertEqual(tally["trips"], 1)
        self.assertEqual(tally["distance_km"], 40.0)
        self.assertEqual(tally["energy_kWh"], 6.6)
        r = TRIP.compute_driving_since_charge(tally, 40.0, 6.8)
        self.assertEqual(r["parked_energy_kWh"], 0.2)

    def test_a_charge_seen_mid_drive(self):
        m = _michaels_day()
        start = _snap("18:00", 3936.0, 95.0, 0.0, 0.1)
        r = TRIP.compute_driving_since_charge(
            m.driving_since_charge, 30.0, 4.0, open_trip_start=start
        )
        self.assertEqual(r["trips"], 0)
        self.assertEqual(r["parked_energy_kWh"], 0.1)


class DuringADriveTests(unittest.TestCase):
    def test_parked_energy_stays_put(self):
        m = _michaels_day()
        start = _snap("17:00", 3936.0, 70.0, 26.0, 6.4)
        r = TRIP.compute_driving_since_charge(
            m.driving_since_charge, 31.0, 7.3, open_trip_start=start
        )
        self.assertTrue(r["trip_in_progress"])
        self.assertEqual(r["parked_energy_kWh"], 1.9)
        # Driving Only still shows the finished drives.
        self.assertEqual(r["driving_distance_km"], 26.0)


class EdgeTests(unittest.TestCase):
    def test_no_counters_no_tally(self):
        tally = TRIP.add_trip_to_driving_tally(
            None, {"distance_km": 5.0, "energy_kWh": 1.0}, None,
            _snap("07:00", 100.0, 50.0, None, None),
        )
        self.assertIsNone(tally)
        self.assertIsNone(TRIP.compute_driving_since_charge(None, None, None))

    def test_a_drive_without_energy_is_counted_and_flagged(self):
        tally = TRIP.add_trip_to_driving_tally(
            None, {"distance_km": 1.0, "energy_kWh": None}, None,
            _snap("07:00", 100.0, 50.0, 1.0, 0.0),
        )
        self.assertEqual(tally["trips_without_energy"], 1)
        self.assertEqual(tally["distance_km"], 1.0)

    def test_approximate_drives_are_flagged(self):
        tally = TRIP.add_trip_to_driving_tally(
            None, {"distance_km": 12.0, "energy_kWh": 2.0, "retrospective": True},
            None, _snap("07:00", 100.0, 50.0, 12.0, 2.0),
        )
        self.assertEqual(tally["approximate_trips"], 1)

    def test_parked_never_negative(self):
        tally = {"distance_km": 10.0, "energy_kWh": 2.5, "trips": 1,
                 "trips_without_energy": 0, "approximate_trips": 0,
                 "since_charge_km": 10.0}
        r = TRIP.compute_driving_since_charge(tally, 10.0, 2.3)
        self.assertEqual(r["parked_energy_kWh"], 0.0)


if __name__ == "__main__":
    unittest.main()


class SensorTests(unittest.TestCase):
    def setUp(self):
        from types import SimpleNamespace as NS
        from test_erac_fallback import SENSOR

        self.SENSOR = SENSOR
        self.SENSOR.create_device_info = lambda *a: {}
        m = _michaels_day()
        self.coordinator = NS(
            data={"charging": object()},
            vin_info=NS(vin="VIN1", brandName="MG", modelName="IM6"),
            trip_stats=m,
            _extract_since_charge=lambda charging: (26.0, 6.2),
        )
        self.entry = NS(entry_id="entry1")

    def test_driving_only(self):
        s = self.SENSOR.SAICMGDrivingEfficiencySinceChargeSensor(self.coordinator, self.entry)
        self.assertEqual(s._attr_name, "MG IM6 Efficiency Since Last Charge (Driving Only)")
        self.assertEqual(s._attr_unique_id, "entry1_VIN1_efficiency_since_charge_driving")
        self.assertEqual(s.native_value, 5.78)
        self.assertEqual(s.extra_state_attributes["parked_energy_kWh"], 1.7)

    def test_parked_energy(self):
        s = self.SENSOR.SAICMGParkedEnergySinceChargeSensor(self.coordinator, self.entry)
        self.assertEqual(s._attr_name, "MG IM6 Parked Energy Since Last Charge")
        self.assertEqual(s._attr_unique_id, "entry1_VIN1_parked_energy_since_charge")
        self.assertEqual(s.native_value, 1.7)

    def test_existing_one_renamed_same_unique_id(self):
        s = self.SENSOR.SAICMGEfficiencySinceChargeSensor(self.coordinator, self.entry)
        self.assertEqual(s.name, "MG IM6 Efficiency Since Last Charge (Driving and Parked)")
        self.assertEqual(s.unique_id, "entry1_VIN1_efficiency_since_charge")

    def test_no_charging_data_unknown(self):
        self.coordinator._extract_since_charge = lambda charging: (None, None)
        s = self.SENSOR.SAICMGParkedEnergySinceChargeSensor(self.coordinator, self.entry)
        self.assertIsNone(s.native_value)
        self.assertTrue(s.available)
