"""Open Boot / Close Boot with a power tailgate (Has Power Tailgate).

James's MG S6 EV (MIS3E), iSmart app captured with mitmproxy, 7 Nov 2026:
- open:  rvcReqType 9, paramId 31 = 1 -> accepted, tailgate opened, bootStatus 1
- close: rvcReqType 9, paramId 31 = 2 -> accepted, tailgate closed, bootStatus 0
mg-saic-client 0.9.8 sends it as control_tailgate. Without the option, Open
Boot still sends open_tailgate (unlock of lock 2), which releases the latch.
"""

import asyncio
import enum
import re
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package
from test_charging_controls_dropout import PKG_DIR, _Entity, _stub

LOGIC = sys.modules["mg_saic.logic"]
API = sys.modules["mg_saic.api"]


def _load_button():
    _stub("homeassistant.components.button", ButtonEntity=_Entity)
    return test_setup_and_config_flow._load(
        "mg_saic.button", PKG_DIR / "button.py", force=True
    )


BUTTON = _load_button()


def _coordinator(has_power_tailgate=True):
    c = MagicMock()
    c.has_power_tailgate = has_power_tailgate
    c.backend_supports = MagicMock(return_value=True)
    c.after_action_delay = 10
    c.tailgate_long_interval = 60
    c.schedule_action_refresh = AsyncMock()
    c.record_command_error = MagicMock()
    return c


def _button(cls, coordinator):
    entity = cls.__new__(cls)
    entity.coordinator = coordinator
    entity._client = NS(
        open_tailgate=AsyncMock(), control_power_tailgate=AsyncMock()
    )
    entity._vin = "VIN1"
    return entity


class _TailgateAction(enum.Enum):
    OPEN = 1
    CLOSE = 2


def _library_file(*parts):
    """A file of the installed mg-saic-client (other tests stub its modules)."""
    for entry in sys.path:
        path = Path(entry or ".", "saic_ismart_client_ng", *parts)
        if path.is_file():
            return path.read_text()
    raise FileNotFoundError("mg-saic-client isn't installed")


class LibraryCallTests(unittest.TestCase):
    def setUp(self):
        locks = types.ModuleType("saic_ismart_client_ng.api.vehicle.locks")
        locks.TailgateAction = _TailgateAction
        self._saved = {
            n: sys.modules.get(n)
            for n in (
                "saic_ismart_client_ng.api",
                "saic_ismart_client_ng.api.vehicle",
                "saic_ismart_client_ng.api.vehicle.locks",
            )
        }
        api = types.ModuleType("saic_ismart_client_ng.api")
        api.__path__ = []
        vehicle = types.ModuleType("saic_ismart_client_ng.api.vehicle")
        vehicle.__path__ = []
        sys.modules["saic_ismart_client_ng.api"] = api
        sys.modules["saic_ismart_client_ng.api.vehicle"] = vehicle
        sys.modules["saic_ismart_client_ng.api.vehicle.locks"] = locks

    def tearDown(self):
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_sends_the_library_tailgate_command(self):
        TailgateAction = _TailgateAction
        client = API.SAICMGAPIClient.__new__(API.SAICMGAPIClient)
        client.saic_api = NS(control_tailgate=object())
        client._make_api_call = AsyncMock()
        asyncio.run(client.control_power_tailgate("VIN1", True))
        client._make_api_call.assert_awaited_with(
            client.saic_api.control_tailgate, "VIN1", action=TailgateAction.OPEN
        )
        asyncio.run(client.control_power_tailgate("VIN1", False))
        client._make_api_call.assert_awaited_with(
            client.saic_api.control_tailgate, "VIN1", action=TailgateAction.CLOSE
        )

    def test_library_values_match_the_capture(self):
        # mg-saic-client 0.9.8+: request type 9, parameter 31, 1 open / 2 close.
        schema = _library_file("api", "vehicle", "schema.py")
        self.assertRegex(schema, r'\n    TAILGATE = "9"\n')
        self.assertRegex(schema, r"\n    TAILGATE = 31\n")
        locks = _library_file("api", "vehicle", "locks", "schema.py")
        self.assertTrue(re.search(r"OPEN = 1\s+CLOSE = 2", locks))


class OpenBootTests(unittest.TestCase):
    def test_with_power_tailgate_opens_it(self):
        entity = _button(BUTTON.SAICMGOpenBootButton, _coordinator())
        asyncio.run(entity.async_press())
        entity._client.control_power_tailgate.assert_awaited_once_with("VIN1", True)
        entity._client.open_tailgate.assert_not_awaited()
        entity.coordinator.schedule_action_refresh.assert_awaited_once_with("VIN1", 10, 60)

    def test_without_it_releases_the_latch_as_before(self):
        entity = _button(BUTTON.SAICMGOpenBootButton, _coordinator(has_power_tailgate=False))
        asyncio.run(entity.async_press())
        entity._client.open_tailgate.assert_awaited_once_with("VIN1")
        entity._client.control_power_tailgate.assert_not_awaited()

    def test_same_entity_either_way(self):
        source = (PKG_DIR / "button.py").read_text()
        self.assertIn('_open_boot_button"', source)


class CloseBootTests(unittest.TestCase):
    def test_closes_it(self):
        entity = _button(BUTTON.SAICMGCloseBootButton, _coordinator())
        asyncio.run(entity.async_press())
        entity._client.control_power_tailgate.assert_awaited_once_with("VIN1", False)
        entity.coordinator.schedule_action_refresh.assert_awaited_once_with("VIN1", 10, 60)

    def test_failure_recorded_not_raised(self):
        entity = _button(BUTTON.SAICMGCloseBootButton, _coordinator())
        error = RuntimeError("return code: 4, message: The remote control instruction failed")
        entity._client.control_power_tailgate.side_effect = error
        asyncio.run(entity.async_press())  # must not raise
        entity.coordinator.record_command_error.assert_called_once_with(
            "Error closing boot", error
        )
        entity.coordinator.schedule_action_refresh.assert_not_awaited()


class OptionTests(unittest.TestCase):
    def test_option_off(self):
        self.assertFalse(LOGIC.power_tailgate_enabled(_coordinator(has_power_tailgate=False)))

    def test_option_on(self):
        self.assertTrue(LOGIC.power_tailgate_enabled(_coordinator()))

    def test_not_on_a_backend_without_it(self):
        c = _coordinator()
        c.backend_supports.return_value = False
        self.assertFalse(LOGIC.power_tailgate_enabled(c))

    def test_close_boot_only_added_with_the_option(self):
        source = (PKG_DIR / "button.py").read_text()
        self.assertIn("if power_tailgate_enabled(coordinator):", source)
        self.assertIn("SAICMGCloseBootButton(coordinator", source)

    def test_option_change_reloads_entities(self):
        self.assertIn('"has_power_tailgate"', (PKG_DIR / "__init__.py").read_text())


if __name__ == "__main__":
    unittest.main()
