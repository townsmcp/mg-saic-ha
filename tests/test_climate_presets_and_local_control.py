"""Climate entity: iSmart-aligned modes/presets and local-control handling (#336).

Covers three things that previously had no entity-level coverage at all:

  * status 6 (climate running under the driver's own local control) must
    report the climate as ON rather than Off. Reporting Off was a deliberate
    1.2.0 choice, revisited here -- see the note in hvac_mode.
  * a remote climate command issued while the driver has local control must
    not be sent, because the car rejects it with a generic "instruction
    failed" and the attempt still costs one of the limited remote commands.
  * the preset set offered per vehicle, which is gated on what each car can
    actually do rather than being the same list everywhere.
"""

import asyncio
import importlib.util
import json
import logging
import sys
import time
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_DIR = REPO_ROOT / "custom_components" / "mg_saic"
PACKAGE = "mg_saic_climate_test"


class _Values:
    def __getattr__(self, name):
        return name.lower()


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def async_write_ha_state(self):
        pass


class _ClimateEntityFeature(int):
    TARGET_TEMPERATURE = 1
    FAN_MODE = 8
    PRESET_MODE = 16
    TURN_ON = 256
    TURN_OFF = 128


class _HVACMode:
    OFF = "off"
    COOL = "cool"
    HEAT = "heat"
    FAN_ONLY = "fan_only"
    HEAT_COOL = "heat_cool"


def _module(name, **attributes):
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_climate():
    homeassistant = _module("homeassistant")
    homeassistant.__path__ = []
    components = _module("homeassistant.components")
    components.__path__ = []
    helpers = _module("homeassistant.helpers")
    helpers.__path__ = []

    class _ClimateEntity:
        _attr_supported_features = 0
        _attr_preset_modes = None
        _attr_preset_mode = None

        def async_write_ha_state(self):
            pass

    climate_mod = _module(
        "homeassistant.components.climate",
        ClimateEntity=_ClimateEntity,
        ClimateEntityFeature=_ClimateEntityFeature,
        HVACMode=_HVACMode,
    )
    climate_mod.__path__ = []
    _module(
        "homeassistant.components.climate.const",
        FAN_LOW="Low",
        FAN_MEDIUM="Medium",
        FAN_HIGH="High",
    )
    _module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
    )
    _module("homeassistant.helpers.entity", EntityCategory=_Values())
    _module(
        "homeassistant.const",
        UnitOfTemperature=_Values(),
        ATTR_TEMPERATURE="temperature",
    )

    package = _module(PACKAGE)
    package.__path__ = [str(PKG_DIR)]

    class _CommandsLimitReachedException(Exception):
        pass

    class _VehicleNotLockedException(Exception):
        pass

    _module(
        f"{PACKAGE}.api",
        CommandsLimitReachedException=_CommandsLimitReachedException,
        VehicleNotLockedException=_VehicleNotLockedException,
    )
    _module(
        f"{PACKAGE}.const",
        DOMAIN="mg_saic",
        LOGGER=logging.getLogger(PACKAGE),
        FRONT_DEFROST_TEMP_C=28,
        CLIMATE_STATUS_LOCAL_CONTROL=6,
        COMMAND_SYNC_GRACE_SECONDS=30,
    )
    _module(f"{PACKAGE}.utils", create_device_info=lambda *a: {})

    class _Feature:
        REAR_WINDOW_HEAT = "rear_window_heat"

    _module(
        f"{PACKAGE}.backends",
        Feature=_Feature,
        backend_supports=lambda client, feature: feature
        in getattr(client, "supported_features", {feature}),
    )
    return _load(f"{PACKAGE}.climate", PKG_DIR / "climate.py")


CLIMATE = _load_climate()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _Base(unittest.TestCase):
    def _entity(self, *, scheme="mode_select", status=0, heat={2}, defrost={5},
                rear_heat=True, max_cool=3, cool=2, cool_uses_start_ac=False,
                climate_mode_heat=4, requested_hvac_mode="off",
                climate_fan_auto=None):
        coordinator = SimpleNamespace(
            climate_control_scheme=scheme,
            climate_status_heat=heat,
            climate_status_cool={cool},
            climate_status_defrost=defrost,
            climate_status_fan_only={1},
            climate_mode_cool=cool,
            climate_mode_heat=climate_mode_heat,
            climate_mode_max_cool=max_cool,
            climate_mode_fan_only=1,
            climate_mode_defrost=5,
            max_cool_forces_min_temp=False,
            cool_uses_start_ac=cool_uses_start_ac,
            climate_fan_auto=climate_fan_auto,
            climate_fan_only_airflow=False,
            fan_speed_low=1,
            fan_speed_medium=2,
            fan_speed_high=3,
            heat_fan_speed=2,
            ac_long_interval=None,
            min_temp=16,
            max_temp=30,
            requested_target_temp=22.0,
            pre_preset_target_temp=None,
            temp_offset=3,
            temp_index_map=None,
            temp_idx_inverted=False,
            requested_hvac_mode=requested_hvac_mode,
            data={
                "status": SimpleNamespace(
                    basicVehicleStatus=SimpleNamespace(remoteClimateStatus=status)
                )
            },
            climate_entity=None,
        )
        coordinator.is_climate_under_local_control = MagicMock(return_value=status == 6)
        coordinator.notify_climate_local_control = AsyncMock()
        coordinator.notify_front_defrost_blocked = AsyncMock()
        coordinator.is_climate_blocking_defrost = MagicMock(return_value=False)
        coordinator.notify_command_limit_reached = AsyncMock()
        coordinator.notify_vehicle_not_locked = AsyncMock()
        coordinator.record_command_error = MagicMock()
        coordinator.schedule_action_refresh = MagicMock()
        coordinator.get_ac_temperature_idx = MagicMock(side_effect=lambda temp: int(temp) - 13)
        coordinator.async_update_listeners = MagicMock()

        client = MagicMock()
        client.supported_features = {"rear_window_heat"} if rear_heat else set()
        client.start_climate = AsyncMock()
        client.start_ac = AsyncMock()
        client.stop_ac = AsyncMock()
        client.control_rear_window_heat = AsyncMock()

        vin_info = SimpleNamespace(
            vin="VIN1", brandName="MG", modelName="Test", series="TEST"
        )
        entry = SimpleNamespace(entry_id="e1")
        entity = CLIMATE.SAICMGClimateEntity(
            coordinator, client, entry, vin_info, "VIN1"
        )
        entity.hass = MagicMock()
        return entity


class LocalControlTests(_Base):
    def test_reports_on_not_off_while_under_local_control(self):
        """The bug: the driver is running the heater and the entity said Off."""
        entity = self._entity(status=6)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)
        self.assertNotEqual(entity.hvac_mode, _HVACMode.OFF)

    def test_applies_to_every_scheme_not_just_one_model(self):
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                entity = self._entity(scheme=scheme, status=6)
                self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_command_is_not_sent_and_the_reason_is_explained(self):
        entity = self._entity(status=6)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        entity.coordinator.notify_climate_local_control.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()
        entity._client.start_ac.assert_not_awaited()

    def test_preset_is_not_sent_under_local_control_either(self):
        entity = self._entity(status=6)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        entity.coordinator.notify_climate_local_control.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()

    def test_turning_off_is_still_allowed(self):
        """Off is how a user regains remote control -- it must not be blocked."""
        entity = self._entity(status=6)
        _run(entity.async_set_hvac_mode(_HVACMode.OFF))
        entity._client.stop_ac.assert_awaited_once()
        entity.coordinator.notify_climate_local_control.assert_not_awaited()

    def test_normal_operation_is_unaffected(self):
        entity = self._entity(status=0)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        entity.coordinator.notify_climate_local_control.assert_not_awaited()


class PresetTests(_Base):
    def test_low_and_high_offered_on_a_car_that_heats(self):
        presets = self._entity()._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_LOW, presets)
        self.assertIn(CLIMATE.PRESET_HIGH, presets)

    def test_high_withheld_from_a_car_with_no_heat_capability(self):
        presets = self._entity(heat=set())._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_LOW, presets)
        self.assertNotIn(CLIMATE.PRESET_HIGH, presets)

    def test_windscreen_presets_are_gated_on_real_capability(self):
        both = self._entity()._attr_preset_modes
        self.assertIn(CLIMATE.PRESET_FRONT_WINDSCREEN, both)
        self.assertIn(CLIMATE.PRESET_REAR_WINDSCREEN, both)

        neither = self._entity(defrost=set(), rear_heat=False)._attr_preset_modes
        self.assertNotIn(CLIMATE.PRESET_FRONT_WINDSCREEN, neither)
        self.assertNotIn(CLIMATE.PRESET_REAR_WINDSCREEN, neither)

    def test_low_pins_the_setpoint_to_the_minimum(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(entity.coordinator.requested_target_temp, 16)

    def test_high_pins_the_setpoint_to_the_maximum(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 30)

    def test_rear_windscreen_uses_its_own_command_not_a_climate_mode(self):
        entity = self._entity()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_REAR_WINDSCREEN))
        entity._client.control_rear_window_heat.assert_awaited_once_with("VIN1", "start")
        entity._client.start_climate.assert_not_awaited()

    def test_presets_are_offered_on_every_scheme(self):
        """Previously only mode_select cars had presets at all."""
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                presets = self._entity(scheme=scheme)._attr_preset_modes
                self.assertIn(CLIMATE.PRESET_LOW, presets)


class AcOnModeTests(_Base):
    def test_ac_on_is_offered_on_every_scheme(self):
        for scheme in ("mode_select", "fan_speed"):
            with self.subTest(scheme=scheme):
                self.assertIn(_HVACMode.HEAT_COOL, self._entity(scheme=scheme)._attr_hvac_modes)

    def test_existing_modes_are_not_removed(self):
        """Additive by design: nobody's automations should break."""
        modes = self._entity()._attr_hvac_modes
        for expected in (_HVACMode.OFF, _HVACMode.COOL, _HVACMode.HEAT, _HVACMode.FAN_ONLY):
            self.assertIn(expected, modes)

    def test_ac_on_sends_a_command_at_the_current_setpoint(self):
        entity = self._entity()
        entity.coordinator.requested_target_temp = 24.0
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertTrue(
            entity._client.start_climate.await_count
            or entity._client.start_ac.await_count
        )
        self.assertEqual(entity.coordinator.requested_target_temp, 24.0)

    def test_ac_on_uses_the_real_fan_speed_on_classic_cars_not_climate_mode_cool(self):
        # The bug: AC On previously sent climate_mode_cool unconditionally,
        # regardless of scheme. climate_mode_cool is a mode_select-only
        # concept and means nothing on a classic fan_speed car -- sending it
        # as a fan_speed byte risked commanding the wrong thing entirely (on
        # the MG4/EH32 specifically, that value is the car's own confirmed
        # HEAT status, #173). climate_mode_cool is deliberately set to an
        # absurd, distinct value here so any accidental use of it is
        # unmistakable rather than coincidentally matching.
        entity = self._entity(scheme="fan_speed", cool=99, cool_uses_start_ac=False)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)  # fan_speed_medium, the real value
        self.assertNotEqual(kwargs["fan_speed"], 99)
        self.assertEqual(kwargs["ac_on"], True)

    def test_ac_on_uses_start_ac_on_simple_ac_cars_not_the_ignored_start_climate(self):
        # e.g. ZP22/MG3 Hybrid: start_climate is silently ignored by the car
        # (see its profile notes). Previously AC On sent it anyway, so
        # selecting AC On did nothing at all on this car. Also confirms AC
        # On does NOT pin the setpoint to min_temp the way an explicit Cool
        # request does -- it must follow whatever temperature is already set.
        entity = self._entity(scheme="fan_speed", cool_uses_start_ac=True)
        entity.coordinator.requested_target_temp = 24.0
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        entity._client.start_ac.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()
        self.assertEqual(entity.coordinator.requested_target_temp, 24.0)

    def test_ac_on_uses_the_real_fan_speed_on_climate_fan_auto_cars(self):
        # e.g. AS33P/HS PHEV: every command uses one fixed value, not
        # climate_mode_cool.
        entity = self._entity(scheme="fan_speed", cool=99, cool_uses_start_ac=False)
        entity.coordinator.climate_fan_auto = 2
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)  # climate_fan_auto, not 99

    def test_ac_on_still_uses_climate_mode_cool_on_mode_select(self):
        # Regression guard: climate_mode_cool IS the genuinely correct byte
        # on this scheme -- must not be "fixed" away by the change above.
        entity = self._entity(scheme="mode_select", cool=2)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)


class AmbiguousModeDisambiguationTests(_Base):
    """hvac_mode on mode_select cars sharing one byte for Cool/Heat (#380).

    AH4EM/MIS3E/EP21/P12L all set cool_uses_start_ac=True purely to flag
    that climate_mode_cool == climate_mode_heat (e.g. both 2): the car's
    remoteClimateStatus alone can't say whether that status means Cool or
    Heat, only which one was last actually requested can. Before this fix,
    resolving that used this ENTITY's own self._attr_hvac_mode -- which the
    same property's status==0 branch could reset moments earlier (a status
    still reading 0 right after a command was sent), leaving nothing
    reliable to fall back on. Fixed by disambiguating from
    coordinator.requested_hvac_mode instead, which only an explicit command
    ever sets, never a property read.
    """

    def _ambiguous_entity(self, *, requested_hvac_mode, status):
        # climate_mode_cool == climate_mode_heat == 2 is what makes this
        # ambiguous; climate_status_heat={2} mirrors the real profiles (it
        # only gates whether HEAT is offered at all -- see climate_mode_
        # from_status for the same convention at the coordinator level).
        return self._entity(
            scheme="mode_select",
            cool=2,
            climate_mode_heat=2,
            heat={2},
            cool_uses_start_ac=True,
            requested_hvac_mode=requested_hvac_mode,
            status=status,
        )

    def test_cool_request_reads_back_as_cool_once_status_settles(self):
        entity = self._ambiguous_entity(requested_hvac_mode="cool", status=2)
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_heat_request_reads_back_as_heat_once_status_settles(self):
        entity = self._ambiguous_entity(requested_hvac_mode="heat", status=2)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)

    def test_ac_on_reads_back_as_heat_cool_once_status_settles(self):
        # AC On (HEAT_COOL) shares this exact same ambiguous status code.
        # Before this fix, only "cool"/"heat" were recognised as requested
        # values -- HEAT_COOL fell through to the Cool default the moment
        # the car's status caught up, silently overriding a genuine AC On
        # selection. Confirmed live on a MGS6 (MIS3E): selecting Heat/Cool
        # displayed correctly for a few seconds, then flipped to Cool.
        entity = self._ambiguous_entity(requested_hvac_mode="heat_cool", status=2)
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_ac_on_survives_a_transient_status_still_reading_off(self):
        entity = self._ambiguous_entity(requested_hvac_mode="heat_cool", status=0)
        entity._attr_hvac_mode = _HVACMode.HEAT_COOL
        entity._last_command_ts = time.monotonic()
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)
        entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = 2
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)

    def test_send_climate_command_records_heat_cool_on_the_coordinator(self):
        # The write side of the same fix: selecting AC On must actually
        # record "heat_cool" for the read side above to have anything to
        # disambiguate from.
        entity = self._ambiguous_entity(requested_hvac_mode="off", status=0)
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertEqual(entity.coordinator.requested_hvac_mode, "heat_cool")

    def test_cool_survives_a_transient_status_still_reading_off(self):
        # This is the exact bug (#380): a poll landing before the car's
        # status has caught up must not lose track of what was asked for.
        entity = self._ambiguous_entity(requested_hvac_mode="cool", status=0)
        entity._attr_hvac_mode = _HVACMode.COOL
        entity._last_command_ts = time.monotonic()
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)
        # And once the car's status catches up to the shared byte, it must
        # still read Cool -- not fall back to something else because
        # _attr_hvac_mode got reset along the way.
        entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = 2
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_heat_survives_a_transient_status_still_reading_off(self):
        entity = self._ambiguous_entity(requested_hvac_mode="heat", status=0)
        entity._attr_hvac_mode = _HVACMode.HEAT
        entity._last_command_ts = time.monotonic()
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)
        entity.coordinator.data["status"].basicVehicleStatus.remoteClimateStatus = 2
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)

    def test_fan_only_is_not_swallowed_by_the_ambiguous_shortcut(self):
        # Before this fix, cool_uses_start_ac being True made the entity
        # ignore every other distinguishable status on mode_select cars,
        # always collapsing to whichever of Cool/Heat was last requested --
        # including fan-only (status 1), which is completely unambiguous.
        entity = self._ambiguous_entity(requested_hvac_mode="heat", status=1)
        self.assertEqual(entity.hvac_mode, _HVACMode.FAN_ONLY)

    def test_max_cool_is_not_swallowed_by_the_ambiguous_shortcut(self):
        entity = self._ambiguous_entity(requested_hvac_mode="cool", status=3)
        # The shared _entity() helper ties climate_status_cool to the same
        # "cool" parameter used for climate_mode_cool (2, the ambiguous
        # byte) -- real ambiguous-car profiles instead point
        # climate_status_cool at the separate, unambiguous max-cool value
        # (3), exactly like AH4EM's actual const.py entry. Set that up
        # explicitly here rather than stretching the shared helper's
        # simpler one-parameter convention to fit.
        entity.coordinator.climate_status_cool = {3}
        self.assertEqual(entity.hvac_mode, _HVACMode.COOL)

    def test_simple_ac_only_car_is_unaffected_by_this_change(self):
        # ZP22 (MG3 Hybrid): cool_uses_start_ac=True and NOT mode_select --
        # must keep using the old self._attr_hvac_mode-trusting shortcut,
        # since there IS no requested_hvac_mode-worthy ambiguity to resolve
        # any other way on this car (only one status covers everything).
        entity = self._entity(
            scheme="fan_speed",
            cool=2,
            climate_mode_heat=2,
            cool_uses_start_ac=True,
            requested_hvac_mode="off",
            status=2,
        )
        entity._attr_hvac_mode = _HVACMode.HEAT
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT)

    def test_simple_ac_car_preserves_heat_cool_not_just_cool_and_heat(self):
        # Same car shape as above: AC On must also survive being read back,
        # not just Cool/Heat -- the preserve-local-state check previously
        # only recognised COOL/HEAT, so a genuine AC On selection silently
        # fell back to Cool the moment status became non-zero.
        entity = self._entity(
            scheme="fan_speed",
            cool=2,
            climate_mode_heat=2,
            cool_uses_start_ac=True,
            requested_hvac_mode="off",
            status=2,
        )
        entity._attr_hvac_mode = _HVACMode.HEAT_COOL
        self.assertEqual(entity.hvac_mode, _HVACMode.HEAT_COOL)


class PresetSetpointRestoreTests(_Base):
    """LOW/HIGH temporarily override the setpoint; it must be restored once
    the user leaves the preset, rather than silently carrying into the next
    command (#374: HIGH then Cool sent HIGH's 28°C, and the car genuinely
    heated while HA showed Cool selected)."""

    def test_cool_after_low_restores_the_pre_preset_temperature(self):
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(entity.coordinator.requested_target_temp, 16)  # min_temp
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        self.assertEqual(entity.coordinator.requested_target_temp, 21.0)
        self.assertIsNone(entity.coordinator.pre_preset_target_temp)

    def test_heat_after_high_restores_the_pre_preset_temperature(self):
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 30)  # max_temp
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT))
        self.assertEqual(entity.coordinator.requested_target_temp, 21.0)

    def test_ac_on_also_restores_the_pre_preset_temperature(self):
        # HEAT_COOL ("AC On") goes through the same default preset=PRESET_NONE
        # path as plain Cool/Heat/Fan-only -- must restore too, not just the
        # HVAC modes tested above.
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _run(entity.async_set_hvac_mode(_HVACMode.HEAT_COOL))
        self.assertEqual(entity.coordinator.requested_target_temp, 21.0)

    def test_explicitly_clearing_the_preset_also_restores(self):
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_NONE))
        self.assertEqual(entity.coordinator.requested_target_temp, 21.0)

    def test_low_then_high_restores_the_original_value_not_lows(self):
        # A preset-to-preset transition (no plain mode in between) must not
        # overwrite the ORIGINAL pre-preset value with the intermediate
        # preset's own override -- otherwise LOW -> HIGH -> Cool would
        # restore to 16°C (LOW's value) instead of the true original.
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        self.assertEqual(entity.coordinator.requested_target_temp, 16)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        self.assertEqual(entity.coordinator.requested_target_temp, 30)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        self.assertEqual(entity.coordinator.requested_target_temp, 21.0)

    def test_manual_temperature_while_a_preset_is_active_cancels_the_restore(self):
        # The user setting their own temperature while LOW/HIGH is active is
        # a deliberate, more recent choice than whatever was active before
        # the preset -- that becomes the new normal, not the old value.
        entity = self._entity()
        entity.coordinator.requested_target_temp = 21.0
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _run(entity.async_set_temperature(temperature=19.0))
        self.assertIsNone(entity.coordinator.pre_preset_target_temp)
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        # 19°C (the manual choice) must survive, not get overwritten by the
        # old pre-preset 21°C.
        self.assertEqual(entity.coordinator.requested_target_temp, 19.0)

    def test_plain_mode_with_no_preset_active_is_unaffected(self):
        # Baseline: nothing saved, nothing to restore -- must not disturb an
        # ordinary temperature change with no preset ever having been used.
        entity = self._entity()
        entity.coordinator.requested_target_temp = 23.0
        _run(entity.async_set_hvac_mode(_HVACMode.COOL))
        self.assertEqual(entity.coordinator.requested_target_temp, 23.0)


class ClassicFanSpeedPresetTests(_Base):
    """LOW/HIGH on classic fan_speed cars (e.g. EH32/MG4) previously reused
    climate_mode_cool -- a mode_select concept meaning nothing on these cars.
    On the MG4 specifically, mode_select's own default of 2 collides with
    this car's climate_status_heat, so LOW reported back as Heat despite
    "Cool" being requested and correctly pinned to min_temp (#380, confirmed
    directly from joaommarques's log: 'Fan speed: 2' sent by LOW)."""

    def test_low_sends_the_strongest_fan_speed_not_a_mode_select_byte(self):
        entity = self._entity(scheme="fan_speed", heat={2})
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        entity._client.start_climate.assert_awaited_once()
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 3)  # fan_speed_high from _entity()
        self.assertEqual(kwargs["ac_on"], True)
        self.assertEqual(entity.coordinator.requested_target_temp, 16)  # min_temp

    def test_high_sends_the_real_heat_fan_speed_with_compressor_off(self):
        # PTC resistive heating only engages with ac_on=False (#173) -- HIGH
        # must match _set_hvac_fan_speed's own HEAT handling exactly.
        entity = self._entity(scheme="fan_speed", heat={2})
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        entity._client.start_climate.assert_awaited_once()
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)  # heat_fan_speed from _entity()
        self.assertEqual(kwargs["ac_on"], False)
        self.assertEqual(entity.coordinator.requested_target_temp, 30)  # max_temp

    def test_low_high_on_mode_select_are_unaffected(self):
        # Regression guard: this fix must not touch the already-correct
        # mode_select path (dedicated max-cool/max-heat bytes, ac_on=True).
        entity = self._entity(scheme="mode_select", heat={2})
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 3)  # climate_mode_max_cool
        self.assertEqual(kwargs["ac_on"], True)

    def test_low_high_on_simple_ac_cars_use_start_ac_not_start_climate(self):
        # e.g. ZP22/MG3 Hybrid: start_climate is silently ignored by the car
        # (see its profile notes) -- LOW/HIGH must use start_ac like every
        # other command on this car, which they did not before this fix.
        entity = self._entity(scheme="fan_speed", cool_uses_start_ac=True)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        entity._client.start_ac.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()
        # _attr_hvac_mode is the value _start_ac_preset itself set -- checked
        # directly rather than via the hvac_mode property, since the fake
        # coordinator's status is still 0 (never simulated as updating),
        # which the property correctly reports as Off regardless.
        self.assertEqual(entity._attr_hvac_mode, _HVACMode.COOL)
        self.assertEqual(entity.coordinator.requested_target_temp, 16)

        entity._client.reset_mock()
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        entity._client.start_ac.assert_awaited_once()
        entity._client.start_climate.assert_not_awaited()
        self.assertEqual(entity._attr_hvac_mode, _HVACMode.HEAT)
        self.assertEqual(entity.coordinator.requested_target_temp, 30)


class ClimateFanAutoPresetTests(_Base):
    """LOW/HIGH on climate_fan_auto cars (e.g. AS33P/HS PHEV) inherited the
    same climate_mode_cool mismatch as classic fan_speed cars, then -- even
    after that was fixed to send fan_speed_high/heat_fan_speed -- still sent
    the wrong value for THIS specific sub-category: these cars have no real
    fan-speed variation at all, and silently ignore any fan_speed value
    other than their one fixed climate_fan_auto value (per the profile's own
    documented notes). Found while investigating a related but distinct
    report on the same car (#262, Harry) -- not yet triggered in practice
    (HIGH needs climate_status_heat, which no climate_fan_auto car currently
    confirms), but fixed alongside the confirmed LOW gap rather than left as
    a second latent bug on the same car."""

    def test_low_sends_the_fixed_auto_value_not_fan_speed_high(self):
        entity = self._entity(scheme="fan_speed", climate_fan_auto=2)
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)  # climate_fan_auto, not fan_speed_high (3)
        self.assertEqual(kwargs["ac_on"], True)
        self.assertEqual(entity.coordinator.requested_target_temp, 16)

    def test_high_sends_the_fixed_auto_value_not_heat_fan_speed(self):
        entity = self._entity(scheme="fan_speed", climate_fan_auto=2, heat={2})
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_HIGH))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 2)  # climate_fan_auto, not heat_fan_speed
        self.assertEqual(kwargs["ac_on"], False)  # PTC-style guard still applies
        self.assertEqual(entity.coordinator.requested_target_temp, 30)

    def test_classic_fan_speed_cars_are_unaffected_by_this_change(self):
        # Regression guard: a car WITHOUT climate_fan_auto set must still get
        # the #380 fix (fan_speed_high/heat_fan_speed), not the new branch.
        entity = self._entity(scheme="fan_speed", climate_fan_auto=None, heat={2})
        _run(entity.async_set_preset_mode(CLIMATE.PRESET_LOW))
        _, kwargs = entity._client.start_climate.await_args
        self.assertEqual(kwargs["fan_speed"], 3)  # fan_speed_high from _entity()


class PresetIconTranslationTests(_Base):
    """icons.json gives each preset a distinct icon instead of plain dots on
    cards like Tile, which need one per preset_mode value to render at all
    (#380, @joaommarques). The icons.json keys must exactly match the real
    PRESET_* string constants -- a mismatch doesn't crash anything, it just
    silently shows no icon for that preset, so this is worth guarding
    against drifting apart if either side is ever renamed."""

    @classmethod
    def setUpClass(cls):
        icons_path = PKG_DIR / "icons.json"
        cls.icons = json.loads(icons_path.read_text())

    def test_climate_entity_sets_the_climate_translation_key(self):
        entity = self._entity()
        self.assertEqual(entity._attr_translation_key, "climate")

    def test_every_preset_constant_has_an_icon(self):
        # PRESET_NONE is deliberately absent from "state": its icon is the
        # attribute-level "default", and hassfest rejects a per-state icon
        # that merely duplicates the default. Keeping the default (rather
        # than an explicit "none" entry) also means any future preset added
        # without an icon still renders something sensible instead of
        # nothing.
        preset_icons = self.icons["entity"]["climate"]["climate"][
            "state_attributes"
        ]["preset_mode"]
        self.assertIn("default", preset_icons)
        for name in (
            "PRESET_LOW",
            "PRESET_HIGH",
            "PRESET_FRONT_WINDSCREEN",
            "PRESET_REAR_WINDSCREEN",
        ):
            value = getattr(CLIMATE, name)
            self.assertIn(
                value,
                preset_icons["state"],
                msg=f"icons.json has no entry for {name} ({value!r})",
            )
        self.assertNotIn(CLIMATE.PRESET_NONE, preset_icons["state"])

    def test_icons_json_has_no_stale_extra_keys(self):
        # The reverse check: every key in icons.json should correspond to a
        # real preset constant, so a renamed/removed preset doesn't leave a
        # dead entry behind silently.
        preset_icons = self.icons["entity"]["climate"]["climate"][
            "state_attributes"
        ]["preset_mode"]["state"]
        real_values = {
            CLIMATE.PRESET_LOW,
            CLIMATE.PRESET_HIGH,
            CLIMATE.PRESET_FRONT_WINDSCREEN,
            CLIMATE.PRESET_REAR_WINDSCREEN,
        }
        self.assertEqual(set(preset_icons.keys()), real_values)

    def test_preset_values_are_snake_case(self):
        # Home Assistant looks these values up verbatim as translation keys,
        # with no normalisation -- anything that isn't snake_case can never
        # resolve to an icon or a translated label, which is exactly why the
        # values were renamed (#380). Guards against a future preset being
        # added back in the old display-text style.
        for name in (
            "PRESET_NONE",
            "PRESET_LOW",
            "PRESET_HIGH",
            "PRESET_FRONT_WINDSCREEN",
            "PRESET_REAR_WINDSCREEN",
        ):
            value = getattr(CLIMATE, name)
            self.assertRegex(
                value,
                r"^[a-z][a-z0-9_]*$",
                msg=f"{name} ({value!r}) is not snake_case -- no icon or "
                f"translated label can resolve for it",
            )

    def test_every_locale_translates_every_preset(self):
        # The rename moved the user-facing text into translations/*.json.
        # If a locale is missing a preset, that preset shows as a raw slug
        # ("front_windscreen") to those users -- silently, with no error.
        real_values = {
            CLIMATE.PRESET_NONE,
            CLIMATE.PRESET_LOW,
            CLIMATE.PRESET_HIGH,
            CLIMATE.PRESET_FRONT_WINDSCREEN,
            CLIMATE.PRESET_REAR_WINDSCREEN,
        }
        locales = sorted((PKG_DIR / "translations").glob("*.json"))
        self.assertTrue(locales, "no translation files found")
        for path in locales:
            data = json.loads(path.read_text(encoding="utf-8"))
            states = (
                data.get("entity", {})
                .get("climate", {})
                .get("climate", {})
                .get("state_attributes", {})
                .get("preset_mode", {})
                .get("state", {})
            )
            self.assertEqual(
                set(states.keys()),
                real_values,
                msg=f"{path.name} preset translations are out of sync",
            )


if __name__ == "__main__":
    unittest.main()
