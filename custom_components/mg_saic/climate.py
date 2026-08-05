# File: climate.py

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
import time
from homeassistant.components.climate.const import (
    FAN_LOW,
    FAN_MEDIUM,
    FAN_HIGH,
)
from homeassistant.const import (
    UnitOfTemperature,
    ATTR_TEMPERATURE,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .api import CommandsLimitReachedException
from .const import (
    DOMAIN,
    FRONT_DEFROST_TEMP_C,
    LOGGER,
)
from .utils import create_device_info

# Preset names used by the "mode_select" climate scheme (e.g. IS31P / MG S9 PHEV).
# These are exposed as HA preset_modes for modes that don't map cleanly onto a
# standard HVACMode — a fast strong-fan cool-down, and windscreen defrost.
PRESET_NONE = "none"
PRESET_MAX_COOL = "Max Cool"
PRESET_DEFROST = "Defrost"

# How long (seconds) to keep showing a locally-set HVAC mode after we send a
# command, while the car catches up and starts reporting the matching status.
# During this window a remoteClimateStatus of 0 is treated as "car hasn't
# responded yet" rather than "climate is off", to avoid the UI flickering back
# to Off immediately after the user turns the AC on. Once the window expires, a
# reported 0 is trusted as genuinely Off — this prevents the entity getting
# stuck on Cool when the car's climate stops via a path other than our command
# (e.g. the driver powering the car on, issue #204).
COMMAND_SYNC_GRACE_SECONDS = 90


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the MG SAIC climate entity."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Climate cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    climate_entity = SAICMGClimateEntity(coordinator, client, entry, vin_info, vin)
    async_add_entities([climate_entity])


class SAICMGClimateEntity(CoordinatorEntity, ClimateEntity):
    """Representation of the vehicle's climate control.

    Two control schemes are supported, selected per-model via the vehicle
    profile (coordinator.climate_control_scheme):

    * "fan_speed" (default): the classic behaviour. HA exposes a Low/Med/High
      fan slider plus Off/Cool/Fan-only HVAC modes. The chosen fan value is
      sent with each AC command. Used by MG4, MGS5, Cyberster, HS PHEV, and any
      unprofiled model.

    * "mode_select": for cars where the API's "fan_speed" byte is actually a
      climate MODE selector, not a linear fan speed (confirmed on IS31P / MG
      S9 PHEV — see const.py). HA instead exposes Off/Fan-only/Cool/Heat HVAC
      modes plus "Max Cool" and "Defrost" presets, and no fan slider. Each maps
      to a fixed mode integer; the car manages its own fan.
    """

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the climate entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info

        self._series = vin_info.series
        self._scheme = coordinator.climate_control_scheme

        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Climate"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_climate"
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Common initial state
        self._attr_min_temp = self.min_temp
        self._attr_max_temp = self.max_temp
        # target_temperature is shared with the Climate Target Temperature
        # number entity through the coordinator (see the target_temperature
        # property below), so both stay in sync. It is display-only and never
        # sends a command on its own.
        self._attr_hvac_mode = HVACMode.OFF
        # Monotonic timestamp of the last climate command we sent, used to
        # bound the "preserve local state on status 0" grace window (see
        # hvac_mode). 0.0 means "no command sent this session".
        self._last_command_ts = 0.0

        if self._scheme == "mode_select":
            # Mode-select cars: no fan slider. Heat and the Defrost preset are
            # only offered when the profile actually defines them. Some variants
            # (e.g. the MG4 EV URBAN, series AH4EM — see #243) have no heat mode
            # at all; offering an HVAC mode the car can't perform is misleading
            # and sending it does nothing useful.
            hvac_modes = [HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.COOL]
            if coordinator.climate_status_heat:
                hvac_modes.append(HVACMode.HEAT)
            self._attr_hvac_modes = hvac_modes

            preset_modes = [PRESET_NONE]
            # Offer Max Cool either when the car has a genuinely distinct
            # max-cool mode value, or when the profile asks Max Cool to pin the
            # setpoint to the minimum (cars whose plain Cool is already the
            # strongest cool — e.g. AH4EM, #243).
            if (
                coordinator.climate_mode_max_cool != coordinator.climate_mode_cool
                or coordinator.max_cool_forces_min_temp
            ):
                preset_modes.append(PRESET_MAX_COOL)
            if coordinator.climate_status_defrost:
                preset_modes.append(PRESET_DEFROST)
            self._attr_preset_modes = preset_modes
            self._attr_preset_mode = PRESET_NONE

            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.PRESET_MODE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
            )
            self._attr_fan_modes = None
            self._attr_fan_mode = None
        elif coordinator.cool_uses_start_ac:
            # Cars that only honour the simple start_ac command (e.g. the MG3
            # Hybrid, series ZP22 — see #258). control_climate (with a fan byte)
            # is silently ignored, and there is no mode byte — start_ac just
            # drives the cabin to a target temperature, heating or cooling as
            # needed. So we expose Cool and Heat as the two ends of the range:
            # Cool = coldest (min temp), Heat = hottest (max temp), each a single
            # tap that also moves the setpoint. No fan slider, no Fan Only, no
            # Defrost (all of those need control_climate, which the car ignores).
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
            )
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT]
            self._attr_fan_modes = None
            self._attr_fan_mode = None
        else:
            # Classic fan-speed cars: Low/Med/High fan slider.
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE
                | ClimateEntityFeature.FAN_MODE
                | ClimateEntityFeature.TURN_ON
                | ClimateEntityFeature.TURN_OFF
            )
            self._attr_hvac_modes = [
                HVACMode.OFF,
                HVACMode.COOL,
                HVACMode.FAN_ONLY,
            ]
            # Some fan-speed cars also have a heater (e.g. the MG4's PTC
            # resistive heater — #173). Only offer Heat when the profile defines
            # a heat status, so cars without confirmed heating don't show a mode
            # that does nothing.
            if coordinator.climate_status_heat:
                self._attr_hvac_modes.append(HVACMode.HEAT)
            self._attr_fan_modes = [FAN_LOW, FAN_MEDIUM, FAN_HIGH]
            self._attr_fan_mode = FAN_MEDIUM

    @property
    def temperature_unit(self) -> str:
        """Return the unit of measurement."""
        return UnitOfTemperature.CELSIUS

    @property
    def target_temperature_step(self) -> float:
        """Return the supported step of target temperature."""
        return 1.0

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        return self.coordinator.min_temp

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        return self.coordinator.max_temp

    def get_temp_offset(self):
        """Return the temperature offset."""
        return self.coordinator.temp_offset

    @property
    def current_temperature(self):
        """Return the current interior temperature."""
        status = self.coordinator.data.get("status")
        if status:
            interior_temp = getattr(
                status.basicVehicleStatus, "interiorTemperature", None
            )
            if interior_temp is not None and interior_temp != -128:
                return interior_temp
        return None

    def _current_climate_status(self):
        """Return the car's current remoteClimateStatus, or None if unavailable."""
        status = self.coordinator.data.get("status")
        if status and status.basicVehicleStatus:
            return getattr(status.basicVehicleStatus, "remoteClimateStatus", 0)
        return None

    @property
    def hvac_mode(self):
        """Return the current HVAC mode, derived from the car's reported status.

        The car echoes its active climate mode back via remoteClimateStatus.
        We map that to an HVAC mode using the per-model reverse maps in the
        coordinator (climate_status_cool / _fan_only / _heat / _defrost).

        When the car reports 0 (off) but we've just issued a command, we keep
        the local state briefly so the UI doesn't flicker back to Off before
        the car confirms. Any other unrecognised/inactive status resolves to
        Off, so the entity never gets stuck showing an active mode after the
        car shuts its climate off on its own (issue #204).
        """
        # Simple-AC cars (start_ac only, e.g. MG3): the car reports the same
        # status (2) whether it is heating or cooling — it can't tell us which.
        # So trust the mode we last asked for (Cool/Heat) while the car reports
        # it is running, and Off when it reports off. Falls back to Off if we
        # never sent anything.
        if self.coordinator.cool_uses_start_ac:
            status = self._current_climate_status()
            if status == 0:
                self._attr_hvac_mode = HVACMode.OFF
                return HVACMode.OFF
            if status is None:
                return self._attr_hvac_mode or HVACMode.OFF
            if self._attr_hvac_mode in (HVACMode.COOL, HVACMode.HEAT):
                return self._attr_hvac_mode
            return HVACMode.COOL

        climate_status = self._current_climate_status()

        if climate_status is not None:
            if climate_status in self.coordinator.climate_status_heat:
                self._attr_hvac_mode = HVACMode.HEAT
                return HVACMode.HEAT
            if climate_status in self.coordinator.climate_status_cool:
                self._attr_hvac_mode = HVACMode.COOL
                return HVACMode.COOL
            if climate_status in self.coordinator.climate_status_defrost:
                # Defrost is a preset layered on top of an active (cooling)
                # system; report COOL as the base HVAC mode.
                self._attr_hvac_mode = HVACMode.COOL
                return HVACMode.COOL
            if climate_status in self.coordinator.climate_status_fan_only:
                self._attr_hvac_mode = HVACMode.FAN_ONLY
                return HVACMode.FAN_ONLY
            if climate_status == 0:
                # Car reports Off. Normally trust it — but briefly preserve a
                # locally-set active mode if we've *just* sent a command and the
                # car hasn't caught up yet, so the UI doesn't flicker to Off the
                # instant the user turns the AC on.
                #
                # Crucially this grace window is time-bounded: once it expires,
                # a reported 0 is always trusted as genuinely Off. Without the
                # time bound, the entity would stay stuck on Cool forever if the
                # car's climate stopped via any path other than our own command
                # (e.g. the driver powering the car on — issue #204).
                if self._attr_hvac_mode in (HVACMode.OFF, None):
                    return HVACMode.OFF
                within_grace = (
                    self._last_command_ts > 0.0
                    and (time.monotonic() - self._last_command_ts)
                    < COMMAND_SYNC_GRACE_SECONDS
                )
                if within_grace:
                    return self._attr_hvac_mode
                # Grace window expired — trust the car. Climate is off.
                self._attr_hvac_mode = HVACMode.OFF
                if self._scheme == "mode_select":
                    self._attr_preset_mode = PRESET_NONE
                return HVACMode.OFF
            # Unrecognised/inactive status → Off (do not preserve stale state).
            self._attr_hvac_mode = HVACMode.OFF
            return HVACMode.OFF

        # No API data — fall back to last known local state.
        return self._attr_hvac_mode if self._attr_hvac_mode is not None else HVACMode.OFF

    @property
    def preset_mode(self):
        """Return the active preset (mode_select scheme only).

        Derived from the car's reported status so it stays in sync: a status
        matching the Max Cool value shows "Max Cool", the Defrost value shows
        "Defrost", anything else shows "none".
        """
        if self._scheme != "mode_select":
            return None

        climate_status = self._current_climate_status()
        if climate_status is not None:
            if climate_status in self.coordinator.climate_status_defrost:
                self._attr_preset_mode = PRESET_DEFROST
                return PRESET_DEFROST
            if climate_status == self.coordinator.climate_mode_max_cool:
                self._attr_preset_mode = PRESET_MAX_COOL
                return PRESET_MAX_COOL
            self._attr_preset_mode = PRESET_NONE
            return PRESET_NONE
        return self._attr_preset_mode

    def _temperature_idx(self):
        """Return the API temperature index for the current target temp."""
        temp_clamped = int(
            max(self.min_temp, min(self.max_temp, round(self.coordinator.requested_target_temp)))
        )
        return self.coordinator.get_ac_temperature_idx(temp_clamped)

    async def _send_climate_command(
        self,
        mode_value: int,
        hvac_mode,
        preset=PRESET_NONE,
        temperature_override: int | None = None,
    ):
        """Send a climate command using a raw mode/fan integer, update state,
        and schedule the post-action refresh. Shared by both schemes.

        temperature_override forces a specific temperature (°C) instead of the
        user's target temperature — used by front defrost, which the iSmart app
        always sends at a fixed 22°C regardless of the AC temperature setting
        (confirmed by decrypted capture: slider at 26°C still sent paramId
        20=8, i.e. 22°C). The user's target temperature is deliberately NOT
        changed by this: the app keeps displaying the user's own setting too,
        and overwriting it would lose their preference when defrost auto-cancels
        after ~10 minutes.
        """
        if temperature_override is not None:
            temperature_idx = self.coordinator.get_ac_temperature_idx(
                temperature_override
            )
        else:
            temperature_idx = self._temperature_idx()

        await self._client.start_climate(
            self._vin,
            temperature_idx=temperature_idx,
            fan_speed=mode_value,
            ac_on=True,
        )
        self._last_command_ts = time.monotonic()
        self._attr_hvac_mode = hvac_mode
        if self._scheme == "mode_select":
            self._attr_preset_mode = preset
        self.async_write_ha_state()
        await self.coordinator.schedule_action_refresh(
            self._vin,
            self.coordinator.after_action_delay,
            self.coordinator.ac_long_interval,
        )

    async def async_set_hvac_mode(self, hvac_mode):
        """Set the HVAC mode."""
        try:
            if hvac_mode == HVACMode.OFF:
                await self._client.stop_ac(self._vin)
                # Off is a definite state — clear the grace window so any
                # subsequent status 0 from the car is trusted immediately.
                self._last_command_ts = 0.0
                self._attr_hvac_mode = HVACMode.OFF
                if self._scheme == "mode_select":
                    self._attr_preset_mode = PRESET_NONE
                self.async_write_ha_state()
                return

            if self._scheme == "mode_select":
                await self._set_hvac_mode_select(hvac_mode)
            else:
                await self._set_hvac_fan_speed(hvac_mode)

        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error setting HVAC mode for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error setting HVAC mode", e)

    async def _set_hvac_mode_select(self, hvac_mode):
        """Handle HVAC mode changes for the mode_select scheme."""
        c = self.coordinator
        if hvac_mode == HVACMode.COOL:
            await self._send_climate_command(c.climate_mode_cool, HVACMode.COOL)
        elif hvac_mode == HVACMode.HEAT:
            await self._send_climate_command(c.climate_mode_heat, HVACMode.HEAT)
        elif hvac_mode == HVACMode.FAN_ONLY:
            await self._send_climate_command(c.climate_mode_fan_only, HVACMode.FAN_ONLY)
        else:
            LOGGER.warning("Unsupported HVAC mode for mode_select: %s", hvac_mode)

    async def _set_hvac_fan_speed(self, hvac_mode):
        """Handle HVAC mode changes for the classic fan_speed scheme."""
        if hvac_mode == HVACMode.COOL:
            if self.coordinator.cool_uses_start_ac:
                # start_ac is the only command this car acts on (#258). "Cool"
                # means "as cold as possible": drive to the minimum temperature.
                # Move the visible setpoint too so the slider reflects it.
                self.coordinator.requested_target_temp = self.min_temp
                await self._client.start_ac(
                    vin=self._vin,
                    temperature_idx=self._temperature_idx(),
                )
            else:
                await self._client.start_climate(
                    self._vin,
                    temperature_idx=self._temperature_idx(),
                    fan_speed=self._fan_speed_to_int(),
                    ac_on=True,
                )
        elif hvac_mode == HVACMode.HEAT:
            if self.coordinator.cool_uses_start_ac:
                # This car has no heat *mode* — start_ac just drives the cabin to
                # a target temperature, so "Heat" means "as hot as possible":
                # drive to the maximum temperature (same command as Cool, other
                # end of the range). Move the visible setpoint to match.
                self.coordinator.requested_target_temp = self.max_temp
                await self._client.start_ac(
                    vin=self._vin,
                    temperature_idx=self._temperature_idx(),
                )
            else:
                # PTC resistive heating (e.g. MG4). Confirmed from decrypted
                # iSmart traffic (#173): the heater engages only with the
                # compressor OFF (ac_on=False) AND the AUTO fan value; any other
                # fan value does nothing. The app drives it to the top of the
                # temperature range, so we send the max index. Only reachable
                # when the profile defines a heat status (see __init__), so cars
                # without a confirmed heater never hit this path.
                await self._client.start_climate(
                    self._vin,
                    temperature_idx=self.coordinator.get_ac_temperature_idx(
                        int(self.max_temp)
                    ),
                    fan_speed=self.coordinator.heat_fan_speed,
                    ac_on=False,
                )
        elif hvac_mode == HVACMode.FAN_ONLY:
            await self._client.start_ac(
                vin=self._vin,
                temperature_idx=self._temperature_idx(),
            )
        else:
            LOGGER.warning("Unsupported HVAC mode: %s", hvac_mode)
            return

        self._last_command_ts = time.monotonic()
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()
        await self.coordinator.schedule_action_refresh(
            self._vin,
            self.coordinator.after_action_delay,
            self.coordinator.ac_long_interval,
        )

    async def async_set_preset_mode(self, preset_mode):
        """Set a preset mode (mode_select scheme only): Max Cool or Defrost."""
        if self._scheme != "mode_select":
            LOGGER.warning("Preset modes are not supported for this vehicle.")
            return

        try:
            c = self.coordinator
            if preset_mode == PRESET_MAX_COOL:
                if c.max_cool_forces_min_temp:
                    # Mirror the iSmart app's LOW-cool button: strongest cool
                    # mode + coldest setpoint in a single tap. Set the visible
                    # target temperature to the profile minimum first — this is
                    # persistent and user-intended (unlike Defrost's transient
                    # fixed temp), so the card should reflect it — then send the
                    # cool mode, which picks up the new setpoint via
                    # _temperature_idx().
                    self.coordinator.requested_target_temp = self.min_temp
                await self._send_climate_command(
                    c.climate_mode_max_cool, HVACMode.COOL, preset=PRESET_MAX_COOL
                )
            elif preset_mode == PRESET_DEFROST:
                # The vehicle will not start front defrost while the AC is
                # already running (the iSmart app blocks this client-side and
                # tells the user to turn AC Auto mode off first). Mirror that
                # rather than sending a command the car ignores.
                if c.is_climate_blocking_defrost():
                    await c.notify_front_defrost_blocked(
                        self._vin, "climate.set_preset_mode"
                    )
                    return
                await self._send_climate_command(
                    c.climate_mode_defrost,
                    HVACMode.COOL,
                    preset=PRESET_DEFROST,
                    temperature_override=FRONT_DEFROST_TEMP_C,
                )
            elif preset_mode == PRESET_NONE:
                # Returning to "none" means plain cool (auto fan).
                await self._send_climate_command(
                    c.climate_mode_cool, HVACMode.COOL, preset=PRESET_NONE
                )
            else:
                LOGGER.warning("Unsupported preset mode: %s", preset_mode)

        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error setting preset mode for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error setting preset mode", e)

    async def async_turn_off(self):
        """Turn the climate entity off."""
        await self.async_set_hvac_mode(HVACMode.OFF)
        await self.coordinator.schedule_action_refresh(
            self._vin,
            self.coordinator.after_action_delay,
            self.coordinator.ac_long_interval,
        )

    @property
    def target_temperature(self):
        """Return the shared target temperature.

        Backed by coordinator.requested_target_temp so the climate entity and
        the Climate Target Temperature number entity always show the same
        value — changing it in either place updates both.
        """
        return self.coordinator.requested_target_temp

    async def async_added_to_hass(self):
        """Register this climate entity so the A/C switch and Climate Mode
        select can delegate their commands to it (single dispatch path)."""
        await super().async_added_to_hass()
        self.coordinator.climate_entity = self

    async def async_turn_on(self):
        """Turn the A/C on at the current setpoint (no mode extreme).

        The plain on control — the climate power button and the separate A/C
        switch — means "run at whatever temperature is set", not a Cool/Heat
        extreme. For most cars that's identical to selecting Cool (which uses
        the setpoint). For simple-AC cars (e.g. the MG3) Cool means "coldest",
        so we send start_ac at the setpoint directly and report Cool as the
        on-state.
        """
        if getattr(self.coordinator, "cool_uses_start_ac", False):
            self._attr_hvac_mode = HVACMode.COOL
            await self._client.start_ac(
                vin=self._vin,
                temperature_idx=self._temperature_idx(),
            )
            self._last_command_ts = time.monotonic()
            self.async_write_ha_state()
            await self.coordinator.schedule_action_refresh(
                self._vin,
                self.coordinator.after_action_delay,
                self.coordinator.ac_long_interval,
            )
        else:
            await self.async_set_hvac_mode(HVACMode.COOL)

    async def async_set_temperature(self, **kwargs):
        """Update the target temperature in local state only.

        Temperature is stored locally and applied the next time the user
        explicitly changes HVAC/preset mode (which sends the actual command).
        Changing temperature alone does NOT send a command — this preserves
        the 3-command-per-day limit for intentional AC actions only.
        """
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        temp_clamped = int(max(self.min_temp, min(self.max_temp, round(temperature))))

        if temp_clamped != self.coordinator.requested_target_temp:
            LOGGER.debug(
                "Target temperature updated to %s°C (will apply on next AC command)",
                temp_clamped,
            )
            self.coordinator.requested_target_temp = temp_clamped
            self.async_write_ha_state()
            # Keep the shared Climate Target Temperature number in sync.
            self.coordinator.async_update_listeners()

    @property
    def fan_mode(self):
        """Return the current fan mode (fan_speed scheme only)."""
        if self._scheme == "mode_select":
            return None
        return self._attr_fan_mode

    async def async_set_fan_mode(self, fan_mode):
        """Update the fan mode in local state only (fan_speed scheme only).

        Fan mode is stored locally and applied the next time the user
        explicitly changes HVAC mode (which sends the actual command).
        Changing fan mode alone does NOT send a command — this preserves
        the 3-command-per-day limit for intentional AC actions only.
        """
        if self._scheme == "mode_select":
            LOGGER.warning("Fan modes are not supported for this vehicle.")
            return

        if fan_mode in (self._attr_fan_modes or []):
            self._attr_fan_mode = fan_mode
            LOGGER.debug(
                "Fan mode updated to %s (will apply on next AC command)", fan_mode
            )
            self.async_write_ha_state()
        else:
            LOGGER.warning("Unsupported fan mode: %s", fan_mode)

    @property
    def available(self):
        """Return True if the climate entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("status") is not None
        )

    def _fan_speed_to_int(self):
        """Convert fan mode to integer value expected by the API (fan_speed scheme).

        Fan speed values are model-specific — values 4 and 5 trigger heating
        and defrost on some models, so per-model values are stored in the
        coordinator's fan_speed_low/medium/high from VEHICLE_PROFILES.
        """
        return {
            FAN_LOW: self.coordinator.fan_speed_low,
            FAN_MEDIUM: self.coordinator.fan_speed_medium,
            FAN_HIGH: self.coordinator.fan_speed_high,
        }.get(self._attr_fan_mode, self.coordinator.fan_speed_medium)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info
