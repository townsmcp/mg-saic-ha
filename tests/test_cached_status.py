"""Reading SAIC's stored copy of the car's status (diagnostic).

Every poll asks the car for a live reading, and the car wakes to answer. On
an MGS6 with a Bluetooth monitor on the 12 V battery, the 13 idle polls
between 00:42 and 06:45 on 6 Oct 2026 lined up with 13 dips of 0.3-0.5 V and
nothing in between. An MG4 owner saw the same, hourly (#407, @hoffeck), and
asked whether the car could be polled without waking it.

The iSmart app has a request the integration never used: on opening it calls
``/vehicle/status/cache`` once and gets a reply in about 0.2 s. The shape
below is that reply, from a capture of 3 Oct 2026 (position replaced).
``read_cached_status`` makes the same request so its effect can be measured.
"""

import asyncio
import hashlib
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

API = sys.modules["mg_saic.api"]
SERVICES = sys.modules["mg_saic.services"]
CACHED = sys.modules["mg_saic.cached_status"]
PKG = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"

# The app's reply, 3 Oct 2026 08:43:48 UTC. Taken 91 seconds earlier.
APP_REPLY = {
    "basicVehicleStatus": {
        "fuelLevelPrc": 0,
        "clstrDspdFuelLvlSgmt": 7,
        "frontLeftTyrePressure": 66,
        "remoteClimateStatus": 0,
        "rearRightTyrePressure": 66,
        "rmtHtdRrWndSt": 0,
        "frontRightTyrePressure": 66,
        "bonnetStatus": 0,
        "lockStatus": 1,
        "bootStatus": 0,
        "fuelRangeElec": 3500,
        "engineStatus": 0,
        "rearRightWindow": 0,
        "rearLeftTyrePressure": 66,
        "extendedData2": 0,
        "passengerWindow": 0,
        "extendedData1": 72,
        "rearLeftWindow": 0,
        "mileage": 51760,
        "elecRangeStdA": 768,
    },
    "gpsPosition": {
        "wayPoint": {"position": {"altitude": 47, "latitude": 1, "longitude": 2}}
    },
    "statusTime": 1791016937,
    "onlineStatus": 1,
}
ASKED_AT = datetime.fromtimestamp(1791017028, timezone.utc)
VIN = "LSJWH4098PN000001"


def _decoded(data=None):
    return CACHED.CachedVehicleStatus(**(APP_REPLY if data is None else data))


class ReplyShapeTests(unittest.TestCase):
    def test_the_library_can_decode_it(self):
        # The library builds the reply with dacite; fields it does not know
        # (none here, but SAIC adds them) must not make that fail.
        try:
            import dacite
        except ImportError:
            self.skipTest("dacite is not installed")
        reply = dacite.from_dict(
            CACHED.CachedVehicleStatus, {**APP_REPLY, "somethingNew": 5}
        )
        self.assertEqual(reply.statusTime, 1791016937)
        self.assertEqual(reply.onlineStatus, 1)
        self.assertEqual(reply.basicVehicleStatus["extendedData1"], 72)

    def test_an_empty_reply_decodes_too(self):
        try:
            import dacite
        except ImportError:
            self.skipTest("dacite is not installed")
        reply = dacite.from_dict(CACHED.CachedVehicleStatus, {})
        self.assertIsNone(reply.basicVehicleStatus)
        self.assertIsNone(reply.statusTime)


class SummaryTests(unittest.TestCase):
    def test_how_old_the_stored_status_is(self):
        summary = CACHED.summarise_cached_status(_decoded(), ASKED_AT)
        self.assertEqual(summary["status_time"], "2026-10-03T08:42:17+00:00")
        self.assertEqual(summary["age_seconds"], 91)
        self.assertEqual(summary["online_status"], 1)

    def test_the_fields_come_back_as_sent(self):
        summary = CACHED.summarise_cached_status(_decoded(), ASKED_AT)
        self.assertEqual(summary["fields"], APP_REPLY["basicVehicleStatus"])
        self.assertEqual(len(summary["fields"]), 20)

    def test_the_position_is_left_out(self):
        summary = CACHED.summarise_cached_status(_decoded(), ASKED_AT)
        self.assertTrue(summary["has_position"])
        text = json.dumps(summary)
        for word in ("latitude", "longitude", "wayPoint", "gpsPosition"):
            self.assertNotIn(word, text)

    def test_a_reply_with_nothing_in_it(self):
        summary = CACHED.summarise_cached_status(_decoded({}), ASKED_AT)
        self.assertEqual(
            summary,
            {
                "online_status": None,
                "status_time": None,
                "age_seconds": None,
                "has_position": False,
                "fields": {},
            },
        )

    def test_a_status_time_that_is_not_a_time(self):
        for bad in (0, -128, None, "1791016937", True):
            summary = CACHED.summarise_cached_status(
                _decoded({"statusTime": bad}), ASKED_AT
            )
            self.assertIsNone(summary["status_time"], bad)
            self.assertIsNone(summary["age_seconds"], bad)


class ApiTests(unittest.TestCase):
    def _client(self):
        client = API.SAICMGAPIClient.__new__(API.SAICMGAPIClient)
        client.vin = VIN
        client._ensure_initialized = AsyncMock()
        client.saic_api = MagicMock()
        client.saic_api.execute_api_call = AsyncMock(return_value="reply")
        return client

    def _read(self, client, *args):
        crypto = types.ModuleType("saic_ismart_client_ng.crypto_utils")
        crypto.sha256_hex_digest = lambda text: hashlib.sha256(
            text.encode()
        ).hexdigest()
        with patch.dict(sys.modules, {"saic_ismart_client_ng.crypto_utils": crypto}):
            return asyncio.run(client.get_cached_vehicle_status(*args))

    def test_it_makes_the_request_the_app_makes(self):
        client = self._client()
        self.assertEqual(self._read(client), "reply")
        call = client.saic_api.execute_api_call.call_args
        self.assertEqual(call.args, ("GET", "/vehicle/status/cache"))
        self.assertEqual(
            call.kwargs["params"],
            {
                "vin": hashlib.sha256(VIN.encode()).hexdigest(),
                "vehStatusReqType": "2",
            },
        )
        self.assertIs(call.kwargs["out_type"], CACHED.CachedVehicleStatus)

    def test_it_is_a_plain_request_not_an_ask_the_car_one(self):
        # The live status goes through execute_api_call_with_event_id, which
        # is the call that makes the car answer. This must never use it.
        client = self._client()
        self._read(client)
        client.saic_api.execute_api_call_with_event_id.assert_not_called()
        client.saic_api.get_vehicle_status.assert_not_called()

    def test_another_car_on_the_same_account(self):
        client = self._client()
        self._read(client, "LSJWH4098PN000002")
        self.assertEqual(
            client.saic_api.execute_api_call.call_args.kwargs["params"]["vin"],
            hashlib.sha256(b"LSJWH4098PN000002").hexdigest(),
        )


class ActionTests(unittest.TestCase):
    def _handler(self, client):
        registered = {}

        def register(domain, name, handler, **kwargs):
            registered[name] = (handler, kwargs)

        coordinator = MagicMock()
        hass = NS(
            services=NS(async_register=register),
            data={
                "mg_saic": {
                    "clients_by_vin": {VIN: client},
                    "coordinators_by_vin": {VIN: coordinator},
                }
            },
        )
        asyncio.run(SERVICES.async_setup_services(hass))
        handler, kwargs = registered["read_cached_status"]
        return handler, kwargs, coordinator

    def _call(self, client, vin=VIN):
        handler, _kwargs, coordinator = self._handler(client)
        return asyncio.run(handler(NS(data={"vin": vin}))), coordinator

    def test_it_returns_the_stored_status(self):
        client = NS(get_cached_vehicle_status=AsyncMock(return_value=_decoded()))
        result, _ = self._call(client)
        self.assertEqual(result["online_status"], 1)
        self.assertEqual(result["status_time"], "2026-10-03T08:42:17+00:00")
        self.assertEqual(result["fields"]["extendedData1"], 72)
        self.assertNotIn("gpsPosition", json.dumps(result))
        client.get_cached_vehicle_status.assert_awaited_once_with(VIN)

    def test_it_only_returns_a_response(self):
        client = NS(get_cached_vehicle_status=AsyncMock(return_value=_decoded()))
        _handler, kwargs, _ = self._handler(client)
        self.assertEqual(kwargs["supports_response"], "only")

    def test_it_refreshes_nothing(self):
        # Diagnostic: it must not poll the car or touch any entity.
        client = NS(get_cached_vehicle_status=AsyncMock(return_value=_decoded()))
        _result, coordinator = self._call(client)
        coordinator.async_request_refresh.assert_not_called()
        coordinator.async_refresh.assert_not_called()
        coordinator.schedule_action_refresh.assert_not_called()
        coordinator.async_set_updated_data.assert_not_called()

    def test_with_one_car_the_vin_can_be_left_out(self):
        client = NS(get_cached_vehicle_status=AsyncMock(return_value=_decoded()))
        handler, kwargs, _ = self._handler(client)
        result = asyncio.run(handler(NS(data={})))
        self.assertEqual(result["online_status"], 1)
        client.get_cached_vehicle_status.assert_awaited_once_with(VIN)
        # ...and the schema lets it be left out (voluptuous is stubbed here,
        # so check what is registered).
        self.assertIs(kwargs["schema"], SERVICES.SERVICE_OPTIONAL_VIN_SCHEMA)
        source = (PKG / "services.py").read_text()
        self.assertIn('vol.Schema({vol.Optional("vin"): cv.string})', source)

    def test_with_two_cars_the_vin_is_needed(self):
        client = NS(get_cached_vehicle_status=AsyncMock(return_value=_decoded()))
        registered = {}

        def register(domain, name, handler, **kwargs):
            registered[name] = handler

        hass = NS(
            services=NS(async_register=register),
            data={
                "mg_saic": {
                    "clients_by_vin": {VIN: client, "LSJWH4098PN000002": client},
                    "coordinators_by_vin": {VIN: MagicMock(), "LSJWH4098PN000002": MagicMock()},
                }
            },
        )
        asyncio.run(SERVICES.async_setup_services(hass))
        result = asyncio.run(registered["read_cached_status"](NS(data={})))
        self.assertEqual(result, {"error": "More than one car is set up: give the VIN."})
        client.get_cached_vehicle_status.assert_not_called()

    def test_a_backend_without_it_says_so(self):
        result, _ = self._call(NS())
        self.assertEqual(result, {"error": "Not available for this region."})

    def test_an_unknown_vin_says_so(self):
        client = NS(get_cached_vehicle_status=AsyncMock())
        result, _ = self._call(client, vin="NOT-A-CAR")
        self.assertEqual(result, {"error": "No vehicle with that VIN is set up."})
        client.get_cached_vehicle_status.assert_not_called()

    def test_a_failed_read_is_reported_not_raised(self):
        client = NS(
            get_cached_vehicle_status=AsyncMock(side_effect=RuntimeError("code 4"))
        )
        result, coordinator = self._call(client)
        self.assertEqual(result, {"error": "code 4"})
        # ...and is not counted against the car's reachability.
        coordinator.note_command_error.assert_not_called()


class DefinitionTests(unittest.TestCase):
    def test_described_for_the_actions_list(self):
        text = (PKG / "services.yaml").read_text()
        self.assertIn("read_cached_status:", text)

    def test_translated_in_every_language(self):
        for path in sorted((PKG / "translations").glob("*.json")):
            entry = json.loads(path.read_text())["services"].get("read_cached_status")
            self.assertIsNotNone(entry, path.name)
            self.assertTrue(entry["name"], path.name)
            self.assertTrue(entry["description"], path.name)
            self.assertIn("vin", entry["fields"], path.name)

    def test_removed_when_the_integration_unloads(self):
        text = (PKG / "services.py").read_text()
        self.assertIn(
            "hass.services.async_remove(DOMAIN, SERVICE_READ_CACHED_STATUS)", text
        )


if __name__ == "__main__":
    unittest.main()
