"""ABRP dispatch through the real coordinator and sender; only HTTP is fake.

Run alone: python -m unittest test_india_abrp_dispatch.
"""

import json
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

import test_setup_and_config_flow as harness


COORDINATOR = sys.modules["mg_saic.coordinator"]
ABRP = harness._load("mg_saic.abrp", harness.PKG_DIR / "abrp.py")


class _Http:
    def __init__(self, statuses=()):
        self.statuses = list(statuses)
        self.requests = []

    def post(self, url, *, params, headers, timeout):
        self.requests.append({
            "url": url,
            "token": params["token"],
            "payload": json.loads(params["tlm"]),
            "headers": headers,
            "timeout": timeout.total,
        })
        status = self.statuses.pop(0) if self.statuses else 200

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

            async def text(self):
                return "ok" if status == 200 else "offline"

        response = Response()
        response.status = status
        return response


def _coordinator(*, india=False, vin="SYNTHETIC-VIN"):
    coordinator = COORDINATOR.SAICMGDataUpdateCoordinator.__new__(
        COORDINATOR.SAICMGDataUpdateCoordinator
    )
    coordinator.client = NS(_india_backend=india)
    coordinator.vin = vin
    coordinator.hass = NS()
    coordinator.config_entry = NS(options={
        COORDINATOR.CONF_ABRP_USER_TOKEN: "synthetic-token",
        COORDINATOR.CONF_ABRP_API_KEY: "synthetic-key",
    })
    return coordinator


def _gps(stamp):
    return NS(
        measurement_time=stamp,
        gps_status_decoded=2,
        wayPoint=NS(
            speed=100,
            heading=90,
            position=NS(latitude=12000000, longitude=77000000, altitude=100),
        ),
    )


def _india_data():
    return {
        "status": NS(
            india=True,
            statusTime=200,
            basicVehicleStatus=NS(
                india=True, measurement_time=200,
                battery_soc_available=True, extendedData1=60,
            ),
            gpsPosition=_gps(200),
        ),
        "charging": NS(
            india=True,
            chrgMgmtData=NS(measurement_time=100, bmsPackSOCDsp=500),
            rvsChargeStatus=NS(measurement_time=100),
        ),
    }


def _global_data():
    return {
        "status": NS(
            statusTime=1000,
            basicVehicleStatus=NS(
                is_parked=False, exteriorTemperature=20,
                mileage=123456, fuelRangeElec=2000,
            ),
            gpsPosition=_gps(1000),
        ),
        "charging": NS(
            chrgMgmtData=NS(
                bmsPackSOCDsp=550, bmsPackCrntV=0, bmsPackCrnt=20040,
                decoded_current=-2, decoded_power=-0.8, decoded_voltage=400,
            ),
            rvsChargeStatus=NS(chargingGunState=1, fuelRangeElec=2500),
        ),
    }


class AbrpDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_india_retries_only_failed_source_across_coordinator_cycles(self):
        coordinator = _coordinator(india=True)
        data = _india_data()
        http = _Http([503, 200, 200])
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            # The ordinary status can be cached while another India source advances.
            await coordinator._maybe_send_abrp(data, fresh_status=False)
            self.assertEqual(
                [request["payload"]["utc"] for request in http.requests],
                [100, 200, 200],
            )
            self.assertEqual(coordinator._india_abrp_watermarks, {"ordinary": 200, "gps": 200})

            # A fresh AbrpApi instance is constructed each cycle, but the
            # coordinator retains successful groups and retries only charge.
            await coordinator._maybe_send_abrp(data, fresh_status=False)
            self.assertEqual(len(http.requests), 4)
            self.assertEqual(http.requests[-1]["payload"], {"utc": 100, "soc": 50.0})
            self.assertEqual(
                coordinator._india_abrp_watermarks,
                {"ordinary": 200, "gps": 200, "charge": 100},
            )
            await coordinator._maybe_send_abrp(data, fresh_status=False)
            self.assertEqual(len(http.requests), 4)

            data["charging"].chrgMgmtData.measurement_time = 300
            data["charging"].chrgMgmtData.bmsPackSOCDsp = 480
            data["charging"].rvsChargeStatus.measurement_time = 300
            await coordinator._maybe_send_abrp(data, fresh_status=False)
            self.assertEqual(len(http.requests), 5)
            self.assertEqual(http.requests[-1]["payload"], {"utc": 300, "soc": 48.0})

    async def test_india_position_keeps_its_clock_without_matching_soc(self):
        for offset in (-1, 2, 5):
            with self.subTest(offset=offset):
                coordinator = _coordinator(india=True)
                data = _india_data()
                gps = data["status"].gpsPosition
                gps.measurement_time = 200 + offset
                http = _Http()
                with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
                    await coordinator._maybe_send_abrp(data, fresh_status=False)
                    positions = [r["payload"] for r in http.requests if "lat" in r["payload"]]
                    self.assertEqual(len(positions), 1)
                    self.assertEqual(positions[0]["utc"], 200 + offset)
                    self.assertNotIn("soc", positions[0])
                    self.assertEqual(positions[0]["lat"], 12.0)
                    self.assertEqual(positions[0]["lon"], 77.0)

                    # Replays stay suppressed, but a fresh GPS-only reading
                    # can advance without a newer battery/status reading.
                    await coordinator._maybe_send_abrp(data, fresh_status=False)
                    self.assertEqual(len(http.requests), 3)
                    gps.measurement_time += 1
                    await coordinator._maybe_send_abrp(data, fresh_status=False)
                    self.assertEqual(len(http.requests), 4)
                    self.assertEqual(http.requests[-1]["payload"]["utc"], 201 + offset)
                    self.assertEqual(coordinator._india_abrp_watermarks["gps"], 201 + offset)

    async def test_india_groups_are_sent_in_time_order_within_each_poll(self):
        coordinator = _coordinator(india=True)
        data = _india_data()
        data["status"].gpsPosition.measurement_time = 150
        http = _Http()
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            await coordinator._maybe_send_abrp(data, fresh_status=False)
        self.assertEqual([r["payload"]["utc"] for r in http.requests], [100, 150, 200])
        self.assertEqual(coordinator._india_abrp_watermarks, {"charge": 100, "gps": 150, "ordinary": 200})

    async def test_india_watermarks_belong_to_each_vehicle(self):
        first = _coordinator(india=True, vin="SYNTHETIC-A")
        second = _coordinator(india=True, vin="SYNTHETIC-B")
        http = _Http()
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            await first._maybe_send_abrp(_india_data(), fresh_status=False)
            await second._maybe_send_abrp(_india_data(), fresh_status=False)
        self.assertEqual(len(http.requests), 6)
        self.assertIsNot(first._india_abrp_watermarks, second._india_abrp_watermarks)

    async def test_global_freshness_and_combined_payload_remain_unchanged(self):
        coordinator = _coordinator()
        http = _Http()
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            await coordinator._maybe_send_abrp(_global_data(), fresh_status=False)
            self.assertEqual(http.requests, [])
            await coordinator._maybe_send_abrp(_global_data(), fresh_status=True)
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0], {
            "url": ABRP.ABRP_SEND_URL,
            "token": "synthetic-token",
            "headers": {"Authorization": "APIKEY synthetic-key"},
            "timeout": 10,
            "payload": {
                "utc": 1000, "soc": 55.0,
                "is_charging": True, "power": -0.8, "voltage": 400, "current": -2,
                "is_parked": False, "ext_temp": 20, "odometer": 12345.6,
                "est_battery_range": 250.0, "speed": 10.0, "heading": 90,
                "elevation": 100, "lat": 12.0, "lon": 77.0,
            },
        })
        self.assertFalse(hasattr(coordinator, "_india_abrp_watermarks"))

    async def test_global_position_only_payload_is_still_sent(self):
        coordinator = _coordinator()
        data = {"status": NS(statusTime=1000, gpsPosition=_gps(1000))}
        http = _Http()
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            await coordinator._maybe_send_abrp(data, fresh_status=True)
        self.assertEqual(len(http.requests), 1)
        self.assertNotIn("soc", http.requests[0]["payload"])
        self.assertEqual(http.requests[0]["payload"]["lat"], 12.0)

    async def test_generic_sender_does_not_select_region_from_observation_markers(self):
        # Region dispatch belongs to the coordinator; generic async_send must
        # retain its combined-payload contract, even for marker-bearing input.
        http = _Http()
        api = ABRP.AbrpApi(http, "synthetic-key", "synthetic-token")
        data = _india_data()
        self.assertEqual(await api.async_send(data["status"], data["charging"]), (True, "ok"))
        self.assertEqual(len(http.requests), 1)
        self.assertEqual(http.requests[0]["payload"]["soc"], 50.0)
        self.assertEqual(http.requests[0]["payload"]["utc"], 200)

    async def test_neither_region_sends_without_configured_user_token(self):
        http = _Http()
        with patch.object(COORDINATOR, "async_get_clientsession", return_value=http):
            for india in (False, True):
                coordinator = _coordinator(india=india)
                coordinator.config_entry.options[COORDINATOR.CONF_ABRP_USER_TOKEN] = ""
                await coordinator._maybe_send_abrp(_india_data() if india else _global_data(), True)
        self.assertEqual(http.requests, [])


if __name__ == "__main__":
    unittest.main()
