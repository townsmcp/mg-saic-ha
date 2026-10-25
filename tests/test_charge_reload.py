"""A reload mid-charge must not lose the reading the charge is measured from.

@HarryFlatter's HS PHEV, 10 Oct 2026 (times BST), a solar top-up in short
bursts:

    14:44:02  poll: plugged in, Charging Stopped, 70.2 %
              (the car's record: a burst 14:35:15-14:41:26)
    14:58:02  the integration reloads
    14:58:12  poll: charging, 71.3 % (burst from 14:54:57)
    14:58:21  poll: Charging Stopped, 71.3 % (burst 14:54:57-14:58:18)
    16:09:40  poll: Connecting -- the charge is closed

The 14:44 reading had never been saved, so after the reload the charge was
measured from 71.3 % to 71.3 %, gained nothing, and was dropped.
"""

import asyncio
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_charge_stats import CSnap, ts as TRIP

UTC = timezone.utc
CAPACITY = 23.2


def _at(h, m, s=0):
    return datetime(2026, 10, 10, h, m, s, tzinfo=UTC)


def _epoch(h, m, s):
    return _at(h, m, s).timestamp()


def _snap(at, soc, start, end):
    return CSnap(ts=at.isoformat(), soc_pct=soc, pack_energy_kwh=None,
                 odometer_km=6381.0, range_km=None,
                 record_start=start, record_end=end)


# Times UTC (BST - 1).
BEFORE = _snap(_at(13, 44, 2), 70.2, _epoch(13, 35, 15), _epoch(13, 41, 26))
CHARGING = _snap(_at(13, 58, 12), 71.3, _epoch(13, 54, 57), _epoch(13, 52, 44))
STOPPED = _snap(_at(13, 58, 21), 71.3, _epoch(13, 54, 57), _epoch(13, 58, 18))
LATER = _snap(_at(15, 9, 40), 71.3, _epoch(13, 54, 57), _epoch(13, 58, 18))


def _manager():
    return TRIP.TripStatsManager(MagicMock(), "entry", "VIN")


def _note(manager, charging, snap, *, paused=False):
    return manager.note_charge_state(
        charging, snap, capacity_kwh=CAPACITY, now_iso=snap.ts,
        is_plugged_in=True, charge_paused=paused,
    )


def _saved(manager):
    saved = {}

    class _Store:
        async def async_save(self, data):
            saved.update(data)

    manager._store = _Store()
    asyncio.run(manager.async_save())
    return saved


def _loaded(saved):
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


class HarrysSolarTopUpTests(unittest.TestCase):
    def test_the_reading_before_is_saved(self):
        manager = _manager()
        _, changed = _note(manager, False, BEFORE, paused=True)
        self.assertTrue(changed)  # the coordinator saves when this is True
        saved = _saved(manager)
        self.assertEqual(saved["pre_charge_snapshot"]["soc_pct"], 70.2)

    def test_after_the_reload_the_charge_is_measured_from_it(self):
        manager = _manager()
        _note(manager, False, BEFORE, paused=True)
        reloaded = _loaded(_saved(manager))

        _note(reloaded, True, CHARGING)
        self.assertEqual(reloaded.open_charge.soc_pct, 70.2)
        _note(reloaded, False, STOPPED, paused=True)
        charge, _ = _note(reloaded, False, LATER, paused=True)
        self.assertIsNotNone(charge)
        self.assertEqual(charge["soc_start_pct"], 70.2)
        self.assertEqual(charge["soc_added_pct"], 1.1)
        self.assertEqual(charge["energy_added_kWh"], round(0.011 * CAPACITY, 3))

    def test_the_same_reading_again_is_not_saved_again(self):
        manager = _manager()
        _note(manager, False, BEFORE, paused=True)
        again = _snap(_at(14, 44, 2), 70.2, _epoch(13, 35, 15), _epoch(13, 41, 26))
        _, changed = _note(manager, False, again, paused=True)
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
