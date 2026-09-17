# File: tests/test_india_client_contract.py
#
# Contract check between the India backend adapter and the *released*
# mg-ismart-india-client pinned in manifest.json.
#
# Every other India test drives the adapter through a hand-written fake
# client, so an attribute the adapter reads but the pinned release does not
# provide passes CI unnoticed.  That is exactly how the 0.1.8 pin survived
# next to accesses to charge_time_remaining_min and
# power_usage_since_last_charge_kwh, neither of which exists in that release:
# the fake supplied them, and a real charging response would have raised
# AttributeError on the first poll.
#
# So this file deliberately does NOT stub mg_ismart_india_client.  It builds a
# real ChargeStatus from whatever release is installed and runs the adapter
# over it, so a pin that lags the attributes the adapter needs fails loudly.
# The python-tests.yaml workflow installs the client straight from the
# manifest pin, so what CI checks is the pin itself.  Locally the client may
# be absent, in which case these tests skip.

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"

# find_spec looks at the filesystem, so a MagicMock another test module left
# in sys.modules cannot make an absent client look installed.
CLIENT_INSTALLED = importlib.util.find_spec("mg_ismart_india_client") is not None


def _stub(name):
    if name in sys.modules:
        return
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = MagicMock()


def _load(name, path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


if CLIENT_INSTALLED:
    for _name in (
        "saic_ismart_client_ng",
        "saic_ismart_client_ng.model",
        "saic_ismart_client_ng.api",
        "saic_ismart_client_ng.api.vehicle_charging",
        "voluptuous",
        "homeassistant",
        "homeassistant.helpers",
        "homeassistant.helpers.config_validation",
    ):
        _stub(_name)

    if "mg_saic" not in sys.modules:
        _pkg = types.ModuleType("mg_saic")
        _pkg.__path__ = [str(PKG_DIR)]
        sys.modules["mg_saic"] = _pkg

    _load("mg_saic.const", PKG_DIR / "const.py")
    _load("mg_saic.logic", PKG_DIR / "logic.py")
    _load("mg_saic.api", PKG_DIR / "api.py")
    _load("mg_saic.backends", PKG_DIR / "backends" / "__init__.py")
    sys.modules["mg_saic.backends"].__path__ = [str(PKG_DIR / "backends")]
    INDIA = _load("mg_saic.backends.india", PKG_DIR / "backends" / "india.py")

    from mg_ismart_india_client.models import ChargeStatus


# Field names the charging adapter reads off ChargeStatus. Listed here rather
# than scraped from the source so that adding an access without checking the
# pinned release provides it is a deliberate two-file change.
CHARGE_FIELDS_READ = (
    "is_charging",
    "is_plugged_in",
    "soc",
    "range_km",
    "charging_voltage",
    "charging_current",
    "battery_energy_kwh",
    "charge_time_elapsed_s",
    "charge_time_remaining_min",
    "odometer_km",
    "distance_since_last_charge_km",
    "power_usage_since_last_charge_kwh",
    "total_battery_capacity_kwh",
)


@unittest.skipUnless(
    CLIENT_INSTALLED, "mg-ismart-india-client is not installed in this environment"
)
class TestChargeStatusContract(unittest.TestCase):
    """The pinned client must supply every field the adapter reads."""

    def _charge(self, **overrides):
        """A ChargeStatus built from the real released model.

        Every field is passed by keyword, so a rename or removal in a future
        release fails here rather than silently in production.
        """
        values = dict(
            is_charging=True,
            is_plugged_in=True,
            charging_type=1,
            charging_electricity_phase=1,
            soc=62.5,
            range_km=85.2,
            charging_voltage=360.0,
            charging_current=15.6,
            battery_energy_kwh=31.75,
            working_voltage=None,
            working_current=None,
            charge_time_elapsed_s=90,
            start_time=None,
            end_time=None,
            charge_time_remaining_min=186,
            charging_pile_id=None,
            charging_pile_supplier=None,
            odometer_km=1234.5,
            distance_since_last_charge_km=45.6,
            power_usage_since_last_charge_kwh=4.0,
            mileage_of_day_raw=None,
            power_usage_of_day_raw=None,
            static_energy_consumption_raw=None,
            total_battery_capacity_kwh=None,
            last_charge_ending_power_kwh=None,
            fota_lowest_voltage_raw=None,
            status_time=1_750_000_000,
            _raw={},
        )
        values.update(overrides)
        return ChargeStatus(**values)

    def _map(self, charge):
        backend = INDIA.IndiaBackend("user", "password", vin="VIN1")
        backend._charge_status_by_vin["VIN1"] = charge
        return _run(backend.get_charging_info("VIN1"))

    def test_every_field_the_adapter_reads_exists_on_the_pinned_model(self):
        charge = self._charge()
        for name in CHARGE_FIELDS_READ:
            with self.subTest(field=name):
                self.assertTrue(
                    hasattr(charge, name),
                    f"{name} is missing from the pinned mg-ismart-india-client; "
                    "bump the pin in manifest.json",
                )

    def test_charging_frame_maps_off_a_real_charge_status(self):
        charging = self._map(self._charge())

        self.assertEqual(charging.chrgMgmtData.bmsPackSOCDsp, 625)
        self.assertEqual(charging.chrgMgmtData.chrgngRmnngTime, 186)
        self.assertEqual(charging.rvsChargeStatus.fuelRangeElec, 852)
        self.assertEqual(charging.rvsChargeStatus.mileage, 12345)
        self.assertEqual(charging.rvsChargeStatus.mileageSinceLastCharge, 456)
        # Real kWh, taken as-is rather than reconstructed.
        self.assertEqual(charging.rvsChargeStatus.packEnergyKwh, 31.75)
        self.assertEqual(charging.rvsChargeStatus.powerUsageSinceLastCharge, 40)
        self.assertEqual(charging.rvsChargeStatus.lastChargeEndingPower, 358)

    def test_optional_fields_absent_from_the_frame_map_to_none(self):
        # The ASN.1 marks these OPTIONAL, and the client resolves an absent or
        # sentinel value to None. The adapter must carry that through instead
        # of raising or inventing a figure.
        charging = self._map(
            self._charge(
                charge_time_remaining_min=None,
                power_usage_since_last_charge_kwh=None,
                distance_since_last_charge_km=None,
            )
        )

        self.assertIsNone(charging.chrgMgmtData.chrgngRmnngTime)
        self.assertIsNone(charging.rvsChargeStatus.powerUsageSinceLastCharge)
        self.assertIsNone(charging.rvsChargeStatus.lastChargeEndingPower)
        self.assertIsNone(charging.rvsChargeStatus.mileageSinceLastCharge)
        # Pack energy does not depend on any of them.
        self.assertEqual(charging.rvsChargeStatus.packEnergyKwh, 31.75)


if __name__ == "__main__":
    unittest.main()
