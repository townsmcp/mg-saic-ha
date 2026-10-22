"""Bounded India metadata across real trip, persistence and legacy-load paths.

The numerical trip calculations and HA storage version remain shared. Only
an explicitly India-enabled manager keeps the optional diagnostic metadata.
"""

import asyncio
import copy
import json
import sys
import unittest
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from test_charge_stats import ts as TRIP


START = datetime(2026, 10, 1, tzinfo=timezone.utc)
FIELDS = {
    "ts": ("snapshot", "ts"),
    "odometer_km": ("status", "mileage"),
    "soc_pct": ("charge", "bmsPackSOCDsp"),
    "fuel_pct": ("status", "fuelLevelPrc"),
    "since_charge_km": ("charge", "mileageSinceLastCharge"),
    "since_charge_kwh": ("charge", "powerUsageSinceLastCharge"),
    "capacity_kwh": ("config", "battery_capacity"),
    "tank_litres": ("config", "fuel_tank_size"),
    "range_km": ("charge", "fuelRangeElec"),
    "pack_energy_kwh": ("charge", "lastChargeEndingPower"),
    "record_start": ("charge", "startTime"),
    "record_end": ("charge", "endTime"),
    "chargingDuration": ("charge", "chargingDuration"),
    "power_kw": ("derived", "power_kw"),
    "soc_low_pct": ("charge", "bmsPackSOCDsp"),
}


def _origins(stamp, *, detailed=False):
    result = {}
    for key, (source, field) in FIELDS.items():
        origin = {
            "source": source, "field": field, "measurement_time": stamp,
            "held": key == "pack_energy_kwh", "factor": 0.1,
        }
        if detailed:
            # All supported descriptive details at once, including four
            # direct observations. An ordinary snapshot rarely has them all.
            origin.update({
                "operation": "selected raw value plus the current offset after an ignored counter reset",
                "method": "use the selected source clock; offsets summarize earlier ignored resets",
                "resolution": "tenths of the source unit before the configured correction factor",
                "substitution": "retain the last usable reading while the source has not advanced",
                "value": 123.4, "energy_correction": 1.0,
                "current_factor": 0.05, "voltage_factor": 0.25,
                "updated_at": stamp, "reset_count": 100,
                "offset_value": 234.5, "offset_updated_at": stamp,
                "inputs": {
                    name: {"source": "charge", "field": FIELDS["since_charge_kwh"][1],
                           "measurement_time": stamp - index, "held": False,
                           "factor": 0.1, "energy_correction": 1.0}
                    for index, name in enumerate(("raw", "offset", "odometer", "baseline"))
                },
            })
        result[key] = origin
    return result


def _snap(index, *, end=False, india=True):
    stamp = START + timedelta(minutes=index * 4 + (2 if end else 0))
    energy = round(index * 0.2 + (0.15 if end else 0), 3)
    snapshot = TRIP.TripSnapshot(
        ts=stamp.isoformat(), odometer_km=1000.0 + index + (1 if end else 0),
        soc_pct=round(80 - energy / 66.0 * 100, 3),
        since_charge_km=float(index + (1 if end else 0)),
        since_charge_kwh=energy,
    )
    if india:
        snapshot.india_provenance = _origins(stamp.timestamp())
    return snapshot


def _manager(*, india=True):
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN", india=india)


def _drive(manager, index, *, india=True):
    manager.open(_snap(index, india=india))
    return manager.close(
        _snap(index, end=True, india=india), capacity_kwh=66.0,
        tank_litres=None, is_electric=True, is_combustion=False,
    )


def _saved(manager):
    saved = {}

    async def save(data):
        saved.update(json.loads(json.dumps(data)))

    manager._store = NS(async_save=save)
    asyncio.run(manager.async_save())
    return saved


def _loaded(saved, *, india=True):
    manager = _manager(india=india)
    store = NS(async_load=AsyncMock(return_value=saved))
    with patch.dict(sys.modules, {
        "homeassistant.helpers.storage": NS(Store=lambda *_a, **_k: store),
    }):
        asyncio.run(manager.async_load())
    return manager


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class IndiaTripProvenanceTests(unittest.TestCase):
    def test_one_hundred_trips_and_restarts_keep_totals_without_accumulating_history(self):
        manager = _manager()
        sizes = []
        for index in range(100):
            trip = _drive(manager, index)
            self.assertEqual(trip["distance_km"], 1.0)
            self.assertEqual(trip["energy_kWh"], 0.15)
            tally = manager.driving_since_charge
            self.assertEqual(tally["trips"], index + 1)
            self.assertEqual(tally["distance_km"], float(index + 1))
            self.assertAlmostEqual(tally["energy_kWh"], (index + 1) * 0.15)
            provenance = tally["india_provenance"]
            self.assertNotIn("trips", provenance)
            self.assertEqual(provenance["latest_trip"], trip["india_provenance"])
            end_clock = _snap(index, end=True).india_provenance["since_charge_kwh"]["measurement_time"]
            self.assertEqual(provenance["latest_trip"]["end"]["since_charge_kwh"]["measurement_time"], end_clock)
            result = TRIP.compute_driving_since_charge(
                tally, index + 1, round(index * 0.2 + 0.15, 3)
            )
            self.assertEqual(result["parked_energy_kWh"], round(index * 0.05, 3))
            self.assertLessEqual(_bytes(result["india_provenance"]), TRIP.INDIA_PROVENANCE_MAX_BYTES)
            sizes.append(_bytes(provenance))
            if index in (49, 99):
                saved = _saved(manager)
                manager = _loaded(saved)
                self.assertEqual(manager.driving_since_charge, tally)
                self.assertEqual(_saved(manager), saved)

        # A later trip replaces fixed fields; counts change only numeric totals.
        self.assertLess(max(sizes[1:]) - min(sizes[1:]), 128)
        self.assertLessEqual(max(sizes), TRIP.INDIA_PROVENANCE_MAX_BYTES)
        self.assertEqual(manager.driving_since_charge["trips"], 100)
        self.assertEqual(manager.driving_since_charge["distance_km"], 100.0)
        self.assertEqual(manager.driving_since_charge["energy_kWh"], 15.0)

    def test_legacy_history_is_normalized_on_load_before_any_new_trip(self):
        manager = _manager()
        _drive(manager, 0)
        saved = _saved(manager)
        old_trip = copy.deepcopy(saved["last_trip"]["india_provenance"])
        history = []
        for stamp in range(100):
            item = copy.deepcopy(old_trip)
            item.pop("schema_version", None)
            item["end"]["since_charge_kwh"]["measurement_time"] = stamp
            history.append(item)
        saved["driving_since_charge"].update({
            "trips": 100, "distance_km": 100.0, "energy_kWh": 15.0,
            "since_charge_km": 100.0,
            "india_provenance": {"trips": history,
                                 "since_charge_km": _origins(99)["since_charge_km"]},
        })
        old_size = _bytes(saved["driving_since_charge"]["india_provenance"])
        self.assertGreater(old_size, 16000)

        restored = _loaded(saved)
        tally = restored.driving_since_charge
        self.assertEqual((tally["trips"], tally["distance_km"], tally["energy_kWh"]),
                         (100, 100.0, 15.0))
        provenance = tally["india_provenance"]
        self.assertNotIn("trips", provenance)
        self.assertEqual(provenance["latest_trip"]["end"]["since_charge_kwh"]["measurement_time"], 99)
        self.assertLessEqual(_bytes(provenance), TRIP.INDIA_PROVENANCE_MAX_BYTES)
        result = TRIP.compute_driving_since_charge(tally, 100.0, 19.95)
        self.assertEqual(result["parked_energy_kWh"], 4.95)
        self.assertLess(_bytes(result), 16000)
        self.assertEqual(len(saved["driving_since_charge"]["india_provenance"]["trips"]), 100)
        self.assertEqual(_loaded(_saved(restored)).driving_since_charge, tally)

    def test_unknown_or_malformed_metadata_never_discards_numeric_state(self):
        manager = _manager()
        _drive(manager, 0)
        saved = _saved(manager)
        snapshot = _snap(1).to_dict(india=True)
        for metadata in ("bad metadata", [1, 2], {"schema_version": 2, "soc_pct": {}},
                         {"schema_version": True, "soc_pct": {}}, {"soc_pct": "bad origin"}):
            with self.subTest(metadata=metadata):
                legacy = copy.deepcopy(saved)
                legacy["open_snapshot"] = {**snapshot, "india_provenance": metadata}
                for key in ("last_trip", "driving_since_charge", "since_charge_baseline"):
                    legacy[key]["india_provenance"] = metadata
                legacy["counter_reset_guard"] = {
                    "offset_km": 6100.0, "offset_kwh": 266.0,
                    "holding": True, "india_provenance": metadata,
                }
                restored = _loaded(legacy)
                self.assertEqual(asdict(restored.open_snapshot), asdict(_snap(1)))
                self.assertFalse(hasattr(restored.open_snapshot, "india_provenance"))
                self.assertEqual(restored.last_trip["distance_km"], 1.0)
                self.assertEqual(restored.driving_since_charge["energy_kWh"], 0.15)
                self.assertEqual(restored.since_charge_baseline["since_charge_km"], 1.0)
                self.assertEqual(restored.counter_reset_guard,
                                 {"offset_km": 6100.0, "offset_kwh": 266.0, "holding": True})
                self.assertNotIn("india_provenance", _saved(restored)["last_trip"])

    def test_global_default_ignores_foreign_metadata_and_preserves_dictionary_shapes(self):
        expected = _manager(india=False)
        actual = TRIP.TripStatsManager(MagicMock(), "entry", "VIN")
        for index in range(3):
            self.assertEqual(_drive(actual, index), _drive(expected, index, india=False))
        self.assertEqual(_saved(actual), _saved(expected))
        india = _manager()
        _drive(india, 0)
        restored_global = _loaded(_saved(india), india=False)
        clean_global = _manager(india=False)
        _drive(clean_global, 0, india=False)
        self.assertEqual(_saved(restored_global), _saved(clean_global))
        for cls, snapshot in (
            (TRIP.TripSnapshot, _snap(0)),
            (TRIP.ChargeSnapshot, TRIP.ChargeSnapshot(ts=START.isoformat(), soc_pct=80)),
        ):
            snapshot.india_provenance = _origins(100)
            self.assertEqual(snapshot.to_dict(), asdict(snapshot))
            self.assertFalse(hasattr(cls.from_dict(snapshot.to_dict(india=True)), "india_provenance"))
        self.assertEqual(TRIP.STORAGE_VERSION, 1)


class IndiaOriginCompactionTests(unittest.TestCase):
    def test_legacy_reset_ancestry_is_bounded_detached_and_keeps_selected_clock(self):
        raw = {"source": "charge", "field": "mileageSinceLastCharge", "measurement_time": 100, "held": True}
        previous = {"source": "derived", "measurement_time": 10}
        for _ in range(1000):
            previous = {"source": "derived", "inputs": {"previous_offset": previous}}
        origin = {"source": "derived", "operation": "raw + offset", "measurement_time": None,
                  "factor": 0.1, "energy_correction": 1.0,
                  "inputs": {"raw": raw, "offset": previous}}
        compact = TRIP.compact_india_origin(origin)
        self.assertEqual(compact["measurement_time"], 100)
        self.assertEqual(compact["inputs"]["raw"]["measurement_time"], 100)
        self.assertEqual(compact["factor"], 0.1)
        self.assertEqual(compact["energy_correction"], 1.0)
        self.assertNotIn("previous_offset", json.dumps(compact))
        raw["measurement_time"] = 200
        self.assertEqual(compact["inputs"]["raw"]["measurement_time"], 100)
        self.assertIsNone(origin["measurement_time"])
        circular = {"source": "filtered", "measurement_time": None}
        circular["input"] = circular
        self.assertIsNone(TRIP.compact_india_origin(circular)["measurement_time"])
        circular["inputs"] = {"raw": circular}
        self.assertLess(_bytes(TRIP.compact_india_origin(circular)), 1024)

    def test_malformed_scalars_are_safe_and_descriptive_iso_labels_survive(self):
        for bad_clock in (True, "2026-10-01T00:00:00+00:00", float("inf"), float("nan"), 10 ** 100):
            result = TRIP.compact_india_origin({
                "source": "charge", "field": "bmsPackSOCDsp", "measurement_time": bad_clock,
                "value": START.isoformat(), "updated_at": START.isoformat(),
            })
            self.assertIsNone(result["measurement_time"])
            self.assertEqual(result["value"], START.isoformat())
            self.assertEqual(result["updated_at"], START.isoformat())
        invalid = TRIP.compact_india_origin({
            "source": "\ud800", "field": "\u0000" * 48, "measurement_time": 123,
            "held": "true", "factor": float("nan"), "operation": ["unexpected"],
        })
        self.assertEqual(invalid, {"source": "unknown", "measurement_time": 123})
        self.assertLessEqual(_bytes(invalid), TRIP.INDIA_PROVENANCE_MAX_BYTES)

    def test_worst_full_field_summaries_preserve_every_selected_source_clock(self):
        snapshot = _origins(1790812800, detailed=True)
        trip = {"start": snapshot, "end": snapshot, "baseline": snapshot,
                "capacity_kwh": {"value": 66.0, "origin": snapshot["capacity_kwh"]}}
        charge = {"start": snapshot, "end": snapshot,
                  "capacity_kwh": {"value": 66.0, "origin": snapshot["capacity_kwh"]}}
        tally = {"latest_trip": trip, "since_charge_km": snapshot["since_charge_km"]}
        driving = {"tally": tally, "parked_reference": snapshot["since_charge_kwh"],
                   "distance_since_charge": snapshot["since_charge_km"],
                   "energy_since_charge": snapshot["since_charge_kwh"]}
        sensor = {"baseline": snapshot, "distance": snapshot["odometer_km"],
                  "energy": snapshot["pack_energy_kwh"], "soc_pct": snapshot["soc_pct"],
                  "odometer_km": snapshot["odometer_km"], "capacity_kwh": snapshot["capacity_kwh"]}
        guard = {"last": {key: snapshot["since_charge_kwh"] for key in ("km", "kwh", "ending", "odo")},
                 "raw": {key: snapshot["since_charge_kwh"] for key in ("km", "kwh", "ending", "odo")},
                 **{key: snapshot["since_charge_kwh"] for key in ("offset_km", "offset_kwh", "held_ending", "baseline_odo")}}

        def selected_origins(item):
            if not isinstance(item, dict):
                return []
            if "source" in item:
                return [item]
            return [origin for value in item.values() for origin in selected_origins(value)]

        # Counts are the public fixed schema: start/end fields, relevant
        # baseline fields, selected capacity and the named current counters.
        for kind, value, selected_count in (
            ("snapshot", snapshot, 15), ("trip", trip, 19), ("charge", charge, 21),
            ("tally", tally, 20), ("driving", driving, 23),
            ("sensor", sensor, 9), ("guard", guard, 12),
        ):
            with self.subTest(kind=kind):
                compact = TRIP.compact_india_provenance(value, kind=kind)
                self.assertLessEqual(_bytes(compact), TRIP.INDIA_PROVENANCE_MAX_BYTES)
                origins = selected_origins(compact)
                self.assertEqual(len(origins), selected_count)
                for origin in origins:
                    self.assertEqual(origin["measurement_time"], 1790812800)
                    self.assertIn(origin["source"], ("status", "charge", "config", "snapshot", "derived"))
                    self.assertIn(origin["field"], {field for _, field in FIELDS.values()})
                    self.assertIs(type(origin["held"]), bool)
                self.assertEqual(TRIP.compact_india_provenance(compact, kind=kind), compact)
        self.assertIn("inputs", snapshot["since_charge_kwh"])

    def test_charge_formulas_and_selected_capacity_survive_normal_compaction(self):
        start = TRIP.ChargeSnapshot(ts=START.isoformat(), soc_pct=40, pack_energy_kwh=20, range_km=150)
        end = TRIP.ChargeSnapshot(ts=(START + timedelta(hours=1)).isoformat(), soc_pct=50,
                                 pack_energy_kwh=26.6, range_km=190)
        start.india_provenance = _origins(100)
        end.india_provenance = _origins(200)
        charge = TRIP.compute_charge_session(start, end, capacity_kwh=66.0)
        self.assertEqual(charge["energy_added_kWh"], 6.6)
        provenance = charge["india_provenance"]
        self.assertEqual(provenance["calculations"]["energy_soc"],
                         "(end.soc_pct - start.soc_pct) / 100 * capacity_kwh")
        self.assertEqual(provenance["calculations"]["energy_counter"],
                         "end.pack_energy_kwh - start.pack_energy_kwh")
        self.assertEqual(provenance["calculations"]["range_added"], "end.range_km - start.range_km")
        self.assertEqual(provenance["calculations"]["average_power"], "selected energy / duration_s * 3600")
        self.assertEqual(provenance["calculations"]["energy_measured"], "power_integration_with_boundary_estimates")
        self.assertEqual(provenance["capacity_kwh"]["value"], 66.0)
        self.assertEqual(provenance["capacity_kwh"]["origin"]["measurement_time"], 200)
        self.assertLessEqual(_bytes(provenance), TRIP.INDIA_PROVENANCE_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
