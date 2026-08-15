"""Regression coverage for MG India tyre pressure mapping.

mg-ismart-india-client reports tyre pressure in psi.  The shared sensor
multiplies whatever the backend maps by PRESSURE_TO_BAR (0.04), i.e. it
expects the EU/global convention of 4 kPa per unit, so the backend has to
rescale rather than pass psi straight through.
"""

import unittest
from types import SimpleNamespace

from test_india_soc import INDIA

PRESSURE_TO_BAR = 0.04
BAR_TO_PSI = 14.5037738


class IndiaTyrePressureTests(unittest.TestCase):
    @staticmethod
    def _basic(**client_fields):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        status = backend._map_status(
            SimpleNamespace(
                status_time=1_800_000_000,
                raw={"basicVehicleStatus": {}},
                **client_fields,
            )
        )
        return status.basicVehicleStatus

    def _psi(self, basic, field):
        value = getattr(basic, field)
        self.assertIsNotNone(value)
        return round(value * PRESSURE_TO_BAR * BAR_TO_PSI, 1)

    def test_client_psi_survives_the_round_trip_to_the_sensor(self):
        basic = self._basic(
            front_left_tyre_psi=33.8,
            front_right_tyre_psi=35.8,
            rear_left_tyre_psi=33.8,
            rear_right_tyre_psi=35.4,
            tyre_monitor_status=0,
        )

        self.assertEqual(self._psi(basic, "frontLeftTyrePressure"), 33.8)
        self.assertEqual(self._psi(basic, "frontRightTyrePressure"), 35.8)
        self.assertEqual(self._psi(basic, "rearLeftTyrePressure"), 33.8)
        self.assertEqual(self._psi(basic, "rearRightTyrePressure"), 35.4)

    def test_wheel_tyre_monitor_status_is_exposed(self):
        basic = self._basic(tyre_monitor_status=0)

        self.assertEqual(basic.wheelTyreMonitorStatus, 0)

    def test_unreported_tyres_map_to_none(self):
        basic = self._basic(
            front_left_tyre_psi=None,
            front_right_tyre_psi=0,
            tyre_monitor_status=None,
        )

        self.assertIsNone(basic.frontLeftTyrePressure)
        self.assertIsNone(basic.frontRightTyrePressure)
        self.assertIsNone(basic.rearLeftTyrePressure)
        self.assertIsNone(basic.wheelTyreMonitorStatus)


if __name__ == "__main__":
    unittest.main()
