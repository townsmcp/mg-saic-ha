"""V2X: the Send V2X Command test action and Charging Gun's v2l_adapter (#484).

@Heiko-Schneider's MG4, 24-25 Nov 2026: V2L only runs with the ignition on,
the car reports Charging Status 11 / 13 / 14 with the adapter in (as an MGS6
does), and the charging gun as connected throughout. Stopping V2L from Home
Assistant is his use case. mg-saic-client has control_v2x (tboxV2XReq 1 start
/ 2 stop) and set_v2x_target_battery_soc (tboxV2XSpSOCReq), neither yet tried
on a car, so the action sends one at a time and shows the car's reply.
"""

import asyncio
import enum
import sys
import types
import unittest
from dataclasses import dataclass
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_command_error_classification import API, _client, _run
from test_data_refresh_sensor import BINARY

SERVICES = sys.modules["mg_saic.services"]
VIN = "LSJWH4092PN000001"


@dataclass
class _Reply:
    chrgngRmnngTime: int = 0
    rvcReqSts: str = "1"


class _Error(Exception):
    pass


class _Code(enum.Enum):
    P_40 = 1
    P_50 = 2
    P_60 = 3
    P_70 = 4
    P_80 = 5
    P_90 = 6
    P_100 = 7


def _schema_module():
    module = types.ModuleType("saic_ismart_client_ng.api.vehicle_charging.schema")
    module.TargetBatteryCode = _Code
    return module


class _Invalid(Exception):
    pass


class LimitRuleTests(unittest.TestCase):
    """The rule tying limit_pct to set_limit (the rest of the schema is
    voluptuous, which these tests stub)."""

    def _check(self, data):
        with patch.object(SERVICES, "vol", NS(Invalid=_Invalid)):
            return SERVICES._v2x_limit_needed(data)

    def test_stop_and_start_without_a_limit(self):
        self.assertEqual(self._check({"command": "stop"}), {"command": "stop"})
        self.assertEqual(self._check({"command": "start"}), {"command": "start"})

    def test_set_limit_needs_a_limit(self):
        with self.assertRaises(_Invalid):
            self._check({"command": "set_limit"})
        self.assertEqual(self._check({"command": "set_limit", "limit_pct": 50})["limit_pct"], 50)

    def test_a_limit_only_goes_with_set_limit(self):
        with self.assertRaises(_Invalid):
            self._check({"command": "stop", "limit_pct": 50})

    def test_the_choices(self):
        self.assertEqual(SERVICES.V2X_COMMANDS, ("stop", "start", "set_limit"))
        self.assertEqual(SERVICES.V2X_LIMIT_PCTS, (40, 50, 60, 70, 80, 90, 100))


class ActionTests(unittest.TestCase):
    def _setup(self, *, client=None, coordinators=None):
        registered = {}

        def register(domain, name, handler, **kwargs):
            registered[name] = (handler, kwargs)

        client = client or MagicMock(
            control_v2x=AsyncMock(return_value=_Reply()),
            set_v2x_limit=AsyncMock(return_value=_Reply()),
        )
        coordinator = MagicMock(after_action_delay=10, schedule_action_refresh=AsyncMock())
        hass = NS(
            services=NS(async_register=register),
            data={
                "mg_saic": {
                    "clients_by_vin": {VIN: client},
                    "coordinators_by_vin": coordinators or {VIN: coordinator},
                }
            },
            async_create_task=MagicMock(),
        )
        asyncio.run(SERVICES.async_setup_services(hass))
        handler, kwargs = registered["send_v2x_command"]
        return handler, kwargs, client, coordinator

    def _call(self, handler, **data):
        with patch.object(SERVICES, "_backend_allows", return_value=True):
            return asyncio.run(handler(NS(data=data)))

    def test_stop(self):
        handler, kwargs, client, coordinator = self._setup()
        result = self._call(handler, command="stop")
        client.control_v2x.assert_awaited_once_with(VIN, stop=True)
        self.assertEqual(result, {"command": "stop", "reply": {"chrgngRmnngTime": 0, "rvcReqSts": "1"}})
        coordinator.schedule_action_refresh.assert_awaited_once()
        self.assertEqual(kwargs["supports_response"], "optional")

    def test_start(self):
        handler, _, client, _ = self._setup()
        self._call(handler, command="start")
        client.control_v2x.assert_awaited_once_with(VIN, stop=False)

    def test_set_limit(self):
        handler, _, client, _ = self._setup()
        result = self._call(handler, command="set_limit", limit_pct=50)
        client.set_v2x_limit.assert_awaited_once_with(VIN, 50)
        self.assertEqual(result["limit_pct"], 50)

    def test_a_refusal_is_shown_and_recorded(self):
        client = MagicMock(control_v2x=AsyncMock(side_effect=Exception("return code: 8, rejected")))
        handler, _, _, coordinator = self._setup(client=client)
        with patch.object(SERVICES, "HomeAssistantError", _Error), patch.object(
            SERVICES, "_record_command_error"
        ) as record:
            with self.assertRaises(_Error) as raised:
                self._call(handler, command="stop")
        self.assertIn("rejected", str(raised.exception))
        record.assert_called_once()
        coordinator.schedule_action_refresh.assert_not_awaited()

    def test_not_on_a_backend_without_charging_control(self):
        handler, _, client, _ = self._setup()
        with patch.object(SERVICES, "HomeAssistantError", _Error), patch.object(
            SERVICES, "_backend_allows", return_value=False
        ):
            with self.assertRaises(_Error):
                asyncio.run(handler(NS(data={"command": "stop"})))
        client.control_v2x.assert_not_awaited()

    def test_two_cars_need_the_vin(self):
        other = MagicMock()
        handler, _, client, coordinator = self._setup(
            coordinators={VIN: MagicMock(), "OTHER": other}
        )
        with patch.object(SERVICES, "HomeAssistantError", _Error):
            with self.assertRaises(_Error):
                self._call(handler, command="stop")
        client.control_v2x.assert_not_awaited()


class ApiTests(unittest.TestCase):
    def test_stop_and_start_go_to_control_v2x(self):
        client = _client()
        client.saic_api.control_v2x = AsyncMock(return_value=_Reply())
        reply = _run(client.control_v2x(VIN, stop=True))
        client.saic_api.control_v2x.assert_awaited_once_with(VIN, stop_v2x=True)
        self.assertEqual(reply, _Reply())
        _run(client.control_v2x(VIN, stop=False))
        client.saic_api.control_v2x.assert_awaited_with(VIN, stop_v2x=False)

    def test_limit_is_coded_like_the_charge_target(self):
        client = _client()
        client.saic_api.set_v2x_target_battery_soc = AsyncMock(return_value=_Reply())
        with patch.dict(
            sys.modules, {"saic_ismart_client_ng.api.vehicle_charging.schema": _schema_module()}
        ):
            _run(client.set_v2x_limit(VIN, 50))
            client.saic_api.set_v2x_target_battery_soc.assert_awaited_once_with(VIN, _Code.P_50)
            with self.assertRaises(ValueError):
                _run(client.set_v2x_limit(VIN, 45))


def _gun(status, field="chargingGunState"):
    sensor = BINARY.SAICMGChargingBinarySensor.__new__(BINARY.SAICMGChargingBinarySensor)
    sensor._field = field
    sensor._data_type = "charging"
    mgmt = NS(bmsChrgSts=status)
    sensor.coordinator = NS(data={"charging": NS(chrgMgmtData=mgmt)})
    return sensor


class ChargingGunTests(unittest.TestCase):
    def test_v2l_statuses(self):
        for status in (11, 13, 14):
            self.assertEqual(_gun(status).extra_state_attributes, {"v2l_adapter": True}, status)

    def test_a_charger(self):
        for status in (0, 1, 2, 3, 5, 7, 8, 10, 12):
            self.assertEqual(_gun(status).extra_state_attributes, {"v2l_adapter": False}, status)

    def test_no_status_no_attribute(self):
        self.assertIsNone(_gun(None).extra_state_attributes)
        sensor = _gun(13)
        sensor.coordinator = NS(data={})
        self.assertIsNone(sensor.extra_state_attributes)

    def test_only_on_the_gun_sensor(self):
        self.assertIsNone(_gun(13, field="somethingElse").extra_state_attributes)


class DefinitionTests(unittest.TestCase):
    def test_described_translated_and_removed(self):
        import json
        from pathlib import Path

        pkg = Path(SERVICES.__file__).resolve().parent
        self.assertIn("send_v2x_command:", (pkg / "services.yaml").read_text())
        for path in sorted((pkg / "translations").glob("*.json")):
            entry = json.loads(path.read_text())["services"]["send_v2x_command"]
            self.assertEqual(set(entry["fields"]), {"vin", "command", "limit_pct"}, path.name)
        self.assertIn(
            "hass.services.async_remove(DOMAIN, SERVICE_SEND_V2X_COMMAND)",
            (pkg / "services.py").read_text(),
        )


if __name__ == "__main__":
    unittest.main()
