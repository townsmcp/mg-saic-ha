"""India reset metadata stays bounded without changing the numerical guard.

Only HA storage/scheduling is replaced. The coordinator, reset detector,
serialization and restoration paths are the production implementations.
"""

import asyncio
import json
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - installs the HA test boundary
from test_counter_reset_guard import BEFORE, PHANTOM, _payload, _r

COORD = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
LOGIC = sys.modules["mg_saic.logic"]
TRIP = sys.modules["mg_saic.trip_stats"]


def _coordinator(*, india):
    coordinator = COORD.__new__(COORD)
    coordinator.client = NS(_india_backend=True) if india else NS()
    coordinator.vin = "TESTVIN"
    coordinator.counter_reset_guard = LOGIC.SinceChargeCounterGuard()
    coordinator.trip_stats = TRIP.TripStatsManager(None, "entry", "TESTVIN", india=india)
    coordinator._schedule_trip_save = MagicMock()
    return coordinator


def _apply(coordinator, stamp, reading):
    frame = _payload(reading)
    frame.india = True
    frame.rvsChargeStatus.india = True
    frame.rvsChargeStatus.measurement_time = stamp
    frame.rvsChargeStatus.chargingGunState = reading["plugged"]
    coordinator._apply_counter_reset_guard(
        frame, datetime.fromtimestamp(stamp, timezone.utc)
    )
    return frame.rvsChargeStatus


def _restart(coordinator, *, india):
    """Round-trip the whole trip store through JSON and the real load path."""
    saved = {}

    async def save(data):
        saved.update(json.loads(json.dumps(data)))

    coordinator.trip_stats._store = NS(async_save=save)
    asyncio.run(coordinator.trip_stats.async_save())
    manager = TRIP.TripStatsManager(None, "entry", "TESTVIN", india=india)
    store = NS(async_load=AsyncMock(return_value=saved))
    with patch.dict(sys.modules, {
        "homeassistant.helpers.storage": NS(Store=lambda *_a, **_k: store),
    }):
        asyncio.run(manager.async_load())
    coordinator.trip_stats = manager
    coordinator.counter_reset_guard = LOGIC.SinceChargeCounterGuard.from_dict(
        manager.counter_reset_guard, india=india
    )
    return saved


class IndiaCounterProvenanceTests(unittest.TestCase):
    def setUp(self):
        # A hundred deliberately bad frames must not flood the CI log.
        warning = patch.object(sys.modules["mg_saic.coordinator"].LOGGER, "warning")
        warning.start()
        self.addCleanup(warning.stop)

    def test_one_hundred_resets_and_restarts_preserve_totals_and_original_clocks(self):
        coordinator = _coordinator(india=True)
        _apply(coordinator, 100, BEFORE)
        sizes = []

        for index in range(100):
            reset_time = 200 + index * 200
            reading = _r(PHANTOM, end=PHANTOM["end"] + index,
                         odo=BEFORE["odo"] + index * 25)
            reset = _apply(coordinator, reset_time, reading)
            self.assertEqual(reset.mileageSinceLastCharge, 6100 + index * 250)
            self.assertEqual(reset.powerUsageSinceLastCharge, 266 + index * 40)
            driven = _apply(coordinator, reset_time + 100,
                            _r(reading, km=250, kwh=40, odo=reading["odo"] + 25))
            self.assertEqual(driven.mileageSinceLastCharge, 6100 + (index + 1) * 250)
            self.assertEqual(driven.powerUsageSinceLastCharge, 266 + (index + 1) * 40)
            self.assertEqual(driven.lastChargeEndingPower, 724)
            self.assertEqual(coordinator.counter_reset_guard.ignored_count, index + 1)
            held = driven.india_provenance["lastChargeEndingPower"]
            self.assertTrue(held["held"])
            self.assertEqual(held["measurement_time"], 100)
            selected = driven.india_provenance["mileageSinceLastCharge"]
            self.assertEqual(selected["measurement_time"], reset_time + 100)
            self.assertEqual(selected["inputs"]["raw"]["measurement_time"], reset_time + 100)
            self.assertNotIn("inputs", selected["inputs"]["offset"])

            metadata = coordinator.counter_reset_guard.india_provenance
            encoded = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
            sizes.append(len(encoded.encode("utf-8")))
            self.assertNotIn("previous_offset", encoded)
            self.assertLessEqual(sizes[-1], TRIP.INDIA_PROVENANCE_MAX_BYTES)
            self.assertLess(len(json.dumps(driven.india_provenance).encode("utf-8")), 16000)
            if index in (49, 99):
                before = coordinator.counter_reset_guard.to_dict(india=True)
                _restart(coordinator, india=True)
                self.assertEqual(coordinator.counter_reset_guard.to_dict(india=True), before)

        # A larger reset count only changes short scalar values, not history size.
        self.assertLess(max(sizes) - min(sizes), 256)
        final = _apply(coordinator, 20300, _r(PHANTOM, km=300, kwh=50,
                       end=PHANTOM["end"] + 99, odo=BEFORE["odo"] + 2505))
        self.assertEqual((final.mileageSinceLastCharge, final.powerUsageSinceLastCharge),
                         (31150, 4276))
        charge = _apply(coordinator, 20400, _r(PHANTOM, soc=80, plugged=True,
                        end=PHANTOM["end"] + 100, odo=BEFORE["odo"] + 2505))
        self.assertEqual((charge.mileageSinceLastCharge, charge.powerUsageSinceLastCharge), (0, 0))
        self.assertFalse(coordinator.counter_reset_guard.holding)
        self.assertEqual(charge.india_provenance["lastChargeEndingPower"]["measurement_time"], 20400)

    def test_unchanged_frame_does_not_schedule_a_metadata_only_save(self):
        coordinator = _coordinator(india=True)
        _apply(coordinator, 100, BEFORE)
        _apply(coordinator, 200, PHANTOM)
        coordinator._schedule_trip_save.reset_mock()
        _apply(coordinator, 200, PHANTOM)
        coordinator._schedule_trip_save.assert_not_called()

    def test_payload_marker_cannot_enable_metadata_on_global_backend(self):
        coordinator = _coordinator(india=False)
        first = _apply(coordinator, 100, BEFORE)
        self.assertFalse(hasattr(first, "india_provenance"))
        coordinator.counter_reset_guard.india_provenance = {
            "last": {"km": {"source": "charge", "measurement_time": 50}},
        }
        reset = _apply(coordinator, 200, PHANTOM)
        self.assertEqual((reset.mileageSinceLastCharge, reset.powerUsageSinceLastCharge), (6100, 266))
        self.assertFalse(hasattr(reset, "india_provenance"))
        self.assertNotIn("india_provenance", coordinator.counter_reset_guard.to_dict())
        self.assertNotIn("india_provenance", coordinator.trip_stats.counter_reset_guard)
        saved = _restart(coordinator, india=False)
        self.assertNotIn("india_provenance", saved["counter_reset_guard"])
        self.assertFalse(hasattr(coordinator.counter_reset_guard, "india_provenance"))
        self.assertEqual(coordinator.counter_reset_guard.offset_km, 6100)


if __name__ == "__main__":
    unittest.main()
