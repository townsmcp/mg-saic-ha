"""India-only, bounded provenance at Home Assistant sensor boundaries.

Run alone: python -m unittest test_india_sensor_provenance.
"""

import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from unittest.mock import patch

from test_india_soc import BACKENDS, INDIA, SENSOR


def _mileage_sensor(backend, vehicle_type, status):
    sensor = SENSOR.SAICMGMileageSensor.__new__(SENSOR.SAICMGMileageSensor)
    sensor.coordinator = NS(
        client=backend,
        backend_supports=lambda feature: BACKENDS.backend_supports(backend, feature),
        last_update_success=True,
        data={"status": status, "charging": None},
    )
    sensor._vehicle_type = vehicle_type
    sensor._last_valid_mileage = None
    sensor._status_type = "basicVehicleStatus"
    sensor._charging_status_type = "rvsChargeStatus"
    sensor._field = "mileage"
    sensor._factor = 0.1
    sensor._name = "Mileage"
    return sensor


def _session_sensor(session, *, india):
    sensor = SENSOR.SAICMGChargeSessionDurationSensor.__new__(
        SENSOR.SAICMGChargeSessionDurationSensor
    )
    sensor.coordinator = NS(
        client=NS(_india_backend=india),
        trip_stats=NS(charge_session=lambda: session),
    )
    return sensor


def _session(origin):
    return {
        "in_progress": False,
        "duration_s": 600,
        "paused_s": 10,
        "interruptions": 1,
        "duration_source": "api",
        "charge_start_ts": 100,
        "charge_end_ts": 700,
        "as_of": "2026-10-14T12:00:00+00:00",
        "india_provenance": origin,
    }


class IndiaSensorProvenanceTests(unittest.TestCase):
    def test_india_ordinary_mileage_available_without_charging(self):
        backend = INDIA.IndiaBackend("test", "test")
        stamp = datetime.now(timezone.utc).timestamp()
        status = backend._map_status(NS(status_time=stamp, odometer_km=1234.5))
        self.assertIn(BACKENDS.Feature.CHARGING_DATA, backend.supported_features)
        for vehicle_type in ("BEV", "PHEV"):
            with self.subTest(vehicle_type=vehicle_type):
                sensor = _mileage_sensor(backend, vehicle_type, status)
                # HA checks availability before requesting the first value.
                self.assertTrue(sensor.available)
                self.assertEqual(sensor.native_value, 1234.5)
                self.assertEqual(sensor.extra_state_attributes["measurement_time"], stamp)
                self.assertEqual(sensor.extra_state_attributes["india_provenance"]["source"], "ordinary")

    def test_india_mileage_retained_on_failed_poll_and_backwards_reading(self):
        backend = INDIA.IndiaBackend("test", "test")
        stamp = datetime.now(timezone.utc).timestamp()
        status = backend._map_status(NS(status_time=stamp, odometer_km=1234.5))
        sensor = _mileage_sensor(backend, "BEV", status)
        self.assertEqual(sensor.native_value, 1234.5)
        original = sensor.extra_state_attributes
        sensor.coordinator.last_update_success = False
        sensor.coordinator.data["status"] = None
        self.assertTrue(sensor.available)
        self.assertEqual(sensor.native_value, 1234.5)
        self.assertEqual(sensor.extra_state_attributes, original)
        sensor.coordinator.last_update_success = True
        sensor.coordinator.data["status"] = backend._map_status(
            NS(status_time=stamp, odometer_km=1234.4)
        )
        self.assertEqual(sensor.native_value, 1234.5)
        self.assertEqual(sensor.extra_state_attributes, original)

    def test_india_missing_or_invalid_mileage_stays_unknown(self):
        backend = INDIA.IndiaBackend("test", "test")
        for mileage in (None, 0, -1, 6553.5):
            with self.subTest(mileage=mileage):
                status = backend._map_status(NS(odometer_km=mileage))
                sensor = _mileage_sensor(backend, "BEV", status)
                self.assertIsNone(sensor.native_value)
                self.assertIsNone(sensor._last_valid_mileage)

    def test_global_bev_mileage_uses_valid_status_before_retention(self):
        for marker in (False, "true", None):
            with self.subTest(marker=marker):
                backend = NS(_india_backend=marker, supported_features=BACKENDS.GLOBAL_FEATURES)
                status = NS(basicVehicleStatus=NS(mileage=None))
                sensor = _mileage_sensor(backend, "BEV", status)
                self.assertFalse(sensor.available)
                status.basicVehicleStatus.mileage = 12345
                self.assertTrue(sensor.available)
                self.assertEqual(sensor.native_value, 1234.5)
                sensor.coordinator.last_update_success = False
                sensor.coordinator.data = {"status": None, "charging": None}
                self.assertTrue(sensor.available)
                self.assertEqual(sensor.native_value, 1234.5)

    def test_global_charge_attributes_keep_existing_schema_despite_retained_india_key(self):
        session = _session({"start": {"soc_pct": {"source": "charge", "measurement_time": 100}}})
        expected = {key: value for key, value in session.items() if key != "india_provenance"}
        for backend_marker in (False, "true", None):
            with self.subTest(backend_marker=backend_marker):
                sensor = _session_sensor(session, india=backend_marker)
                self.assertEqual(sensor.extra_state_attributes, expected)
                self.assertEqual(sensor.native_value, 10.0)

    def test_india_session_attributes_project_old_history_without_changing_values(self):
        origin = {
            "source": "charge", "field": "bmsPackSOCDsp", "measurement_time": 100,
            "factor": 0.1, "unused_field": "must not be exported",
        }
        # A legacy retained origin can refer back to itself; projection must
        # never traverse or export that ancestry.
        origin["inputs"] = {"raw": origin}
        provenance = {
            "start": {"soc_pct": origin},
            "end": {"soc_pct": origin},
            "trips": [origin] * 1000,
        }
        sensor = _session_sensor(_session(provenance), india=True)
        attrs = sensor.extra_state_attributes
        projected = attrs["india_provenance"]
        encoded = json.dumps(projected, ensure_ascii=False).encode("utf-8")
        self.assertLess(len(encoded), 6144)
        self.assertNotIn("trips", projected)
        self.assertNotIn("unused_field", encoded.decode())
        self.assertEqual(projected["schema_version"], 1)
        self.assertEqual(projected["start"]["soc_pct"]["measurement_time"], 100)
        self.assertEqual(projected["end"]["soc_pct"]["measurement_time"], 100)
        self.assertEqual(sensor.native_value, 10.0)
        projected["start"]["soc_pct"]["measurement_time"] = 999
        self.assertEqual(origin["measurement_time"], 100)

    def test_payload_marker_alone_does_not_enable_india_sensor_attributes(self):
        sensor = SENSOR.SAICMGMileageSensor.__new__(SENSOR.SAICMGMileageSensor)
        sensor.coordinator = NS(
            client=NS(_india_backend=False),
            data={"status": NS(basicVehicleStatus=NS(india=True))},
        )
        sensor._india_origin = {"source": "ordinary", "measurement_time": 100}
        sensor._india_measurement_time = 100
        with patch.object(SENSOR.SensorEntity, "extra_state_attributes", None, create=True):
            self.assertIsNone(sensor.extra_state_attributes)
            sensor.coordinator.client._india_backend = True
            self.assertEqual(sensor.extra_state_attributes["measurement_time"], 100)
            self.assertEqual(
                sensor.extra_state_attributes["india_provenance"]["source"], "ordinary"
            )


if __name__ == "__main__":
    unittest.main()
