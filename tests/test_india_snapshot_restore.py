"""Restored optional India metadata cannot break numeric source comparisons.

Only Home Assistant storage I/O is replaced; manager loading, projection and
the coordinator's snapshot selection use their production implementations.
Run alone: python -m unittest test_india_snapshot_restore.
"""

import sys
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_setup_and_config_flow  # noqa: F401 - installs the HA test boundary


COORD = sys.modules["mg_saic.coordinator"].SAICMGDataUpdateCoordinator
TRIP = sys.modules["mg_saic.trip_stats"]
PREVIOUS_ISO = "2026-10-14T12:00:00+00:00"
PREVIOUS_EPOCH = datetime.fromisoformat(PREVIOUS_ISO).timestamp()


async def _restore(metadata):
    saved = {
        "open_snapshot": {
            "ts": PREVIOUS_ISO,
            "odometer_km": 1000,
            "soc_pct": 60,
            "since_charge_km": 10,
            "since_charge_kwh": 2,
            "india_provenance": metadata,
        },
    }
    store = NS(async_load=AsyncMock(return_value=saved))
    manager = TRIP.TripStatsManager(None, "entry", "SYNTHETIC-VIN", india=True)
    with patch.dict(sys.modules, {
        "homeassistant.helpers.storage": NS(Store=lambda *_args, **_kwargs: store),
    }):
        await manager.async_load()
    coordinator = COORD.__new__(COORD)
    coordinator.client = NS(_india_backend=True)
    coordinator.trip_stats = manager
    return coordinator


def _ordinary(soc, stamp):
    return NS(
        india=True,
        battery_soc_available=True,
        extendedData1=soc,
        measurement_time=stamp,
    )


class IndiaSnapshotRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_iso_or_malformed_selected_clocks_cannot_block_a_numeric_observation(self):
        for selected_clock in (PREVIOUS_ISO, "invalid", True, None, {"bad": "clock"}, 2 ** 80):
            with self.subTest(selected_clock=selected_clock):
                coordinator = await _restore({
                    "soc_pct": {
                        "source": "ordinary", "measurement_time": selected_clock,
                        "updated_at": PREVIOUS_ISO,
                    },
                    "ts": {"source": "snapshot", "field": "ts", "value": PREVIOUS_ISO},
                })
                snapshot = coordinator.trip_stats.open_snapshot
                self.assertEqual(snapshot.soc_pct, 60)
                self.assertEqual(snapshot.odometer_km, 1000)
                self.assertEqual(snapshot.since_charge_km, 10)
                self.assertEqual(snapshot.since_charge_kwh, 2)
                self.assertEqual(snapshot.ts, PREVIOUS_ISO)
                self.assertIsNone(snapshot.india_provenance["soc_pct"]["measurement_time"])
                self.assertEqual(snapshot.india_provenance["soc_pct"]["updated_at"], PREVIOUS_ISO)
                self.assertEqual(snapshot.india_provenance["ts"]["value"], PREVIOUS_ISO)

                value, origin = coordinator._india_snapshot_measurement(
                    _ordinary(61, PREVIOUS_EPOCH + 1), None, "soc"
                )
                self.assertEqual(value, 61)
                self.assertEqual(origin["measurement_time"], PREVIOUS_EPOCH + 1)
                self.assertEqual(snapshot.soc_pct, 60)

    async def test_a_valid_numeric_clock_in_legacy_raw_input_still_rejects_older_data(self):
        coordinator = await _restore({
            "soc_pct": {
                "source": "derived", "measurement_time": PREVIOUS_ISO,
                "inputs": {"raw": {
                    "source": "ordinary", "field": "extendedData1",
                    "measurement_time": PREVIOUS_EPOCH,
                }},
            },
        })
        origin = coordinator.trip_stats.open_snapshot.india_provenance["soc_pct"]
        self.assertEqual(origin["measurement_time"], PREVIOUS_EPOCH)
        value, selected = coordinator._india_snapshot_measurement(
            _ordinary(59, PREVIOUS_EPOCH - 1), None, "soc"
        )
        self.assertEqual(value, 60)
        self.assertEqual(selected["measurement_time"], PREVIOUS_EPOCH)
        self.assertEqual(selected["inputs"]["raw"]["measurement_time"], PREVIOUS_EPOCH)

    async def test_future_metadata_version_does_not_discard_snapshot_values(self):
        coordinator = await _restore({
            "schema_version": 999,
            "soc_pct": {"source": "ordinary", "measurement_time": PREVIOUS_ISO},
        })
        snapshot = coordinator.trip_stats.open_snapshot
        self.assertEqual(snapshot.soc_pct, 60)
        self.assertEqual(snapshot.odometer_km, 1000)
        self.assertEqual(snapshot.ts, PREVIOUS_ISO)
        self.assertFalse(hasattr(snapshot, "india_provenance"))
        self.assertNotIn("india_provenance", snapshot.to_dict(india=True))
        value, origin = coordinator._india_snapshot_measurement(
            _ordinary(61, PREVIOUS_EPOCH + 1), None, "soc"
        )
        self.assertEqual(value, 61)
        self.assertEqual(origin["measurement_time"], PREVIOUS_EPOCH + 1)


if __name__ == "__main__":
    unittest.main()
