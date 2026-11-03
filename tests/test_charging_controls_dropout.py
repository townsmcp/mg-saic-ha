"""The charging controls stay usable while the car isn't answering (#419).

@SteveMSJ's MG4, 10-11 Oct 2026: from 03:35 on the 10th to 01:01 on the
11th every charging request got return code 4 ("The remote control
instruction failed") while the vehicle status kept working, and the iSmart
app's charging page failed the same way. Target SOC, Charging Current Limit,
Scheduled Charging Mode / Start / End and the Charging switch all went
unavailable for those 21 hours, although the car still accepted a new
target from the app.

Now the settings controls show the last values the car gave and stay
usable; Charging and Battery Heating stay usable but read unknown, since
whether it is charging (or heating) right now isn't known.
"""

import sys
import types
import unittest
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import test_setup_and_config_flow  # noqa: F401 - loads the stubbed mg_saic package

PKG_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "mg_saic"
LOGIC = sys.modules["mg_saic.logic"]


def _stub(name, **attrs):
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        if not hasattr(module, key):
            setattr(module, key, value)
    sys.modules[name] = module
    return module


class _Entity:
    def __init__(self, *args, **kwargs):
        pass


class _CoordinatorEntity:
    def __init__(self, coordinator, *args, **kwargs):
        self.coordinator = coordinator


def _load_platforms():
    _stub("homeassistant.components.switch", SwitchEntity=_Entity)
    _stub("homeassistant.components.number", NumberEntity=_Entity,
          NumberMode=NS(SLIDER="slider", BOX="box"),
          NumberDeviceClass=NS(TEMPERATURE="temperature", BATTERY="battery"))
    _stub("homeassistant.components.select", SelectEntity=_Entity)
    _stub("homeassistant.components.time", TimeEntity=_Entity)
    _stub("homeassistant.components.climate",
          HVACMode=NS(OFF="off", COOL="cool", HEAT="heat", AUTO="auto",
                      FAN_ONLY="fan_only", DRY="dry", HEAT_COOL="heat_cool"))
    _stub("homeassistant.helpers.entity",
          EntityCategory=NS(CONFIG="config", DIAGNOSTIC="diagnostic"))
    _stub("homeassistant.const", PERCENTAGE="%",
          UnitOfTemperature=NS(CELSIUS="°C"))
    util = _stub("homeassistant.util")
    dt_util = _stub("homeassistant.util.dt", now=MagicMock(), utcnow=MagicMock())
    util.dt = dt_util
    uc = sys.modules["homeassistant.helpers.update_coordinator"]
    uc.CoordinatorEntity = _CoordinatorEntity
    vc = _stub("saic_ismart_client_ng.api.vehicle_charging",
               ScheduledChargingMode=MagicMock(), ChargeCurrentLimitCode=MagicMock(),
               TargetBatteryCode=MagicMock())
    _stub("saic_ismart_client_ng.api", vehicle_charging=vc)
    loaded = {}
    for name in ("switch", "number", "select", "time"):
        loaded[name] = test_setup_and_config_flow._load(
            f"mg_saic.{name}", PKG_DIR / f"{name}.py", force=True
        )
    return loaded


MODS = _load_platforms()


def _charging(sts=1, soc_cmd=5, heat=0):
    return NS(chrgMgmtData=NS(
        bmsChrgSts=sts,
        bmsOnBdChrgTrgtSOCDspCmd=soc_cmd,
        bmsPTCHeatResp=heat,
        bmsReserStHourDspCmd=1,
        bmsReserStMintueDspCmd=30,
        bmsReserSpHourDspCmd=5,
        bmsReserSpMintueDspCmd=30,
    ))


def _coordinator(charging, last=None):
    return NS(
        data={"status": NS(), "charging": charging},
        last_charging_data=last,
        last_update_success=True,
        scheduled_charging_pending_start=None,
        scheduled_charging_pending_end=None,
    )


def _entity(cls, coordinator):
    entity = cls.__new__(cls)
    entity.coordinator = coordinator
    entity._last_valid_value = None
    return entity


class HelperTests(unittest.TestCase):
    def test_latest_reply_first(self):
        latest, older = _charging(), _charging(sts=0)
        self.assertIs(LOGIC.charging_data_for_controls(_coordinator(latest, older)), latest)

    def test_falls_back_to_the_last_reply(self):
        older = _charging()
        self.assertIs(LOGIC.charging_data_for_controls(_coordinator(None, older)), older)

    def test_nothing_ever(self):
        self.assertIsNone(LOGIC.charging_data_for_controls(_coordinator(None, None)))
        self.assertIsNone(LOGIC.charging_data_for_controls(NS(data=None)))


class StevesOutageTests(unittest.TestCase):
    """The car answered at 03:00, then not for 21 hours."""

    def setUp(self):
        self.before = _charging(sts=1, soc_cmd=7)  # charging, target 100 %
        self.c = _coordinator(None, self.before)

    def test_target_soc_keeps_its_value_and_stays_usable(self):
        number = _entity(MODS["number"].SAICMGTargetSOCNumber, self.c)
        self.assertTrue(number.available)
        self.assertEqual(number.native_value, 100)

    def test_charging_stays_usable_but_unknown(self):
        switch = _entity(MODS["switch"].SAICMGChargingSwitch, self.c)
        self.assertTrue(switch.available)
        self.assertIsNone(switch.is_on)

    def test_battery_heating_stays_usable_but_unknown(self):
        switch = _entity(MODS["switch"].SAICMGBatteryHeatingSwitch, self.c)
        self.assertTrue(switch.available)
        self.assertIsNone(switch.is_on)

    def test_current_limit_and_schedule_stay_usable(self):
        for cls in (
            MODS["select"].SAICMGChargingCurrentSelect,
            MODS["select"].SAICMGScheduledChargingModeSelect,
        ):
            self.assertTrue(_entity(cls, self.c).available, cls.__name__)

    def test_schedule_times_show_the_last_values(self):
        cls = MODS["time"].SAICMGScheduledChargingTime
        start = _entity(cls, self.c)
        start._hour_attr, start._minute_attr = "bmsReserStHourDspCmd", "bmsReserStMintueDspCmd"
        start._pending_value = lambda: None
        start._default_value = dtime(0, 0)
        self.assertTrue(start.available)
        self.assertEqual(start.native_value, dtime(1, 30))

    def test_never_any_charging_data_is_still_unavailable(self):
        c = _coordinator(None, None)
        self.assertFalse(_entity(MODS["number"].SAICMGTargetSOCNumber, c).available)
        self.assertFalse(_entity(MODS["switch"].SAICMGChargingSwitch, c).available)

    def test_when_the_car_answers_again_it_shows_the_new_reply(self):
        self.c.data["charging"] = _charging(sts=0, soc_cmd=5)
        number = _entity(MODS["number"].SAICMGTargetSOCNumber, self.c)
        self.assertEqual(number.native_value, 80)
        switch = _entity(MODS["switch"].SAICMGChargingSwitch, self.c)
        self.assertFalse(switch.is_on)


class CoordinatorKeepsTheLastReplyTests(unittest.TestCase):
    def test_global_coordinator_retains_controls_through_dropout_and_recovers(self):
        coord_mod = sys.modules["mg_saic.coordinator"]
        cls = coord_mod.SAICMGDataUpdateCoordinator
        coordinator = cls(
            None, NS(),
            NS(entry_id="entry", data={"vin": "VIN", "vehicle_type": "BEV"}, options={}),
        )
        coordinator.hass = MagicMock()
        coordinator.last_update_success = True
        # Home Assistant event/storage boundaries only: the coordinator and
        # its state, trip and charging logic execute unchanged.
        coordinator.async_update_listeners = MagicMock()
        coordinator._schedule_trip_save = MagicMock()
        status = NS(basicVehicleStatus=NS(powerMode=0))
        number = _entity(MODS["number"].SAICMGTargetSOCNumber, coordinator)
        charging_switch = _entity(MODS["switch"].SAICMGChargingSwitch, coordinator)
        heating_switch = _entity(MODS["switch"].SAICMGBatteryHeatingSwitch, coordinator)

        def update(charging):
            coordinator.data = {"status": status, "charging": charging}
            coordinator._update_state(coordinator.data)

        before = _charging(sts=1, soc_cmd=7, heat=1)
        update(before)
        self.assertIs(coordinator.last_charging_data, before)
        self.assertEqual(number.native_value, 100)
        self.assertTrue(charging_switch.is_on)
        self.assertTrue(heating_switch.is_on)

        update(None)
        self.assertIs(coordinator.last_charging_data, before)
        self.assertTrue(number.available)
        self.assertEqual(number.native_value, 100)
        for switch in (charging_switch, heating_switch):
            self.assertTrue(switch.available)
            self.assertIsNone(switch.is_on)
        for entity_type in (
            MODS["select"].SAICMGChargingCurrentSelect,
            MODS["select"].SAICMGScheduledChargingModeSelect,
        ):
            self.assertTrue(_entity(entity_type, coordinator).available)

        recovered = _charging(sts=0, soc_cmd=5, heat=0)
        update(recovered)
        self.assertIs(coordinator.last_charging_data, recovered)
        self.assertEqual(number.native_value, 80)
        self.assertFalse(charging_switch.is_on)
        self.assertFalse(heating_switch.is_on)
        self.assertEqual(coordinator.async_update_listeners.call_count, 3)


class LogNoiseTests(unittest.TestCase):
    def test_no_error_per_sensor_per_poll(self):
        source = (PKG_DIR / "sensor.py").read_text()
        self.assertNotIn('LOGGER.error("No charging data available for %s"', source)


if __name__ == "__main__":
    unittest.main()
