"""A measured energy figure for a charge (#407).

@hoffeck, 5 Oct 2026: Last Charge Energy is the SOC change times the pack
size (AC 4.435 kWh = 8.4 % x 52.8; DC 26.506 kWh = 50.2 % x 52.8). Pack
voltage times current, added up over the charge, would be a measurement.

His DC charge was "Connecting" at 15:39:34 and first seen charging at
15:41:43 -- one two-minute poll later, with the battery already up 3.8 %. The
measured figure fills that stretch in from the car's own start time.

1.3.0-beta12 also tried to see the start sooner, by polling again a minute
later (three times) whenever the car was first seen "Connecting". That was
taken out in beta13: see NoQuickPollsTests at the bottom.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_charge_stats import CSnap, ts as TRIP
from test_erac_fallback import LOGIC, SENSOR

UTC = timezone.utc
T0 = datetime(2026, 10, 5, 14, 40, tzinfo=UTC)
START = int(T0.timestamp())


def _iso(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


class TrackPowerTests(unittest.TestCase):
    def test_first_reading_adds_nothing(self):
        tracker = TRIP.track_charge_power(None, _iso(0), 7.0)
        self.assertEqual(tracker["kwh"], 0.0)
        self.assertEqual(tracker["samples"], 1)

    def test_steady_power(self):
        tracker = None
        for minute in range(0, 61, 10):
            tracker = TRIP.track_charge_power(tracker, _iso(minute * 60), 7.0)
        self.assertAlmostEqual(tracker["kwh"], 7.0)
        self.assertEqual(tracker["samples"], 7)
        self.assertEqual(tracker["max_gap_s"], 600)

    def test_a_taper_is_averaged_between_readings(self):
        tracker = TRIP.track_charge_power(None, _iso(0), 76.0)
        tracker = TRIP.track_charge_power(tracker, _iso(60), 70.0)
        self.assertAlmostEqual(tracker["kwh"], 73.0 / 60)

    def test_readings_that_say_nothing_are_ignored(self):
        tracker = TRIP.track_charge_power(None, _iso(0), 7.0)
        for power in (None, -1.0, "7", True, float("nan")):
            with self.subTest(power=power):
                self.assertIs(TRIP.track_charge_power(tracker, _iso(600), power), tracker)
        self.assertIs(TRIP.track_charge_power(tracker, "not a time", 7.0), tracker)
        # Same instant, or earlier: nothing to add.
        self.assertIs(TRIP.track_charge_power(tracker, _iso(0), 7.0), tracker)
        self.assertIsNone(TRIP.track_charge_power(None, _iso(0), None))


class MeasuredEnergyTests(unittest.TestCase):
    def _tracker(self):
        tracker = None
        for second in (120, 180, 240):
            tracker = TRIP.track_charge_power(tracker, _iso(second), 60.0)
        return tracker  # 2 kWh between 120 s and 240 s

    def test_needs_two_readings(self):
        self.assertIsNone(TRIP.measured_charge_energy(None))
        one = TRIP.track_charge_power(None, _iso(0), 7.0)
        self.assertIsNone(TRIP.measured_charge_energy(one))

    def test_between_the_readings_only(self):
        result = TRIP.measured_charge_energy(self._tracker())
        self.assertEqual(result["energy_measured_kWh"], 2.0)
        self.assertEqual(result["energy_measured_estimated_kWh"], 0.0)
        self.assertEqual(result["energy_measured_samples"], 3)
        self.assertEqual(result["energy_measured_max_gap_s"], 60)

    def test_the_ends_are_filled_in_and_reported(self):
        # The charge really ran from 0 s to 300 s.
        result = TRIP.measured_charge_energy(
            self._tracker(), charge_start=START, charge_end=START + 300
        )
        # 120 s before the first reading + 60 s after the last, at 60 kW.
        self.assertEqual(result["energy_measured_estimated_kWh"], 3.0)
        self.assertEqual(result["energy_measured_kWh"], 5.0)

    def test_an_end_too_far_from_a_reading_is_not_guessed(self):
        result = TRIP.measured_charge_energy(
            self._tracker(), charge_start=START - 7200, charge_end=START + 240 + 7200
        )
        self.assertEqual(result["energy_measured_estimated_kWh"], 0.0)
        self.assertEqual(TRIP.MEASURED_ENERGY_MAX_EDGE_S, 3600)

    def test_times_on_the_wrong_side_add_nothing(self):
        result = TRIP.measured_charge_energy(
            self._tracker(), charge_start=START + 200, charge_end=START + 200
        )
        self.assertEqual(result["energy_measured_estimated_kWh"], 0.0)


def _snap(second, soc, start, end):
    return CSnap(ts=_iso(second), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=100.0, range_km=None, record_start=start, record_end=end)


class WholeChargeTests(unittest.TestCase):
    """A DC charge polled every minute, first seen two minutes in."""

    PREVIOUS = (START - 90000, START - 86000)

    def _charge(self, **overrides):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")

        def note(second, charging, soc, start, end, power, paused=False):
            return manager.note_charge_state(
                charging, _snap(second, soc, start, end), capacity_kwh=52.8,
                now_iso=_iso(second), is_plugged_in=True, charge_paused=paused,
                power_kw=power,
            )

        note(-60, False, 24.9, *self.PREVIOUS, 0.0)          # Connecting
        for second in range(120, 1801, 60):                  # charging at 60 kW
            note(second, True, 28.7 + second / 100, START, self.PREVIOUS[1], 60.0)
        charge, _ = note(1900, False, 75.1, START, START + 1830, 0.0)
        return charge, manager

    def test_the_measured_figure(self):
        charge, _manager = self._charge()
        # 1680 s between first and last reading, 120 s before, 30 s after.
        self.assertEqual(charge["energy_measured_kWh"], 30.5)
        self.assertEqual(charge["energy_measured_estimated_kWh"], 2.5)
        self.assertEqual(charge["energy_measured_samples"], 29)
        self.assertEqual(charge["energy_measured_max_gap_s"], 60)

    def test_the_headline_figure_is_unchanged(self):
        charge, _manager = self._charge()
        self.assertEqual(charge["method"], "soc")
        self.assertEqual(charge["energy_added_kWh"], round(50.2 / 100 * 52.8, 3))

    def test_nothing_is_left_over(self):
        _charge, manager = self._charge()
        self.assertIsNone(manager.open_charge_power)

    def test_no_power_readings_no_measured_figure(self):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        for second, charging, soc, start, end in (
            (-60, False, 50.0, *self.PREVIOUS),
            (60, True, 51.0, START, self.PREVIOUS[1]),
            (660, True, 55.0, START, self.PREVIOUS[1]),
            (1300, False, 60.0, START, START + 1200),
        ):
            charge, _ = manager.note_charge_state(
                charging, _snap(second, soc, start, end), capacity_kwh=52.8,
                now_iso=_iso(second), is_plugged_in=True,
            )
        self.assertNotIn("energy_measured_kWh", charge)

    def test_the_attributes_reach_the_sensor(self):
        for key in ("energy_measured_kWh", "energy_measured_estimated_kWh",
                    "energy_measured_samples", "energy_measured_max_gap_s"):
            self.assertIn(key, SENSOR.SAICMGLastChargeEnergySensor._CHARGE_ATTR_KEYS)

    def test_survives_a_restart(self):
        manager = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        manager.note_charge_state(
            True, _snap(60, 30.0, START, self.PREVIOUS[1]), capacity_kwh=52.8,
            now_iso=_iso(60), is_plugged_in=True, power_kw=60.0,
        )
        manager.note_charge_state(
            True, _snap(120, 31.0, START, self.PREVIOUS[1]), capacity_kwh=52.8,
            now_iso=_iso(120), is_plugged_in=True, power_kw=60.0,
        )
        saved = {}

        class _Store:
            async def async_save(self, data):
                saved.update(data)

        manager._store = _Store()
        asyncio.run(manager.async_save())
        self.assertEqual(saved["open_charge_power"]["samples"], 2)
        self.assertAlmostEqual(saved["open_charge_power"]["kwh"], 1.0)


class PackPowerTests(unittest.TestCase):
    def setUp(self):
        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        self.power = sys.modules[
            "mg_saic.coordinator"
        ].SAICMGDataUpdateCoordinator._pack_power_kw

    def test_the_same_sum_as_the_charging_power_sensor(self):
        # An MGS6 on a 10 A supply: 5.0 A into a 431 V pack.
        self.assertEqual(self.power(NS(bmsPackCrnt=19900, bmsPackVol=1724)), 2.155)

    def test_never_negative(self):
        self.assertEqual(self.power(NS(bmsPackCrnt=20010, bmsPackVol=1724)), 0.0)

    def test_missing_or_no_value(self):
        self.assertIsNone(self.power(NS(bmsPackCrnt=None, bmsPackVol=1724)))
        self.assertIsNone(self.power(NS(bmsPackCrnt=19900, bmsPackVol=-128)))
        self.assertIsNone(self.power(NS()))

    def test_the_coordinator_hands_it_to_the_tracker(self):
        cls = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
        c = cls.__new__(cls)
        c.vin = "VIN"
        c.trip_stats = MagicMock()
        c.trip_stats.note_charge_state.return_value = (None, False)
        c._charge_snapshot = MagicMock(return_value="snapshot")
        c.resolve_battery_capacity_for = MagicMock(return_value=(52.8, "override"))
        c._schedule_trip_save = MagicMock()
        charging = NS(
            chrgMgmtData=NS(bmsChrgSts=1, bmsPackCrnt=19900, bmsPackVol=1724),
            rvsChargeStatus=NS(chargingGunState=1, chargingDuration=60),
        )
        cls._update_charge_state(c, NS(), charging)
        self.assertEqual(
            c.trip_stats.note_charge_state.call_args.kwargs["power_kw"], 2.155
        )


class NoQuickPollsTests(unittest.TestCase):
    """"Connecting" does not mean a charge is about to start.

    @SteveMSJ, 6 Oct 2026 (#407): on a smart tariff with a Zappi his cars sit
    in Connecting for hours -- 16:21 to 23:01 before the first burst, and
    again between bursts through the night. beta12's extra polls, a minute
    apart each time that state was first seen, "aren't going to catch
    anything in this situation and would appear to be a waste". An MG HS
    PHEV on a Zappi reports the same state while it waits for its schedule.
    """

    def setUp(self):
        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        self.const = sys.modules["mg_saic.const"]
        self.coordinator_module = sys.modules["mg_saic.coordinator"]

    def _interval(self, **state):
        cls = self.coordinator_module.SAICMGDataUpdateCoordinator
        c = cls.__new__(cls)
        now = datetime.now(UTC)
        c._action_interval_active = False
        c.last_powered_off_time = now - timedelta(hours=6)
        c.last_vehicle_activity = now - timedelta(hours=6)
        c.is_powered_on = False
        c.is_charging = False
        c.is_dc_charging = False
        c.default_update_interval = timedelta(minutes=60)
        c.powered_update_interval = timedelta(minutes=15)
        c.charging_update_interval = timedelta(minutes=5)
        c.dc_charging_update_interval = timedelta(minutes=5)
        c.grace_period_update_interval = timedelta(minutes=10)
        c.after_shutdown_update_interval = timedelta(minutes=2)
        c.holiday_mode = False
        c.holiday_update_interval = timedelta(hours=12)
        c.update_interval = None
        c._schedule_refresh = MagicMock()
        for name, value in state.items():
            setattr(c, name, value)
        cls._adjust_update_interval(c)
        return c.update_interval

    def test_a_car_waiting_for_its_charger_is_polled_at_the_normal_interval(self):
        # Plugged in, "Connecting", nothing flowing: the idle interval, as
        # for any parked car.
        self.assertEqual(self._interval(), timedelta(minutes=60))

    def test_whatever_beta12_left_behind_is_ignored(self):
        # The flags beta12 set on the coordinator no longer mean anything.
        self.assertEqual(
            self._interval(_charge_connecting=True, _connecting_polls=0),
            timedelta(minutes=60),
        )

    def test_charging_still_gets_the_charging_interval(self):
        self.assertEqual(self._interval(is_charging=True), timedelta(minutes=5))

    def test_the_machinery_is_gone(self):
        for name in (
            "CHARGE_CONNECTING_STATUS_CODE",
            "UPDATE_INTERVAL_CONNECTING",
            "MAX_CONNECTING_POLLS",
        ):
            self.assertFalse(hasattr(self.const, name), name)
        self.assertFalse(hasattr(LOGIC, "connecting_repoll"))
        self.assertFalse(hasattr(self.coordinator_module, "connecting_repoll"))

    def test_connecting_is_still_a_paused_charge_not_a_finished_one(self):
        # What the state does still mean: a charge that drops to it is kept
        # open for a while rather than ended.
        self.assertIn(5, self.const.CHARGE_PAUSED_STATUS_CODES)


if __name__ == "__main__":
    unittest.main()
