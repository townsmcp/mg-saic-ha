# File: select.py

from homeassistant.components.select import SelectEntity
from homeassistant.components.climate import HVACMode
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import (
    DOMAIN,
    LOGGER,
    SCHEDULED_CHARGING_MODE_LABELS,
    ChargeCurrentLimitOption,
    BatterySoc,
)
from saic_ismart_client_ng.api.vehicle_charging import (
    ScheduledChargingMode,
    ChargeCurrentLimitCode as ExternalChargeCurrentLimitCode,
)
from .api import CommandsLimitReachedException
from .backends import Feature
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC select entities."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Select entities cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    select_entities = []

    # Climate Mode select — mirrors the climate entity's HVAC mode so it can be
    # set by automations and voice/MCP (which can't set a climate entity's mode
    # directly). Options are gated to the modes the car actually has. Skipped on
    # simple-AC models (e.g. the MG3 Hybrid, which has only Cool/Off — the A/C
    # switch already covers those).
    if not getattr(coordinator, "cool_uses_start_ac", False):
        mode_options = ["Off", "Cool", "Fan Only"]
        if coordinator.climate_status_heat:
            mode_options.append("Heat")
        select_entities.append(
            SAICMGClimateModeSelect(
                coordinator, client, entry, vin_info, vin, mode_options
            )
        )

    if coordinator.supports_charging_current_limit and coordinator.backend_supports(
        Feature.CURRENT_LIMIT
    ):
        select_entities.append(
            SAICMGChargingCurrentSelect(
                coordinator, client, entry, vin_info, vin, "mdi:current-ac"
            )
        )

    if coordinator.vehicle_type in ["BEV", "PHEV"] and coordinator.backend_supports(
        Feature.SCHEDULED_CHARGING
    ):
        select_entities.append(
            SAICMGScheduledChargingModeSelect(coordinator, client, entry, vin_info, vin)
        )

    if coordinator.has_heated_seats:
        # Front seats get a level SELECT (Off/Low/Medium/High) that stores the
        # desired level locally without sending a command — the value is applied
        # when the matching front-seat SWITCH (in switch.py) is turned on. This
        # mirrors the climate entity pattern and avoids spending a command on
        # every dropdown change. Rear seats are on/off only (switch, no select).
        select_entities.extend(
            [
                SAICMGHeatedSeatLevelSelect(
                    coordinator, client, entry, vin_info, vin,
                    "Front Left", "front_left", "frontLeftSeatHeatLevel",
                    "mdi:car-seat-heater",
                ),
                SAICMGHeatedSeatLevelSelect(
                    coordinator, client, entry, vin_info, vin,
                    "Front Right", "front_right", "frontRightSeatHeatLevel",
                    "mdi:car-seat-heater",
                ),
            ]
        )

    async_add_entities(select_entities)


class SAICMGChargingCurrentSelect(CoordinatorEntity, SelectEntity):
    """Representation of a Charging Current Limit select entity."""

    def __init__(self, coordinator, client, entry, vin_info, vin, icon):
        """Initialize the Charging Current Limit select entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._icon = icon

        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Charging Current Limit"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_charging_current_limit"
        self._attr_options = [e.limit for e in ChargeCurrentLimitOption]

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def current_option(self):
        """Return the current selected option."""
        charging_data = self.coordinator.data.get("charging")
        if charging_data:
            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
            if chrg_mgmt_data:
                current_limit_code_value = getattr(
                    chrg_mgmt_data, "bmsAltngChrgCrntDspCmd", None
                )
                if current_limit_code_value is not None:
                    try:
                        external_code = ExternalChargeCurrentLimitCode(
                            current_limit_code_value
                        )
                        for option in ChargeCurrentLimitOption:
                            if option.value == external_code.value:
                                return option.limit
                    except ValueError:
                        LOGGER.error(
                            f"Unknown external charge current limit code: {current_limit_code_value}"
                        )
                        return None
        return None

    @property
    def available(self):
        """Return True if the entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("charging") is not None
        )

    async def async_select_option(self, option: str):
        """Set the Charging Current Limit to the selected option."""
        try:
            # Map the string option to the local enum
            selected_code = ChargeCurrentLimitOption.to_code(option)

            # Get the current target_soc from coordinator's data
            charging_data = self.coordinator.data.get("charging")
            if not charging_data:
                LOGGER.error(
                    "No charging data available to set charging current limit."
                )
                return

            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
            if not chrg_mgmt_data:
                LOGGER.error(
                    "No charging management data available to set charging current limit."
                )
                return

            target_soc_value = getattr(chrg_mgmt_data, "bmsOnBdChrgTrgtSOCDspCmd", None)
            if target_soc_value is None:
                LOGGER.error(
                    "Target SOC value is not available to set charging current limit."
                )
                return

            # Map the target_soc_value to BatterySoc enum
            target_soc_enum = {
                1: BatterySoc.SOC_40,
                2: BatterySoc.SOC_50,
                3: BatterySoc.SOC_60,
                4: BatterySoc.SOC_70,
                5: BatterySoc.SOC_80,
                6: BatterySoc.SOC_90,
                7: BatterySoc.SOC_100,
            }.get(target_soc_value, None)

            if target_soc_enum is None:
                LOGGER.error(f"Unknown target SOC value: {target_soc_value}")
                raise ValueError(f"Unknown target SOC value: {target_soc_value}")

            # Set the charging current limit with target_soc
            await self._client.set_current_limit(
                self._vin, target_soc_enum, selected_code
            )
            LOGGER.info(
                "Set Charging Current Limit to %s for VIN: %s", option, self._vin
            )
            # Schedule a refresh
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.charging_current_long_interval

            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except ValueError as e:
            LOGGER.error("Invalid option selected: %s", option)
            raise
        except Exception as e:
            LOGGER.error(
                "Error setting Charging Current Limit to %s for VIN %s: %s",
                option,
                self._vin,
                e,
            )


class SAICMGScheduledChargingModeSelect(CoordinatorEntity, SelectEntity):
    """Select entity for the scheduled charging mode.

    Selecting a mode sends one command to the vehicle, applying the mode
    together with the charging window from the Scheduled Charging Start/End
    time entities (pending values take precedence over vehicle-reported ones).
    """

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Scheduled Charging Mode select entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Scheduled Charging Mode"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_scheduled_charging_mode"
        self._attr_icon = "mdi:battery-clock"

        # Hide the target-SOC mode on vehicles that do not support target SOC,
        # matching the upstream mqtt-gateway guard.
        self._attr_options = [
            label
            for label, mode_name in SCHEDULED_CHARGING_MODE_LABELS.items()
            if mode_name != "UNTIL_CONFIGURED_SOC"
            or getattr(coordinator, "supports_target_soc", True)
        ]

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def current_option(self):
        """Return the currently active scheduled charging mode."""
        charging_data = self.coordinator.data.get("charging")
        chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
        if chrg_mgmt_data is None:
            return None
        raw_mode = getattr(chrg_mgmt_data, "bmsReserCtrlDspCmd", None)
        if raw_mode is None or raw_mode == 0:
            return None
        try:
            mode = ScheduledChargingMode(raw_mode)
        except ValueError:
            LOGGER.debug(
                "Unknown scheduled charging mode code %s for VIN %s",
                raw_mode,
                self._vin,
            )
            return None
        for label, mode_name in SCHEDULED_CHARGING_MODE_LABELS.items():
            if mode_name == mode.name:
                return label
        return None

    @property
    def available(self):
        """Return True if the entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("charging") is not None
        )

    def _resolve_window(self):
        """Resolve the charging window to send: pending -> vehicle -> default."""
        from .time import (
            DEFAULT_SCHEDULED_CHARGING_END,
            DEFAULT_SCHEDULED_CHARGING_START,
            decode_scheduled_charging_field,
        )

        charging_data = self.coordinator.data.get("charging")
        chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)

        start = self.coordinator.scheduled_charging_pending_start
        if start is None:
            start = decode_scheduled_charging_field(
                chrg_mgmt_data, "bmsReserStHourDspCmd", "bmsReserStMintueDspCmd"
            )
        if start is None:
            start = DEFAULT_SCHEDULED_CHARGING_START

        end = self.coordinator.scheduled_charging_pending_end
        if end is None:
            end = decode_scheduled_charging_field(
                chrg_mgmt_data, "bmsReserSpHourDspCmd", "bmsReserSpMintueDspCmd"
            )
        if end is None:
            end = DEFAULT_SCHEDULED_CHARGING_END

        return start, end

    async def async_select_option(self, option: str):
        """Apply the selected scheduled charging mode with the current window."""
        mode_name = SCHEDULED_CHARGING_MODE_LABELS.get(option)
        if mode_name is None:
            LOGGER.error("Invalid scheduled charging mode option: %s", option)
            return
        mode = ScheduledChargingMode[mode_name]

        try:
            start, end = self._resolve_window()
            await self._client.set_scheduled_charging(self._vin, start, end, mode)
            LOGGER.info(
                "Scheduled charging set to '%s' (%s - %s) for VIN: %s",
                option,
                start,
                end,
                self._vin,
            )
            await self.coordinator.schedule_action_refresh(
                self._vin,
                self.coordinator.after_action_delay,
                self.coordinator.charging_current_long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error(
                "Error setting scheduled charging mode to %s for VIN %s: %s",
                option,
                self._vin,
                e,
            )
            self.coordinator.record_command_error(
                "Error setting scheduled charging mode", e
            )


class SAICMGHeatedSeatLevelSelect(CoordinatorEntity, SelectEntity):
    """Level selector for a FRONT heated seat (Off/Low/Medium/High).

    This stores the desired level LOCALLY only — selecting a level does NOT
    send a command to the car. The level is applied when the matching front
    seat switch (SAICMGHeatedSeatSwitch) is turned on. This mirrors the climate
    entity's "set locally, action sends" pattern and conserves the command
    budget. The coordinator holds the pending level so the switch can read it.
    """

    def __init__(
        self, coordinator, client, entry, vin_info, vin,
        seat_name, seat_key, status_field, icon,
    ):
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._seat_key = seat_key          # "front_left" / "front_right"
        self._status_field = status_field
        self._icon = icon
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Heated Seat {seat_name} Level"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_heated_seat_{seat_key}_level"
        self._attr_options = ["Off", "Low", "Medium", "High"]
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        return self._device_info

    @property
    def icon(self):
        return self._icon

    @property
    def current_option(self):
        """Show the pending local level if one is set, else the car's reported level.

        The pending level (set locally, not yet applied) is stored on the
        coordinator keyed by seat, so it survives across entity refreshes and is
        readable by the switch.
        """
        pending = self.coordinator.pending_seat_levels.get(self._seat_key)
        if pending is not None:
            return {0: "Off", 1: "Low", 2: "Medium", 3: "High"}.get(pending, "Off")

        status = self.coordinator.data.get("status")
        level = 0
        if status:
            basic_status = getattr(status, "basicVehicleStatus", None)
            if basic_status:
                level = getattr(basic_status, self._status_field, 0) or 0
        return {0: "Off", 1: "Low", 2: "Medium", 3: "High"}.get(level, "Off")

    async def async_select_option(self, option: str):
        """Store the chosen level locally (no command sent)."""
        level = {"Off": 0, "Low": 1, "Medium": 2, "High": 3}.get(option, 0)
        self.coordinator.pending_seat_levels[self._seat_key] = level
        LOGGER.debug(
            "Heated seat %s level set locally to %s (%d) — will apply when the "
            "seat switch is turned on.",
            self._seat_key,
            option,
            level,
        )
        self.async_write_ha_state()


CLIMATE_MODE_OPTION_TO_HVAC = {
    "Off": HVACMode.OFF,
    "Cool": HVACMode.COOL,
    "Fan Only": HVACMode.FAN_ONLY,
    "Heat": HVACMode.HEAT,
}


class SAICMGClimateModeSelect(CoordinatorEntity, SelectEntity):
    """Climate mode as a standalone select (Off / Cool / Fan Only / [Heat]).

    Mirrors the climate entity's HVAC mode so the mode can be set by
    automations and voice/MCP, which cannot set a climate entity's mode
    directly. Delegates the command to the climate entity (single dispatch
    path) and reflects the car's real mode from remoteClimateStatus, so it
    stays in sync with the climate entity, A/C switch and temperature number.
    """

    def __init__(self, coordinator, client, entry, vin_info, vin, options):
        """Initialize the Climate Mode select."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Climate Mode"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_climate_mode"
        self._attr_icon = "mdi:hvac"
        self._attr_options = options
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def available(self):
        """Unavailable whenever the climate entity is (e.g. the car is
        unreachable), so the mode select mirrors the climate entity it
        controls rather than showing a stale selection."""
        climate = self.coordinator.climate_entity
        if climate is None:
            return False
        return climate.available

    @property
    def current_option(self):
        """Reflect the climate entity's reconciled mode, so the select never
        disagrees with it (unlike decoding the raw status separately, which
        showed no selection whenever the car reported an unmapped status)."""
        climate = self.coordinator.climate_entity
        if climate is None:
            return None
        mapping = {
            HVACMode.OFF: "Off",
            HVACMode.COOL: "Cool",
            HVACMode.FAN_ONLY: "Fan Only",
            HVACMode.HEAT: "Heat",
        }
        opt = mapping.get(climate.hvac_mode)
        return opt if opt in self._attr_options else None

    async def async_select_option(self, option):
        """Set the climate mode by delegating to the climate entity."""
        climate = self.coordinator.climate_entity
        if climate is None:
            LOGGER.warning("Climate entity not ready; cannot set the climate mode.")
            return
        hvac = CLIMATE_MODE_OPTION_TO_HVAC.get(option)
        if hvac is None:
            LOGGER.warning("Unknown climate mode option: %s", option)
            return
        await climate.async_set_hvac_mode(hvac)
        self.coordinator.async_update_listeners()
