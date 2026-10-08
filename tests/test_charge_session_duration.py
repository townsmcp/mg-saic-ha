"""Charge Session Duration: the whole charge, while it is still going (#262).

@HarryFlatter's HS PHEV, night of 3-4 Oct 2026 (times UTC; BST is an hour
later). The car charged in four stretches with three pauses of under a minute,
and restarted its record -- and its Charging Duration counter -- each time:

    23:31:57 -> 23:55:30   1413 s      39 s pause
    23:56:09 -> 00:43:46   2857 s      53 s pause
    00:44:39 -> 01:10:38   1559 s      48 s pause
    01:11:26 -> 01:17:22    356 s
                           ------
                           6185 s  (1 h 43 min 5 s), 140 s paused

Home Assistant polled every 30 minutes (and was restarted at 00:13 to install
an update), so the counter was read at 11, 403, 1059, 1067, 1085, 21, 261 and
0 seconds. Harry, looking at that graph: "I cannot reconcile the spikes to
Duration s of 6185 seconds." They can't be: each reading is how far the
stretch then running had got, and the stretches went on after it.

Last Charge Duration has the whole figure, but only once the charge is over.
This is the same figure worked out at each reading while the charge is open.

The figures below are the car's, from his log.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_charge_stats import CSnap, ts as TRIP
from test_erac_fallback import SENSOR

UTC = timezone.utc
CAPACITY = 23.2

PREVIOUS_START = 1790902521
PREVIOUS_END = 1790904093
S1, S1_END = 1791070317, 1791071730
S2, S2_END = 1791071769, 1791074626
S3, S3_END = 1791074679, 1791076238
S4, S4_END = 1791076286, 1791076642


def _at(day, h, m, s=0):
    return datetime(2026, 10, day, h, m, s, tzinfo=UTC)


def _snap(at, soc, start, end):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=6200.0, range_km=None,
                 record_start=start, record_end=end)


# (reading UTC, charging?, SOC, startTime, endTime, the car's counter,
#  seconds spent charging so far)
LAST_NIGHT = [
    (_at(3, 22, 48, 30), False, 64.9, PREVIOUS_START, PREVIOUS_END, 0, None),
    (_at(3, 23, 32, 13), True, 65.0, S1, PREVIOUS_END, 11, 11),
    (_at(4, 0, 2, 57), True, 79.4, S2, S1_END, 403, 1413 + 403),
    (_at(4, 0, 13, 52), True, 84.5, S2, S1_END, 1059, 1413 + 1059),
    (_at(4, 0, 14, 0), True, 84.5, S2, S1_END, 1067, 1413 + 1067),
    (_at(4, 0, 14, 18), True, 84.6, S2, S1_END, 1085, 1413 + 1085),
    (_at(4, 0, 45, 4), True, 93.9, S3, S2_END, 21, 1413 + 2857 + 21),
    (_at(4, 1, 15, 51), True, 99.6, S4, S3_END, 261, 1413 + 2857 + 1559 + 261),
    (_at(4, 1, 46, 54), False, 100.0, S4, S4_END, 0, 6185),
]


def _note(manager, at, charging, soc, start, end, counter=None, *,
          plugged=True, paused=False):
    return manager.note_charge_state(
        charging, _snap(at, soc, start, end),
        capacity_kwh=CAPACITY, now_iso=at.isoformat(),
        is_plugged_in=plugged, charge_paused=paused, stretch_s=counter,
    )


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


def _sensor(manager):
    coordinator = NS(
        vin_info=NS(vin="VIN1", brandName="MG", modelName="HS"),
        trip_stats=manager,
    )
    return SENSOR.SAICMGChargeSessionDurationSensor(coordinator, NS(entry_id="e"))


class LastNightTests(unittest.TestCase):
    def test_it_counts_up_across_the_pauses(self):
        manager = _manager()
        entity = _sensor(manager)
        seen = []
        for at, charging, soc, start, end, counter, expected in LAST_NIGHT:
            _note(manager, at, charging, soc, start, end, counter)
            session = manager.charge_session()
            with self.subTest(at=at.isoformat()):
                if expected is None:
                    self.assertIsNone(session)
                    self.assertIsNone(entity.native_value)
                    continue
                self.assertEqual(session["duration_s"], expected)
                self.assertEqual(session["in_progress"], charging)
                self.assertEqual(entity.native_value, round(expected / 60, 1))
                seen.append(session["duration_s"])
        self.assertEqual(seen, sorted(seen), "never goes backwards within a charge")
        self.assertEqual(seen, [11, 1816, 2472, 2480, 2498, 4291, 6090, 6185])

    def test_the_pauses_are_counted_as_they_happen(self):
        manager = _manager()
        counted = []
        for at, charging, soc, start, end, counter, _ in LAST_NIGHT[:-1]:
            _note(manager, at, charging, soc, start, end, counter)
            session = manager.charge_session()
            if session:
                counted.append((session["interruptions"], session["paused_s"]))
        self.assertEqual(
            counted, [(0, 0), (1, 39), (1, 39), (1, 39), (1, 39), (2, 92), (3, 140)]
        )

    def test_it_ends_on_the_figure_last_charge_reports(self):
        manager = _manager()
        charge = None
        for at, charging, soc, start, end, counter, _ in LAST_NIGHT:
            charge, _changed = _note(manager, at, charging, soc, start, end, counter)
        self.assertEqual(charge["duration_s"], 6185)
        self.assertEqual(charge["interruptions"], 3)
        self.assertEqual(charge["paused_s"], 140)
        self.assertEqual(charge["charge_start_ts"], _at(3, 23, 31, 57).isoformat())
        self.assertEqual(charge["charge_end_ts"], _at(4, 1, 17, 22).isoformat())
        session = manager.charge_session()
        self.assertFalse(session["in_progress"])
        for key in ("duration_s", "interruptions", "paused_s", "duration_source",
                    "charge_start_ts", "charge_end_ts"):
            self.assertEqual(session[key], charge[key], key)
        self.assertIsNone(manager.charge_progress)

    def test_the_start_is_the_first_stretch_throughout(self):
        manager = _manager()
        for at, charging, soc, start, end, counter, _ in LAST_NIGHT[:-1]:
            _note(manager, at, charging, soc, start, end, counter)
            session = manager.charge_session()
            if session:
                self.assertEqual(
                    session["charge_start_ts"], _at(3, 23, 31, 57).isoformat()
                )
                self.assertEqual(session["as_of"], at.isoformat())
                self.assertNotIn("charge_end_ts", session)

    def test_a_mid_charge_reading_is_not_a_state_change(self):
        # It moves on every reading; that alone must not write to storage.
        manager = _manager()
        for row in LAST_NIGHT[:3]:
            _note(manager, *row[:6])
        _charge, changed = _note(manager, *LAST_NIGHT[3][:6])
        self.assertFalse(changed)
        self.assertEqual(manager.charge_progress["duration_s"], 1413 + 1059)


class StretchCounterTests(unittest.TestCase):
    """The running stretch is the car's counter when that is believable."""

    def _to_second_stretch(self, counter):
        manager = _manager()
        for row in LAST_NIGHT[:2]:
            _note(manager, *row[:6])
        at = _at(4, 0, 2, 57)  # 408 s after the second stretch started
        _note(manager, at, True, 79.4, S2, S1_END, counter)
        return manager.charge_progress["duration_s"]

    def test_the_cars_counter(self):
        self.assertEqual(self._to_second_stretch(403), 1413 + 403)

    def test_no_counter_uses_the_time_since_the_stretch_started(self):
        self.assertEqual(self._to_second_stretch(None), 1413 + 408)

    def test_a_counter_that_cannot_be_right_is_ignored(self):
        # Left over from the stretch before, or simply wrong.
        for counter in (99999, -5, "403", True):
            with self.subTest(counter=counter):
                self.assertEqual(self._to_second_stretch(counter), 1413 + 408)

    def test_a_reading_dated_before_the_stretch_started_is_zero_not_negative(self):
        manager = _manager()
        _note(manager, *LAST_NIGHT[0][:6])
        _note(manager, _at(3, 23, 31, 50), True, 65.0, S1, PREVIOUS_END, None)
        self.assertEqual(manager.charge_progress["duration_s"], 0)
        self.assertEqual(_sensor(manager).native_value, 0.0)


class PausedReadingTests(unittest.TestCase):
    def _paused_after_the_first_stretch(self):
        manager = _manager()
        for row in LAST_NIGHT[:2]:
            _note(manager, *row[:6])
        # 23:55:50, inside the first pause (23:55:30 -> 23:56:09).
        _note(manager, _at(3, 23, 55, 50), False, 76.0, S1, S1_END, 0, paused=True)
        return manager

    def test_a_reading_in_a_pause_shows_the_stretches_so_far(self):
        manager = self._paused_after_the_first_stretch()
        session = manager.charge_session()
        self.assertTrue(session["in_progress"])
        self.assertEqual(session["duration_s"], 1413)
        self.assertEqual(session["interruptions"], 0)

    def test_it_carries_on_from_there_when_charging_resumes(self):
        manager = self._paused_after_the_first_stretch()
        _note(manager, *LAST_NIGHT[2][:6])
        session = manager.charge_session()
        self.assertEqual(session["duration_s"], 1413 + 403)
        self.assertEqual((session["interruptions"], session["paused_s"]), (1, 39))

    def test_a_pause_whose_end_the_car_has_not_written_keeps_the_last_figure(self):
        manager = _manager()
        for row in LAST_NIGHT[:2]:
            _note(manager, *row[:6])
        # Stopped, but endTime is still the previous charge's.
        _note(manager, _at(3, 23, 55, 50), False, 76.0, S1, PREVIOUS_END, 0, paused=True)
        self.assertEqual(manager.charge_session()["duration_s"], 11)

    def test_a_restart_and_stop_between_two_readings_is_counted(self):
        manager = _manager()
        for row in LAST_NIGHT[:2]:
            _note(manager, *row[:6])
        # Next reading: the second stretch has been and gone.
        _note(manager, _at(4, 0, 44, 0), False, 93.0, S2, S2_END, 0, paused=True)
        session = manager.charge_session()
        self.assertEqual(session["interruptions"], 1)
        # The pause between them was never seen, so it is not taken out.
        self.assertEqual(session["duration_s"], S2_END - S1)


class OtherShapesTests(unittest.TestCase):
    def test_a_car_with_no_record_goes_by_the_readings(self):
        manager = _manager()
        _note(manager, _at(3, 23, 0), False, 60.0, None, None)
        _note(manager, _at(3, 23, 30), True, 62.0, None, None, 20)
        _note(manager, _at(4, 0, 0), True, 70.0, None, None, 1820)
        session = manager.charge_session()
        self.assertEqual(session["duration_source"], "polls")
        # From the last reading before charging, as the final figure is.
        self.assertEqual(session["duration_s"], 3600)
        self.assertNotIn("charge_start_ts", session)
        charge, _ = _note(manager, _at(4, 0, 30), False, 80.0, None, None, 0)
        self.assertEqual(charge["duration_source"], "polls")
        self.assertEqual(charge["duration_s"], 5400)

    def test_a_stretch_missed_before_the_first_reading_is_flagged(self):
        manager = _manager()
        _note(manager, *LAST_NIGHT[0][:6])
        # First charging reading is already in the second stretch.
        _note(manager, *LAST_NIGHT[2][:6])
        session = manager.charge_session()
        self.assertEqual(session["duration_source"], "car_partial")
        self.assertEqual(session["duration_s"], 403)

    def test_nothing_before_any_charge(self):
        manager = _manager()
        self.assertIsNone(manager.charge_session())
        _note(manager, *LAST_NIGHT[0][:6])
        self.assertIsNone(manager.charge_session())

    def test_between_charges_it_holds_the_last_total(self):
        manager = _manager()
        for row in LAST_NIGHT:
            _note(manager, *row[:6])
        # Still plugged in next morning, then unplugged and driven.
        _note(manager, _at(4, 7, 0), False, 100.0, S4, S4_END, 0)
        _note(manager, _at(4, 9, 0), False, 80.0, S4, S4_END, 0, plugged=False)
        session = manager.charge_session()
        self.assertFalse(session["in_progress"])
        self.assertEqual(session["duration_s"], 6185)

    def test_the_next_charge_starts_again_from_nothing(self):
        manager = _manager()
        for row in LAST_NIGHT:
            _note(manager, *row[:6])
        new_start = S4_END + 20 * 3600
        at = datetime.fromtimestamp(new_start + 30, UTC)
        _note(manager, at, True, 50.0, new_start, S4_END, 25)
        session = manager.charge_session()
        self.assertTrue(session["in_progress"])
        self.assertEqual(session["duration_s"], 25)
        self.assertEqual(session["interruptions"], 0)
        # Last Charge still describes the charge that finished.
        self.assertEqual(manager.last_charge["duration_s"], 6185)

    def test_an_abandoned_charge_leaves_no_running_total(self):
        manager = _manager()
        for row in LAST_NIGHT[:3]:
            _note(manager, *row[:6])
        # Nothing heard for three days: the stop was never seen.
        _note(manager, _at(7, 0, 0), False, 40.0, S2, S2_END, 0, plugged=False)
        self.assertIsNone(manager.open_charge)
        self.assertIsNone(manager.charge_progress)


class SensorTests(unittest.TestCase):
    def test_identity(self):
        entity = _sensor(_manager())
        self.assertEqual(entity.name, "MG HS Charge Session Duration")
        self.assertEqual(entity.unique_id, "e_VIN1_charge_session_duration")
        self.assertEqual(entity._attr_device_class, SENSOR.SensorDeviceClass.DURATION)
        self.assertEqual(
            entity._attr_native_unit_of_measurement, SENSOR.UnitOfTime.MINUTES
        )

    def test_unknown_but_available_before_any_charge(self):
        entity = _sensor(_manager())
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)
        entity.coordinator.trip_stats = None
        self.assertTrue(entity.available)
        self.assertIsNone(entity.native_value)
        self.assertIsNone(entity.extra_state_attributes)

    def test_attributes_while_charging(self):
        manager = _manager()
        for row in LAST_NIGHT[:7]:
            _note(manager, *row[:6])
        self.assertEqual(
            _sensor(manager).extra_state_attributes,
            {
                "in_progress": True,
                "duration_s": 4291,
                "paused_s": 92,
                "interruptions": 2,
                "duration_source": "car",
                "charge_start_ts": _at(3, 23, 31, 57).isoformat(),
                "as_of": _at(4, 0, 45, 4).isoformat(),
            },
        )

    def test_attributes_after_the_charge(self):
        manager = _manager()
        for row in LAST_NIGHT:
            _note(manager, *row[:6])
        entity = _sensor(manager)
        self.assertEqual(entity.native_value, 103.1)
        self.assertEqual(
            entity.extra_state_attributes,
            {
                "in_progress": False,
                "duration_s": 6185,
                "paused_s": 140,
                "interruptions": 3,
                "duration_source": "car",
                "charge_start_ts": _at(3, 23, 31, 57).isoformat(),
                "charge_end_ts": _at(4, 1, 17, 22).isoformat(),
            },
        )

    def test_paused_and_interruptions_are_there_when_there_were_none(self):
        manager = _manager()
        manager.last_charge = {"duration_s": 1680, "duration_source": "car"}
        attrs = _sensor(manager).extra_state_attributes
        self.assertEqual(attrs["paused_s"], 0)
        self.assertEqual(attrs["interruptions"], 0)

    def test_a_finished_charge_with_no_duration_is_unknown(self):
        manager = _manager()
        for charge in ({"energy_added_kWh": 5.0}, {"duration_s": 0}):
            manager.last_charge = charge
            with self.subTest(charge=charge):
                self.assertIsNone(_sensor(manager).native_value)

    def test_agrees_with_last_charge_duration_once_finished(self):
        manager = _manager()
        for row in LAST_NIGHT:
            _note(manager, *row[:6])
        session = _sensor(manager)
        last = SENSOR.SAICMGLastChargeDurationSensor(
            session.coordinator, NS(entry_id="e")
        )
        self.assertEqual(session.native_value, last.native_value)

    def test_last_charge_duration_does_not_move_during_the_next_charge(self):
        manager = _manager()
        manager.last_charge = {"duration_s": 1680}
        for row in LAST_NIGHT[:4]:
            _note(manager, *row[:6])
        session = _sensor(manager)
        last = SENSOR.SAICMGLastChargeDurationSensor(
            session.coordinator, NS(entry_id="e")
        )
        self.assertEqual(last.native_value, 28.0)
        self.assertEqual(session.native_value, round((1413 + 1059) / 60, 1))


class RestartTests(unittest.TestCase):
    """Harry restarted Home Assistant 42 minutes into this charge."""

    def _saved(self, manager):
        saved = {}

        class _Store:
            async def async_save(self, data):
                saved.update(data)

        manager._store = _Store()
        asyncio.run(manager.async_save())
        return saved

    def _loaded(self, saved):
        manager = _manager()

        class _Store:
            async def async_load(self):
                return dict(saved)

        store_module = sys.modules.get("homeassistant.helpers.storage")
        original = getattr(store_module, "Store", None) if store_module else None
        module = store_module or NS()
        module.Store = lambda *_a, **_k: _Store()
        helpers = sys.modules.setdefault("homeassistant.helpers", NS())
        sys.modules.setdefault("homeassistant", NS(helpers=helpers))
        sys.modules["homeassistant.helpers.storage"] = module
        try:
            asyncio.run(manager.async_load())
        finally:
            if store_module is None:
                sys.modules.pop("homeassistant.helpers.storage", None)
            elif original is not None:
                store_module.Store = original
        return manager

    def test_the_running_total_survives_and_carries_on(self):
        manager = _manager()
        for row in LAST_NIGHT[:3]:
            _note(manager, *row[:6])
        saved = self._saved(manager)
        self.assertEqual(saved["charge_progress"]["duration_s"], 1413 + 403)

        reloaded = self._loaded(saved)
        # Before the first reading after the restart: the stored figure.
        session = reloaded.charge_session()
        self.assertTrue(session["in_progress"])
        self.assertEqual(session["duration_s"], 1413 + 403)
        self.assertEqual(session["as_of"], _at(4, 0, 2, 57).isoformat())

        charge = None
        for row in LAST_NIGHT[3:]:
            charge, _ = _note(reloaded, *row[:6])
        self.assertEqual(charge["duration_s"], 6185)

    def test_nothing_is_stored_or_restored_without_an_open_charge(self):
        manager = _manager()
        for row in LAST_NIGHT:
            _note(manager, *row[:6])
        manager.charge_progress = {"duration_s": 99}  # must not leak out
        saved = self._saved(manager)
        self.assertIsNone(saved["charge_progress"])

        saved["charge_progress"] = {"duration_s": 99}
        reloaded = self._loaded(saved)
        self.assertIsNone(reloaded.charge_progress)
        self.assertEqual(reloaded.charge_session()["duration_s"], 6185)

    def test_updated_mid_charge_from_a_version_without_it(self):
        manager = _manager()
        for row in LAST_NIGHT[:3]:
            _note(manager, *row[:6])
        saved = self._saved(manager)
        del saved["charge_progress"]
        reloaded = self._loaded(saved)
        # Unknown, not the previous charge's total, until the car is read.
        self.assertIsNone(reloaded.charge_session())
        self.assertIsNone(_sensor(reloaded).native_value)
        _note(reloaded, *LAST_NIGHT[3][:6])
        self.assertEqual(reloaded.charge_session()["duration_s"], 1413 + 1059)


class CoordinatorPassesTheCounterTests(unittest.TestCase):
    def _call(self, rcs):
        import test_setup_and_config_flow  # noqa: F401 - loads the stubbed package

        cls = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
        c = cls.__new__(cls)
        c.vin = "VIN"
        c.trip_stats = MagicMock()
        c.trip_stats.note_charge_state.return_value = (None, False)
        c._charge_snapshot = MagicMock(return_value="snapshot")
        c.resolve_battery_capacity_for = MagicMock(return_value=(CAPACITY, "profile"))
        c._schedule_trip_save = MagicMock()
        charging = NS(chrgMgmtData=NS(bmsChrgSts=1), rvsChargeStatus=rcs)
        cls._update_charge_state(c, NS(), charging)
        return c.trip_stats.note_charge_state.call_args.kwargs["stretch_s"]

    def test_the_cars_counter_in_seconds(self):
        self.assertEqual(self._call(NS(chargingGunState=1, chargingDuration=403)), 403)

    def test_a_car_without_one(self):
        self.assertIsNone(self._call(NS(chargingGunState=1)))
        self.assertIsNone(self._call(None))


if __name__ == "__main__":
    unittest.main()
