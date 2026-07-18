"""Unit tests for config-entry setup failure handling."""

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
import unittest
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE_NAME = "mg_saic_setup_under_test"


class ConfigEntryNotReady(Exception):
    """Test replacement for Home Assistant's retryable setup exception."""


class LoginError(Exception):
    """Distinct backend error used to verify exception chaining."""


class FailingClient:
    def __init__(self):
        self.closed = False

    async def login(self):
        raise LoginError("temporary login failure")

    async def close(self):
        self.closed = True


def _module(name, **attributes):
    """Register a small module stub with the supplied attributes."""
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_integration_module(client):
    """Load mg_saic.__init__ with only its runtime imports stubbed."""
    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    _module(
        "homeassistant.config_entries",
        ConfigEntry=object,
        ConfigEntryNotReady=ConfigEntryNotReady,
    )
    _module("homeassistant.core", HomeAssistant=object)

    package = ModuleType(PACKAGE_NAME)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[PACKAGE_NAME] = package

    feature = SimpleNamespace(ALARM_MESSAGES="alarm_messages")
    _module(
        f"{PACKAGE_NAME}.backends",
        Feature=feature,
        backend_supports=lambda *_args: False,
        create_backend=lambda _data: client,
    )
    _module(
        f"{PACKAGE_NAME}.coordinator",
        SAICMGDataUpdateCoordinator=MagicMock(),
    )
    _module(
        f"{PACKAGE_NAME}.message_poller",
        SAICMGAccountPoller=MagicMock(),
    )
    _module(
        f"{PACKAGE_NAME}.const",
        DOMAIN="mg_saic",
        LOGGER=MagicMock(),
        PLATFORMS=(),
    )

    async def _noop(*_args, **_kwargs):
        return None

    _module(
        f"{PACKAGE_NAME}.services",
        async_setup_services=_noop,
        async_unload_services=_noop,
    )

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PKG_DIR / "__init__.py",
        submodule_search_locations=[str(PKG_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SetupLoginFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_login_is_closed_and_raised_as_retryable(self):
        client = FailingClient()
        integration = _load_integration_module(client)
        hass = SimpleNamespace(data={})
        entry = SimpleNamespace(
            entry_id="entry-1",
            data={
                "username": "test@example.invalid",
                "region": "India",
                "vin": "TESTVIN1234567890",
            },
        )

        with self.assertRaises(ConfigEntryNotReady) as context:
            await integration.async_setup_entry(hass, entry)

        self.assertIsInstance(context.exception.__cause__, LoginError)
        self.assertTrue(client.closed)
        account_key = (entry.data["username"], entry.data["region"])
        self.assertNotIn(
            account_key,
            hass.data["mg_saic"]["account_clients"],
        )


if __name__ == "__main__":
    unittest.main()
