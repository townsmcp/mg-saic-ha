"""Battery Capacity (Measured Over Time) and long gaps between readings (#508).

@dni990's ZS EV Long Range, Quiet Charging 22:00-07:00 with a wallbox as the
charger entity. An AC charge ran 16:55 to 06:47 at a steady ~1.9 kW, 33.1 ->
80.0 %, 31.3 kWh at the wall. Readings every half hour until 21:58, then none
until 06:49, two minutes after the charge ended (0 kW).

Before: the nine-hour gap was a straight line from 1.9 kW to 0, about 8 kWh
short, and as a gap in the middle it counted as measured, so the charge went
into the average as 18.295 kWh / 47.1 % = 38.8 kWh.

Now: the power is held at 1.9 kW until the car's end time (06:47), the gap is
counted as filled in, and a charge that is mostly filled in doesn't count.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from test_charge_stats import ts as TRIP
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

SERVICES = sys.modules["mg_saic.services"]
UTC = timezone.utc
START = datetime(2026, 11, 22, 16, 55, tzinfo=UTC)
END = datetime(2026, 11, 23, 6, 47, tzinfo=UTC)


def _night():
    """dni990's readings: 16:55 to 21:55 every half hour at 1.9 kW, one at
    21:58, then the 06:49 reading after the charge ended."""
    tracker = None
    moment = START
    while moment <= START + timedelta(hours=5):
        tracker = TRIP.track_charge_power(tracker, moment.isoformat(), 1.9, charging=True)
        moment += timedelta(minutes=30)
    tracker = TRIP.track_charge_power(
        tracker, datetime(2026, 11, 22, 21, 58, tzinfo=UTC).isoformat(), 1.9, charging=True
    )
    # Not charging any more: the paused branch passes charging=False.
    return TRIP.track_charge_power(
        tracker, datetime(2026, 11, 23, 6, 49, tzinfo=UTC).isoformat(), 0.0
    )


def _measure(tracker, end=END):
    return TRIP.measured_charge_energy(
        tracker,
        charge_start=int(START.timestamp()),
        charge_end=int(end.timestamp()) if end else None,
    )


def _charge(measured, added=47.1, at="2026-11-23T06:47:00+00:00"):
    return {"soc_added_pct": added, "charge_end_ts": at, **measured}


class DniNightTests(unittest.TestCase):
    def test_the_power_is_held_until_the_charge_ended(self):
        result = _measure(_night())
        # 16:55-21:58 at 1.9 kW is 9.6 kWh; 21:58-06:47 at 1.9 kW is 16.8.
        self.assertAlmostEqual(result["energy_measured_kWh"], 26.35, delta=0.05)
        self.assertAlmostEqual(result["energy_measured_estimated_kWh"], 16.75, delta=0.05)
        self.assertNotIn("energy_measured_unseen_s", result)

    def test_the_gap_is_reported(self):
        result = _measure(_night())
        # 21:58 to 06:49.
        self.assertEqual(result["energy_measured_max_gap_s"], 8 * 3600 + 51 * 60)

    def test_it_no_longer_counts_towards_capacity(self):
        self.assertIsNone(TRIP.capacity_sample(_charge(_measure(_night()))))

    def test_with_no_end_time_from_the_car(self):
        # The straight line as before, but counted as filled in.
        result = _measure(_night(), end=None)
        self.assertAlmostEqual(result["energy_measured_kWh"], 18.0, delta=0.1)
        self.assertAlmostEqual(result["energy_measured_estimated_kWh"], 8.4, delta=0.05)
        self.assertIsNone(TRIP.capacity_sample(_charge(result)))

    def test_an_end_time_after_the_reading_isnt_used(self):
        # The car's end is after the zero reading: that reading wasn't after
        # the end, so it counts (as a long, filled-in gap).
        late = datetime(2026, 11, 23, 7, 30, tzinfo=UTC)
        result = _measure(_night(), end=late)
        self.assertAlmostEqual(result["energy_measured_estimated_kWh"], 8.4, delta=0.05)


class GapTests(unittest.TestCase):
    def test_half_hourly_readings_are_all_measured(self):
        tracker = None
        for minutes in range(0, 300, 30):
            moment = (START + timedelta(minutes=minutes)).isoformat()
            tracker = TRIP.track_charge_power(tracker, moment, 7.0, charging=True)
        self.assertNotIn("gap_estimated_kwh", tracker)

    def test_a_long_gap_while_still_charging_is_filled_in(self):
        tracker = TRIP.track_charge_power(None, START.isoformat(), 7.0, charging=True)
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(minutes=30)).isoformat(), 7.0, charging=True
        )
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(hours=3)).isoformat(), 7.0, charging=True
        )
        self.assertAlmostEqual(tracker["gap_estimated_kwh"], 17.5)
        self.assertAlmostEqual(tracker["kwh"], 21.0)

    def test_a_zero_after_a_long_gap_followed_by_more_charging(self):
        # It wasn't the end after all: counted, as a filled-in gap.
        tracker = TRIP.track_charge_power(None, START.isoformat(), 2.0, charging=True)
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(minutes=30)).isoformat(), 2.0, charging=True
        )
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(hours=3)).isoformat(), 0.0
        )
        self.assertIn("stopped_zero_ts", tracker)
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(hours=3, minutes=30)).isoformat(), 2.0, charging=True
        )
        self.assertNotIn("stopped_zero_ts", tracker)
        self.assertAlmostEqual(tracker["gap_estimated_kwh"], 2.5)
        self.assertEqual(tracker["samples"], 4)

    def test_a_short_pause_zero_is_unchanged(self):
        # A zero half an hour after the last reading is an ordinary reading.
        tracker = TRIP.track_charge_power(None, START.isoformat(), 2.0, charging=True)
        tracker = TRIP.track_charge_power(
            tracker, (START + timedelta(minutes=30)).isoformat(), 0.0
        )
        self.assertNotIn("stopped_zero_ts", tracker)
        self.assertEqual(tracker["samples"], 2)


class UnseenEndTests(unittest.TestCase):
    def _tracker(self):
        tracker = None
        for minutes in (0, 30, 60):
            moment = (START + timedelta(minutes=minutes)).isoformat()
            tracker = TRIP.track_charge_power(tracker, moment, 7.0, charging=True)
        return tracker

    def test_a_long_end_is_reported_not_filled(self):
        result = TRIP.measured_charge_energy(
            self._tracker(),
            charge_start=int(START.timestamp()),
            charge_end=int((START + timedelta(hours=4)).timestamp()),
        )
        self.assertEqual(result["energy_measured_estimated_kWh"], 0.0)
        self.assertEqual(result["energy_measured_unseen_s"], 3 * 3600)

    def test_and_doesnt_count_towards_capacity(self):
        result = TRIP.measured_charge_energy(
            self._tracker(),
            charge_start=int(START.timestamp()),
            charge_end=int((START + timedelta(hours=4)).timestamp()),
        )
        self.assertIsNone(TRIP.capacity_sample(_charge(result, added=10.0 + 30)))

    def test_a_long_start_too(self):
        result = TRIP.measured_charge_energy(
            self._tracker(),
            charge_start=int((START - timedelta(hours=2)).timestamp()),
            charge_end=int((START + timedelta(minutes=60)).timestamp()),
        )
        self.assertEqual(result["energy_measured_unseen_s"], 2 * 3600)

    def test_a_short_end_is_filled_as_before(self):
        result = TRIP.measured_charge_energy(
            self._tracker(),
            charge_start=int(START.timestamp()),
            charge_end=int((START + timedelta(minutes=80)).timestamp()),
        )
        self.assertNotIn("energy_measured_unseen_s", result)
        self.assertAlmostEqual(result["energy_measured_estimated_kWh"], 7.0 * 20 / 60, places=3)


def _samples(*values):
    return [
        {
            "at": f"2026-11-{10 + i:02d}T06:00:00+00:00",
            "capacity_kWh": value,
            "energy_kWh": round(value * 0.5, 3),
            "soc_added_pct": 50.0,
        }
        for i, value in enumerate(values)
    ]


class RemoveSampleTests(unittest.TestCase):
    def _manager(self, *values):
        m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        m.capacity_samples = _samples(*values)
        m._settle_health_references()
        return m

    def test_removes_the_one_asked_for(self):
        m = self._manager(66.0, 67.0, 38.8, 66.5)
        result = m.remove_capacity_sample("2026-11-12T06:00:00+00:00")
        self.assertEqual(result["removed"]["capacity_kWh"], 38.8)
        self.assertEqual(result["charges_in_average"], 3)
        self.assertEqual([s["capacity_kWh"] for s in m.capacity_samples], [66.0, 67.0, 66.5])
        self.assertEqual(TRIP.measured_capacity(m.capacity_samples), 66.5)

    def test_any_way_of_writing_the_moment(self):
        for written in (
            "2026-11-12T06:00:00Z",
            "2026-11-12T06:00:00",
            "2026-11-12 06:00:00+00:00",
            "2026-11-12T07:00:00+01:00",
            " 2026-11-12T06:00:00+00:00 ",
        ):
            m = self._manager(66.0, 67.0, 38.8)
            self.assertIsNotNone(m.remove_capacity_sample(written), written)

    def test_no_such_charge(self):
        m = self._manager(66.0, 67.0)
        self.assertIsNone(m.remove_capacity_sample("2026-11-01T06:00:00+00:00"))
        self.assertIsNone(m.remove_capacity_sample("not a time"))
        self.assertIsNone(m.remove_capacity_sample(""))
        self.assertEqual(len(m.capacity_samples), 2)

    def test_a_charge_in_the_health_reference_resets_it(self):
        m = self._manager(66.0, 67.0, 38.8, 66.5, 67.5)
        self.assertIsNotNone(m.health_baseline)
        result = m.remove_capacity_sample("2026-11-12T06:00:00+00:00")
        self.assertEqual(result["health_reference_reset"], ["measured"])
        self.assertIsNone(m.health_baseline)
        # The next good charge makes five again and sets it from those.
        m.note_capacity_sample(
            {
                "charge_end_ts": "2026-11-20T06:00:00+00:00",
                "energy_measured_kWh": 33.0,
                "energy_measured_estimated_kWh": 0.0,
                "energy_measured_samples": 20,
                "soc_added_pct": 50.0,
            }
        )
        self.assertEqual(m.health_baseline["capacity_kWh"], 66.6)

    def test_a_later_charge_leaves_the_reference_alone(self):
        # Five good charges set the reference; a bad sixth comes later.
        m = self._manager(66.0, 67.0, 66.5, 67.5, 66.0)
        baseline = dict(m.health_baseline)
        m.capacity_samples = (m.capacity_samples + _samples(*[0] * 10, 38.8)[-1:])[-5:]
        result = m.remove_capacity_sample("2026-11-20T06:00:00+00:00")
        self.assertEqual(result["health_reference_reset"], [])
        self.assertEqual(m.health_baseline, baseline)

    def test_an_official_reference_is_anchored_again(self):
        m = self._manager(66.0, 67.0, 38.8, 66.5, 67.5)
        m.set_official_health(94, "2026-11-15T00:00:00+00:00")
        self.assertIsNotNone(m.health_official["capacity_kWh"])
        result = m.remove_capacity_sample("2026-11-12T06:00:00+00:00")
        self.assertIn("official", result["health_reference_reset"])
        self.assertEqual(m.health_official["pct"], 94.0)
        self.assertIsNone(m.health_official["capacity_kWh"])


class _Error(Exception):
    pass


class ActionTests(unittest.TestCase):
    VIN = "LSJA24U90NG000001"

    def _setup(self, coordinators):
        registered = {}

        def register(domain, name, handler, **kwargs):
            registered[name] = (handler, kwargs)

        hass = NS(
            services=NS(async_register=register),
            data={"mg_saic": {"clients_by_vin": {}, "coordinators_by_vin": coordinators}},
        )
        asyncio.run(SERVICES.async_setup_services(hass))
        return registered["remove_capacity_charge"]

    def _coordinator(self):
        m = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        m.capacity_samples = _samples(66.0, 38.8, 67.0)
        return MagicMock(trip_stats=m)

    def test_removes_saves_and_updates(self):
        coordinator = self._coordinator()
        handler, kwargs = self._setup({self.VIN: coordinator})
        result = asyncio.run(handler(NS(data={"charged_at": "2026-11-11T06:00:00+00:00"})))
        self.assertEqual(result["removed"]["capacity_kWh"], 38.8)
        coordinator._schedule_trip_save.assert_called_once()
        coordinator.async_update_listeners.assert_called_once()
        self.assertEqual(kwargs["supports_response"], "optional")
        self.assertIs(kwargs["schema"], SERVICES.SERVICE_REMOVE_CAPACITY_CHARGE_SCHEMA)

    def test_nothing_to_remove_says_why(self):
        coordinator = self._coordinator()
        handler, _ = self._setup({self.VIN: coordinator})
        with patch.object(SERVICES, "HomeAssistantError", _Error):
            with self.assertRaises(_Error) as raised:
                asyncio.run(handler(NS(data={"charged_at": "2026-01-01T00:00:00+00:00"})))
        self.assertIn("recent_charges", str(raised.exception))
        coordinator._schedule_trip_save.assert_not_called()

    def test_two_cars_need_the_vin(self):
        handler, _ = self._setup({self.VIN: self._coordinator(), "OTHER": self._coordinator()})
        with patch.object(SERVICES, "HomeAssistantError", _Error):
            with self.assertRaises(_Error):
                asyncio.run(handler(NS(data={"charged_at": "2026-11-11T06:00:00+00:00"})))
            result = asyncio.run(
                handler(NS(data={"vin": self.VIN, "charged_at": "2026-11-11T06:00:00+00:00"}))
            )
        self.assertEqual(result["charges_in_average"], 2)


class DefinitionTests(unittest.TestCase):
    def test_described_translated_and_removed(self):
        import json
        from pathlib import Path

        pkg = Path(TRIP.__file__).resolve().parent
        self.assertIn("remove_capacity_charge:", (pkg / "services.yaml").read_text())
        for path in sorted((pkg / "translations").glob("*.json")):
            entry = json.loads(path.read_text())["services"]["remove_capacity_charge"]
            self.assertTrue(entry["name"], path.name)
            self.assertIn("charged_at", entry["fields"], path.name)
        self.assertIn(
            "hass.services.async_remove(DOMAIN, SERVICE_REMOVE_CAPACITY_CHARGE)",
            (pkg / "services.py").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
