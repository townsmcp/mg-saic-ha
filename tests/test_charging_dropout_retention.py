"""Charging sensors must hold what they last displayed across a drop-out (#262).

When the charging endpoint fails (timeout, error, or retries exhausted), the
coordinator publishes charging=None and each charging sensor falls back to its
retained value. The explicit "not charging -> 0" reading used to be excluded
from retention, so on an unplugged or parked car a single drop-out replayed
the previous charge session's figures -- e.g. Charging Power jumping from 0 to
6 kW and back, or Instant Power showing the last driving figure -- which is
exactly the kind of spike @HarryFlatter spotted in the HA history panel.

Reuses the Home Assistant stub harness from test_erac_fallback (same pattern
as test_india_tyre_pressure importing test_india_soc).
"""

import unittest
from types import SimpleNamespace as NS

from test_erac_fallback import SENSOR


def _coordinator():
    return NS(
        data={"charging": None, "status": None},
        vin_info=NS(vin="VIN1", brandName="MG", modelName="Test"),
        last_update_success=True,
        charging_capacity_correction=None,
    )


ENTRY = NS(entry_id="entry1")


def _poll(coordinator, *, sts, crnt=20000, vol=1597):
    coordinator.data["charging"] = NS(
        chrgMgmtData=NS(bmsChrgSts=sts, bmsPackCrnt=crnt, bmsPackVol=vol)
    )


def _dropout(coordinator):
    coordinator.data["charging"] = None


class ChargingDropoutRetentionTests(unittest.TestCase):
    def _sensors(self, coordinator):
        return {
            "power": SENSOR.SAICMGChargingPowerSensor(
                coordinator, ENTRY, "Charging Power", None, None, None, None
            ),
            "current": SENSOR.SAICMGChargingCurrentSensor(
                coordinator, ENTRY, "Charging Current", "bmsPackCrnt",
                None, None, None, None, 0.05,
            ),
            "voltage": SENSOR.SAICMGChargingSensor(
                coordinator, ENTRY, "Charging Voltage", "bmsPackVol",
                None, None, None, None, 0.25,
            ),
        }

    def _read(self, sensors):
        return {name: s.native_value for name, s in sensors.items()}

    def test_unplugged_car_holds_zero_through_dropout(self):
        """The bug: 0 -> last session's figure -> 0 across one drop-out."""
        coordinator = _coordinator()
        sensors = self._sensors(coordinator)

        _poll(coordinator, sts=1, crnt=19700, vol=1600)  # charging
        charging = self._read(sensors)
        self.assertGreater(charging["power"], 0)
        self.assertGreater(charging["voltage"], 0)

        _poll(coordinator, sts=0)  # unplugged
        self.assertEqual(self._read(sensors), {"power": 0, "current": 0, "voltage": 0})

        _dropout(coordinator)
        self.assertEqual(
            self._read(sensors),
            {"power": 0, "current": 0, "voltage": 0},
            "a drop-out must hold the 0 last displayed, not replay the "
            "previous charge session",
        )

    def test_charging_car_still_holds_live_figures_through_dropout(self):
        """Retention's original purpose is unchanged: mid-charge drop-outs
        keep showing the last real charging figures."""
        coordinator = _coordinator()
        sensors = self._sensors(coordinator)

        _poll(coordinator, sts=1, crnt=19700, vol=1600)
        before = self._read(sensors)
        _dropout(coordinator)
        self.assertEqual(self._read(sensors), before)

    def test_connecting_status_is_held_too(self):
        coordinator = _coordinator()
        sensors = self._sensors(coordinator)
        # native_value is read after every coordinator update in HA, and
        # retention is updated on read -- so read after each poll here too.
        _poll(coordinator, sts=1, crnt=19700, vol=1600)
        self._read(sensors)
        _poll(coordinator, sts=5)  # connecting
        self._read(sensors)
        _dropout(coordinator)
        self.assertEqual(self._read(sensors), {"power": 0, "current": 0, "voltage": 0})

    def test_instant_power_does_not_replay_driving_figure_when_parked(self):
        coordinator = _coordinator()
        sensor = SENSOR.SAICMGInstantPowerSensor(
            coordinator, ENTRY, "Instant Power", None, None, None, None
        )
        coordinator.data["status"] = NS(basicVehicleStatus=NS(powerMode=2))
        _poll(coordinator, sts=0, crnt=20700, vol=1600)  # driving, discharging
        self.assertLess(sensor.native_value, 0)

        coordinator.data["status"] = NS(basicVehicleStatus=NS(powerMode=0))
        _poll(coordinator, sts=0)  # parked
        self.assertEqual(sensor.native_value, 0)

        _dropout(coordinator)
        self.assertEqual(sensor.native_value, 0)


if __name__ == "__main__":
    unittest.main()
