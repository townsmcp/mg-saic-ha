"""Phantom since-charge counter resets (#262).

@HarryFlatter's log (MG HS PHEV) caught the car resetting its own since-charge
counters without a charge: after a ~2 h SAIC outage, the first good response
had Mileage Since Last Charge 610 km -> 0, Power Usage Since Last Charge -> 0,
lastChargeEndingPower reset to the pack's current energy, and a charge record
stamped mid-outage with startTime 0 -- while SOC, plug state, charging status
and odometer were unchanged. The genuine charge record earlier in the same log
has a real start and end.

The two payloads below are copied from that log.
"""

import asyncio
import logging
import sys
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

from test_erac_fallback import LOGIC
import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charging_data_freshness import UpdateCycleRecordingTests

COORD_MOD = sys.modules["mg_saic.coordinator"]
TRIP_MOD = sys.modules["mg_saic.trip_stats"]

# 21 Sep 15:47 -- before the outage. Genuine record: Sat 19 Sep 11:26 -> 16:06 UTC.
BEFORE = dict(km=6100, kwh=266, ending=724, start=1789817185, end=1789834017,
              soc=63.2, odo=60860, plugged=False)
# 21 Sep 18:56 -- first good response after the outage. Phantom record.
PHANTOM = dict(km=0, kwh=0, ending=458, start=0, end=1790011819,
               soc=63.2, odo=60860, plugged=False)


def _r(base, **changes):
    reading = dict(base)
    reading.update(changes)
    return reading


def _shown(adjusted):
    return adjusted["km"], adjusted["kwh"], adjusted["ending"]


class GuardLogicTests(unittest.TestCase):
    def setUp(self):
        self.guard = LOGIC.SinceChargeCounterGuard()
        self.guard.process("t0", BEFORE)

    # ── the phantom ──

    def test_harrys_phantom_reset_is_ignored_and_held(self):
        adjusted, event, persist = self.guard.process("t1", PHANTOM)
        self.assertEqual(event, "ignored")
        self.assertTrue(persist)
        self.assertEqual(_shown(adjusted), (6100, 266, 724))
        self.assertEqual(
            self.guard.attributes(),
            {"counter_reset_held": True, "ignored_counter_reset_at": "t1",
             "ignored_counter_resets": 1, "mileage_since_charge_from_odometer": False},
        )

    def test_driving_after_a_held_reset_counts_on_top(self):
        self.guard.process("t1", PHANTOM)
        adjusted, event, _ = self.guard.process(
            "t2", _r(PHANTOM, km=250, kwh=40, odo=60885, soc=58.0))
        self.assertIsNone(event)
        self.assertEqual(_shown(adjusted), (6350, 306, 724))

    def test_a_second_phantom_folds_in_again(self):
        self.guard.process("t1", PHANTOM)
        self.guard.process("t2", _r(PHANTOM, km=250, kwh=40, odo=60885, soc=58.0))
        adjusted, event, _ = self.guard.process(
            "t3", _r(PHANTOM, km=0, kwh=0, end=1790099999, odo=60885, soc=58.0))
        self.assertEqual(event, "ignored")
        self.assertEqual(_shown(adjusted), (6350, 306, 724))
        self.assertEqual(self.guard.ignored_count, 2)

    # ── genuine charges are accepted ──

    def test_soc_rise_while_parked_is_a_charge(self):
        adjusted, event, _ = self.guard.process("t1", _r(PHANTOM, soc=64.5))
        self.assertIsNone(event)
        self.assertEqual(_shown(adjusted), (0, 0, 458))

    def test_parked_soc_wobble_is_not_a_charge(self):
        _, event, _ = self.guard.process("t1", _r(PHANTOM, soc=63.7))
        self.assertEqual(event, "ignored")

    def test_small_soc_rise_with_driving_is_not_a_charge(self):
        """Regen can add a little between polls -- not proof of a charge."""
        _, event, _ = self.guard.process(
            "t1", _r(PHANTOM, km=30, soc=65.0, odo=60863))
        self.assertEqual(event, "ignored")

    def test_large_soc_rise_is_a_charge_even_after_driving(self):
        adjusted, event, _ = self.guard.process(
            "t1", _r(PHANTOM, km=120, soc=71.0, odo=60872))
        self.assertIsNone(event)
        self.assertEqual(adjusted["km"], 120)

    def test_plugged_in_at_reset_is_a_charge(self):
        _, event, _ = self.guard.process("t1", _r(PHANTOM, plugged=True))
        self.assertIsNone(event)

    def test_plugged_in_on_an_earlier_poll_is_a_charge(self):
        self.guard.process("t1", _r(BEFORE, plugged=True))
        _, event, _ = self.guard.process("t2", PHANTOM)
        self.assertIsNone(event)

    def test_plug_evidence_is_spent_once_the_car_drives(self):
        self.guard.process("t1", _r(BEFORE, plugged=True))
        self.guard.process("t2", _r(BEFORE, km=6300, odo=60880))  # driven off
        _, event, _ = self.guard.process("t3", _r(PHANTOM, odo=60880))
        self.assertEqual(event, "ignored")

    def test_real_charge_record_is_a_charge(self):
        """Covers a charge + drive between polls that SOC alone can't prove."""
        _, event, _ = self.guard.process(
            "t1", _r(PHANTOM, km=50, soc=64.0, odo=60865,
                     start=1790000000, end=1790011819))
        self.assertIsNone(event)

    # ── clearing a hold ──

    def test_genuine_charge_clears_the_hold(self):
        self.guard.process("t1", PHANTOM)
        adjusted, event, persist = self.guard.process(
            "t2", _r(PHANTOM, end=1790050000, start=1790030000,
                     soc=80.0, ending=700))
        self.assertEqual(event, "accepted")
        self.assertTrue(persist)
        self.assertEqual(_shown(adjusted), (0, 0, 700))
        self.assertFalse(self.guard.holding)

    def test_genuine_charge_clears_the_hold_even_with_counters_still_at_zero(self):
        """Not driven since the phantom: the counters can't drop any further,
        so the new charge record's end time is what reveals the reset."""
        self.guard.process("t1", PHANTOM)
        _, event, _ = self.guard.process(
            "t2", _r(PHANTOM, end=1790050000, plugged=True))
        self.assertEqual(event, "accepted")
        self.assertFalse(self.guard.holding)

    # ── fail-safe and bookkeeping ──

    def test_first_reading_is_only_a_baseline(self):
        guard = LOGIC.SinceChargeCounterGuard()
        adjusted, event, persist = guard.process("t0", PHANTOM)
        # Saved once: the first reading establishes the charge baseline
        # (odometer - mileage since charge), used if SAIC later sends the
        # odometer as the mileage.
        self.assertEqual((event, persist), (None, True))
        self.assertEqual(_shown(adjusted), (0, 0, 458))

    def test_missing_soc_does_not_invent_evidence(self):
        _, event, _ = self.guard.process("t1", _r(PHANTOM, soc=None))
        self.assertEqual(event, "ignored")

    def test_normal_polls_do_not_persist_when_not_holding(self):
        # A realistic drive: mileage and odometer both +5.0 km, so the charge
        # baseline doesn't move. (This used odo +0.5 km, which can't happen.)
        _, _, persist = self.guard.process("t1", _r(BEFORE, km=6150, odo=60910))
        self.assertFalse(persist)

    def test_changes_persist_while_holding(self):
        self.guard.process("t1", PHANTOM)
        _, _, persist = self.guard.process("t2", _r(PHANTOM, km=10, odo=60861))
        self.assertTrue(persist)
        _, _, persist = self.guard.process("t3", _r(PHANTOM, km=10, odo=60861))
        self.assertFalse(persist, "nothing changed")

    def test_round_trip_survives_a_restart(self):
        self.guard.process("t1", PHANTOM)
        restored = LOGIC.SinceChargeCounterGuard.from_dict(self.guard.to_dict())
        adjusted, event, _ = restored.process("t2", PHANTOM)
        self.assertIsNone(event)
        self.assertEqual(_shown(adjusted), (6100, 266, 724))

    def test_from_dict_tolerates_nothing_stored(self):
        self.assertFalse(LOGIC.SinceChargeCounterGuard.from_dict(None).holding)


class TripStatsPersistenceTests(unittest.TestCase):
    def test_guard_state_is_saved_and_loaded(self):
        saved = {}

        class _FakeStore:
            def __init__(self, *_args, **_kwargs):
                pass

            async def async_save(self, data):
                saved.update(data)

            async def async_load(self):
                return dict(saved)

        storage = type(sys)("homeassistant.helpers.storage")
        storage.Store = _FakeStore
        previous = sys.modules.get("homeassistant.helpers.storage")
        sys.modules["homeassistant.helpers.storage"] = storage
        try:
            manager = TRIP_MOD.TripStatsManager(MagicMock(), "entry", "VIN")
            _run = asyncio.new_event_loop().run_until_complete
            _run(manager.async_load())
            manager.counter_reset_guard = {"offset_km": 6100}
            _run(manager.async_save())

            reloaded = TRIP_MOD.TripStatsManager(MagicMock(), "entry", "VIN")
            _run(reloaded.async_load())
            self.assertEqual(reloaded.counter_reset_guard, {"offset_km": 6100})
        finally:
            if previous is None:
                sys.modules.pop("homeassistant.helpers.storage", None)
            else:
                sys.modules["homeassistant.helpers.storage"] = previous


def _payload(reading):
    return NS(
        rvsChargeStatus=NS(
            mileageSinceLastCharge=reading["km"],
            powerUsageSinceLastCharge=reading["kwh"],
            lastChargeEndingPower=reading["ending"],
            startTime=reading["start"],
            endTime=reading["end"],
            mileage=reading["odo"],
            chargingGunState=0,
        ),
        chrgMgmtData=NS(
            bmsPackSOCDsp=round(reading["soc"] * 10),
            bmsChrgSts=0,
            ccuOnbdChrgrPlugOn=0,
            ccuOffBdChrgrPlugOn=0,
        ),
    )


class CoordinatorIntegrationTests(unittest.TestCase):
    """The real _run_update_cycle, with Harry's two payloads in sequence."""

    def _coord(self, payloads):
        payloads = list(payloads)
        c = UpdateCycleRecordingTests._coord(
            UpdateCycleRecordingTests(),
            status=UpdateCycleRecordingTests._status,
            charging=lambda: payloads.pop(0),
        )
        c.counter_reset_guard = LOGIC.SinceChargeCounterGuard()
        c.trip_stats = NS(counter_reset_guard=None)
        c._schedule_trip_save = MagicMock()
        return c

    def _cycle(self, c):
        data = asyncio.new_event_loop().run_until_complete(c._run_update_cycle())
        return data["charging"].rvsChargeStatus

    def test_phantom_payload_is_corrected_before_anything_reads_it(self):
        c = self._coord([_payload(BEFORE), _payload(PHANTOM)])
        self._cycle(c)
        with self.assertLogs(COORD_MOD.LOGGER, logging.WARNING) as logs:
            rcs = self._cycle(c)
        self.assertEqual(
            (rcs.mileageSinceLastCharge, rcs.powerUsageSinceLastCharge,
             rcs.lastChargeEndingPower),
            (6100, 266, 724),
        )
        self.assertTrue(any("reset without a charge" in m for m in logs.output))
        self.assertEqual(c.trip_stats.counter_reset_guard["offset_km"], 6100)
        c._schedule_trip_save.assert_called()
        self.assertEqual(c.charging_data_freshness, "live")

    def test_genuine_payload_is_untouched(self):
        c = self._coord([_payload(BEFORE), _payload(_r(PHANTOM, soc=70.0))])
        self._cycle(c)
        rcs = self._cycle(c)
        self.assertEqual(rcs.mileageSinceLastCharge, 0)
        self.assertEqual(rcs.lastChargeEndingPower, 458)
        # The payload is untouched. State IS saved now -- the charge moved the
        # baseline (odometer at the last charge) -- but nothing is held.
        self.assertEqual(c.trip_stats.counter_reset_guard["baseline_odo"], 60860)
        self.assertEqual(c.trip_stats.counter_reset_guard["offset_km"], 0)

    def test_a_guard_failure_never_costs_the_poll(self):
        c = self._coord([_payload(BEFORE)])
        c.counter_reset_guard = None  # .process -> AttributeError
        rcs = self._cycle(c)
        self.assertEqual(rcs.mileageSinceLastCharge, 6100)
        self.assertEqual(c.charging_data_freshness, "live")


# ── SAIC sending the odometer as Mileage Since Last Charge ───────────────
#
# @HarryFlatter's HS PHEV, raw values from his logs: after the 23 Sep charge
# mileageSinceLastCharge == odometer (61120/61120), rising with it on the next
# drives (61150/61150 at 13:54, 61160/61160 at 14:33 on 24 Sep) until the
# 14:48 plug-in reset it to 0. The 25 Sep charge ended correctly at 0.

def _h(km, kwh, odo, end, soc, *, plugged=False, start=1):
    return dict(km=km, kwh=kwh, ending=724, start=start, end=end, soc=soc,
                odo=odo, plugged=plugged)


PRE_CHARGE = _h(3200, 266, 61120, 100, 69.1)                       # sane
CHARGE_END = _h(61120, 0, 61120, 200, 100.0, plugged=True, start=150)
DRIVE_1354 = _h(61150, 21, 61150, 200, 97.0)
DRIVE_1433 = _h(61160, 30, 61160, 200, 95.7)
PLUG_1448 = _h(0, 0, 61180, 300, 94.8, plugged=True, start=250)


class OdometerInMileageTests(unittest.TestCase):
    def _shown(self, guard, reading):
        adjusted, _event, _persist = guard.process("t", reading)
        return adjusted["km"], adjusted["km_from_odometer"]

    def test_harrys_sequence(self):
        g = LOGIC.SinceChargeCounterGuard()
        self.assertEqual(self._shown(g, PRE_CHARGE), (3200, False))
        self.assertEqual(self._shown(g, CHARGE_END), (0, True), "a genuine charge, just now")
        self.assertEqual(self._shown(g, DRIVE_1354), (30, True), "3.0 km -- the reconstructed trip")
        self.assertEqual(self._shown(g, DRIVE_1433), (40, True))
        self.assertEqual(self._shown(g, PLUG_1448), (0, False), "sane again at the plug-in")
        self.assertEqual(self._shown(g, _h(120, 15, 61300, 300, 90.0)), (120, False))

    def test_no_baseline_means_nothing_rather_than_the_odometer(self):
        """Harry's own state on upgrading: the fault started before the guard
        existed, so there's no charge baseline to work from."""
        g = LOGIC.SinceChargeCounterGuard()
        self.assertEqual(self._shown(g, DRIVE_1354), (None, True))

    def test_it_is_flagged_in_the_attributes(self):
        g = LOGIC.SinceChargeCounterGuard()
        g.process("t", PRE_CHARGE)
        g.process("t", CHARGE_END)
        self.assertTrue(g.attributes()["mileage_since_charge_from_odometer"])
        g.process("t", PLUG_1448)
        self.assertFalse(g.attributes()["mileage_since_charge_from_odometer"])

    def test_baseline_survives_a_restart(self):
        g = LOGIC.SinceChargeCounterGuard()
        g.process("t", PRE_CHARGE)
        g.process("t", CHARGE_END)
        restored = LOGIC.SinceChargeCounterGuard.from_dict(g.to_dict())
        self.assertEqual(self._shown(restored, DRIVE_1354), (30, True))

    def test_a_charge_moving_the_baseline_is_saved(self):
        g = LOGIC.SinceChargeCounterGuard()
        g.process("t", PRE_CHARGE)
        _, _, persist = g.process("t", CHARGE_END)
        self.assertTrue(persist)

    def test_a_tenth_of_a_km_drift_is_not_saved_every_poll(self):
        g = LOGIC.SinceChargeCounterGuard()
        g.process("t", PRE_CHARGE)
        _, _, persist = g.process("t", _h(3251, 270, 61170, 100, 68.0))  # km +5.1, odo +5.0
        self.assertFalse(persist)


class OdometerInMileageCoordinatorTests(unittest.TestCase):
    def _coord(self, payloads):
        return CoordinatorIntegrationTests._coord(CoordinatorIntegrationTests(), payloads)

    def _cycle(self, c):
        return CoordinatorIntegrationTests._cycle(CoordinatorIntegrationTests(), c)

    def test_payload_carries_the_worked_out_figure(self):
        c = self._coord([_payload(PRE_CHARGE), _payload(CHARGE_END), _payload(DRIVE_1354)])
        self._cycle(c)
        with self.assertLogs(COORD_MOD.LOGGER, logging.WARNING) as logs:
            self._cycle(c)
        self.assertTrue(any("reporting the odometer" in m for m in logs.output))
        self.assertEqual(self._cycle(c).mileageSinceLastCharge, 30)

    def test_no_baseline_clears_the_odometer_figure(self):
        c = self._coord([_payload(DRIVE_1354)])
        self.assertIsNone(self._cycle(c).mileageSinceLastCharge)


if __name__ == "__main__":
    unittest.main()
