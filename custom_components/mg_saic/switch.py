# File: switch.py

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .api import CommandsLimitReachedException
from .const import (
    DOMAIN,
    LOGGER,
    CHARGING_STATUS_CODES,
)
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC switches."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Switches cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    switches = []

    # Front Defrost Switch
    switches.append(SAICMGFrontDefrostSwitch(coordinator, client, entry, vin_info, vin))

    # Rear Window Defrost
    switches.append(
        SAICMGRearWindowDefrostSwitch(coordinator, client, entry, vin_info, vin)
    )

    # Sunroof Switch
    # NOTE: sunroof control and status are currently non-functional on tested
    # models. On the MGS6 EV, the SAIC API reports sunroofStatus=0 permanently
    # regardless of the physical roof position (confirmed by capture, 2026-07),
    # and no working control command has been identified. The has_sunroof option
    # is therefore off by default and effectively experimental — it is retained
    # so existing users who enabled it keep their entities, and so it can be
    # re-tested if MG adds sunroof support to the iSmart app in future.
    if coordinator.has_sunroof:
        switches.append(SAICMGSunroofSwitch(coordinator, client, entry, vin_info, vin))
    else:
        LOGGER.debug(f"Sunroof switch not created for VIN {vin}.")

    # Heated Seats Switch (if applicable)
    if coordinator.has_heated_seats:
        switches.extend(
            [
                SAICMGHeatedSeatsSwitch(
                    coordinator,
                    client,
                    entry,
                    vin_info,
                    vin,
                    "Front Left",
                    "left",
                    "frontLeftSeatHeatLevel",
                ),
                SAICMGHeatedSeatsSwitch(
                    coordinator,
                    client,
                    entry,
                    vin_info,
                    vin,
                    "Front Right",
                    "right",
                    "frontRightSeatHeatLevel",
                ),
            ]
        )
    else:
        LOGGER.debug(f"Heated seats switch not created for VIN {vin}.")

    # Charging Switches (for BEV and PHEV)
    if coordinator.vehicle_type in ["BEV", "PHEV"]:
        switches.append(SAICMGChargingSwitch(coordinator, client, entry, vin_info, vin))
        switches.append(
            SAICMGChargingPortLockSwitch(coordinator, client, entry, vin_info, vin)
        )

        # Check if battery heating is supported
        if coordinator.has_battery_heating:
            switches.append(
                SAICMGBatteryHeatingSwitch(coordinator, client, entry, vin_info, vin)
            )
        else:
            LOGGER.debug(f"Battery heating switch not created for VIN {vin}.")

    async_add_entities(switches)


class SAICMGVehicleSwitch(CoordinatorEntity, SwitchEntity):
    """Base class for MG SAIC switches."""

    def __init__(self, coordinator, client, entry, vin_info, vin, name, icon):
        """Initialize the switch."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} {name}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{vin}_{name.replace(' ', '_').lower()}_switch"
        )
        self._attr_icon = icon

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    async def async_turn_on(self, **kwargs):
        """Turn the switch on."""
        raise NotImplementedError()

    async def async_turn_off(self, **kwargs):
        """Turn the switch off."""
        raise NotImplementedError()

    @property
    def is_on(self):
        """Return true if the switch is on."""
        raise NotImplementedError()

    @property
    def available(self):
        """Return True if the switch entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("status") is not None
        )


class SAICMGBatteryHeatingSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control battery heating."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Battery Heating switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Battery Heating"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_battery_heating_switch"
        self._attr_icon = "mdi:heat-wave"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if battery heating is active."""
        charging_data = self.coordinator.data.get("charging")
        if charging_data:
            chrgMgmtData = getattr(charging_data, "chrgMgmtData", None)
            if chrgMgmtData:
                bmsPTCHeatResp = getattr(chrgMgmtData, "bmsPTCHeatResp", None)
                return bmsPTCHeatResp == 1
        return False

    @property
    def available(self):
        """Return True if the switch entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("charging") is not None
        )

    async def async_turn_on(self, **kwargs):
        """Start battery heating."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.battery_heating_long_interval

            await self._client.send_vehicle_charging_ptc_heat(self._vin, "start")
            LOGGER.info("Battery heating started for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error starting battery heating for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error starting battery heating", e)

    async def async_turn_off(self, **kwargs):
        """Stop battery heating."""
        try:
            await self._client.send_vehicle_charging_ptc_heat(self._vin, "stop")
            LOGGER.info("Battery heating stopped for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error stopping battery heating for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error stopping battery heating", e)


class SAICMGChargingPortLockSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control the charging port lock (lock/unlock)."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Charging Port Lock switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Charging Port Lock"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_charging_port_lock_switch"
        self._attr_icon = "mdi:lock"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if the charging port is locked."""
        charging_data = self.coordinator.data.get("charging")
        if charging_data:
            lock_status = getattr(
                charging_data.chrgMgmtData, "ccuEleccLckCtrlDspCmd", None
            )
            return lock_status == 1  # Assuming 1 represents locked
        return False

    async def async_turn_on(self, **kwargs):
        """Lock the charging port."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.charging_port_lock_long_interval

            await self._client.control_charging_port_lock(self._vin, unlock=False)
            LOGGER.info("Charging port locked for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error locking charging port for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error locking charging port", e)

    async def async_turn_off(self, **kwargs):
        """Unlock the charging port."""
        try:
            await self._client.control_charging_port_lock(self._vin, unlock=True)
            LOGGER.info("Charging port unlocked for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error unlocking charging port for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error unlocking charging port", e)


class SAICMGChargingSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control vehicle charging."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Charging switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Charging"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_charging_switch"
        self._attr_icon = "mdi:ev-station"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if charging is active."""
        charging_data = self.coordinator.data.get("charging")
        if charging_data:
            chrgMgmtData = getattr(charging_data, "chrgMgmtData", None)
            if chrgMgmtData:
                charging_status = getattr(chrgMgmtData, "bmsChrgSts", None)
                return charging_status in CHARGING_STATUS_CODES
        return False

    @property
    def available(self):
        """Return True if the switch entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("charging") is not None
        )

    async def async_turn_on(self, **kwargs):
        """Start charging."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.charging_long_interval

            await self._client.send_vehicle_charging_control(self._vin, "start")
            LOGGER.info("Charging started for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error starting charging for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error starting charging", e)

    async def async_turn_off(self, **kwargs):
        """Stop charging."""
        try:
            await self._client.send_vehicle_charging_control(self._vin, "stop")
            LOGGER.info("Charging stopped for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error stopping charging for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error stopping charging", e)


class SAICMGFrontDefrostSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control the front defrost."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Front Defrost switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Front Defrost"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_front_defrost_switch"
        self._attr_icon = "mdi:car-defrost-front"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if front defrost is on."""
        status = self.coordinator.data.get("status")
        if status:
            basic_status = getattr(status, "basicVehicleStatus", None)
            if basic_status:
                remote_climate_status = getattr(
                    basic_status, "remoteClimateStatus", None
                )
                return remote_climate_status == 5
        return False

    async def async_turn_on(self, **kwargs):
        """Start front defrost."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.front_defrost_long_interval

            await self._client.start_front_defrost(self._vin)
            LOGGER.info("Front defrost started for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error starting front defrost for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error starting front defrost", e)

    async def async_turn_off(self, **kwargs):
        """Stop front defrost by stopping the AC."""
        try:
            await self._client.stop_ac(self._vin)
            LOGGER.info("Front defrost stopped (AC stopped) for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error stopping front defrost for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error stopping front defrost", e)


class SAICMGHeatedSeatsSwitch(SAICMGVehicleSwitch):
    """Switch to control individual heated seats."""

    def __init__(
        self,
        coordinator,
        client,
        entry,
        vin_info,
        vin,
        seat_name,
        seat_side,
        status_attr,
    ):
        """Initialize the Heated Seat switch."""
        super().__init__(
            coordinator,
            client,
            entry,
            vin_info,
            vin,
            f"Heated Seat {seat_name}",
            "mdi:car-seat-heater",
        )
        self._seat_side = seat_side
        self._status_attr = status_attr
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Heated Seat {seat_name}"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_heated_seat_{seat_side}"
        self._attr_icon = "mdi:car-seat-heater"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if heated seats are on."""
        status = self.coordinator.data.get("status")
        if status:
            basic_status = getattr(status, "basicVehicleStatus", None)
            if basic_status:
                seat_level = getattr(
                    basic_status,
                    self._status_attr,
                    0,
                )
                return seat_level > 0
        return False

    @property
    def available(self):
        """Return True if the switch entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("status") is not None
        )

    async def async_turn_on(self, **kwargs):
        """Turn the heated seat on."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.heated_seats_long_interval

            # Fetch current levels to avoid overriding the opposite seat
            status = self.coordinator.data.get("status", {})
            basic_status = getattr(status, "basicVehicleStatus", {})
            left_level = getattr(basic_status, "frontLeftSeatHeatLevel", 0)
            right_level = getattr(basic_status, "frontRightSeatHeatLevel", 0)

            if self._seat_side == "left":
                await self._client.control_heated_seats(self._vin, 2, right_level)
            elif self._seat_side == "right":
                await self._client.control_heated_seats(self._vin, left_level, 2)

            LOGGER.info(
                "Heated seat %s turned on for VIN: %s", self._seat_side, self._vin
            )
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error(
                "Error turning on heated seat %s for VIN %s: %s",
                self._seat_side,
                self._vin,
                e,
            )
            self.coordinator.record_command_error("Error turning on heated seat", e)

    async def async_turn_off(self, **kwargs):
        """Turn the heated seat off."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.heated_seats_long_interval

            # Fetch current levels to avoid overriding the opposite seat
            status = self.coordinator.data.get("status", {})
            basic_status = getattr(status, "basicVehicleStatus", {})
            left_level = getattr(basic_status, "frontLeftSeatHeatLevel", 0)
            right_level = getattr(basic_status, "frontRightSeatHeatLevel", 0)

            if self._seat_side == "left":
                await self._client.control_heated_seats(self._vin, 0, right_level)
            elif self._seat_side == "right":
                await self._client.control_heated_seats(self._vin, left_level, 0)

            LOGGER.info(
                "Heated seat %s turned off for VIN: %s", self._seat_side, self._vin
            )
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error(
                "Error turning off heated seat %s for VIN %s: %s",
                self._seat_side,
                self._vin,
                e,
            )
            self.coordinator.record_command_error("Error turning off heated seat", e)


class SAICMGRearWindowDefrostSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control the rear window defrost."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Rear Window Defrost switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Rear Window Defrost"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_rear_window_defrost_switch"
        self._attr_icon = "mdi:car-defrost-rear"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if rear window defrost is on."""
        status = self.coordinator.data.get("status")
        if status:
            basic_status = getattr(status, "basicVehicleStatus", None)
            if basic_status:
                rear_window_heat_status = getattr(basic_status, "rmtHtdRrWndSt", None)
                return rear_window_heat_status == 1
        return False

    async def async_turn_on(self, **kwargs):
        """Turn the rear window defrost on."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.rear_window_heat_long_interval

            await self._client.control_rear_window_heat(self._vin, "start")
            LOGGER.info("Rear window defrost turned on for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error(
                "Error turning on rear window defrost for VIN %s: %s", self._vin, e
            )
            self.coordinator.record_command_error(
                "Error turning on rear window defrost", e
            )

    async def async_turn_off(self, **kwargs):
        """Turn the rear window defrost off."""
        try:
            await self._client.control_rear_window_heat(self._vin, "stop")
            LOGGER.info("Rear window defrost turned off for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error(
                "Error turning off rear window defrost for VIN %s: %s", self._vin, e
            )
            self.coordinator.record_command_error(
                "Error turning off rear window defrost", e
            )


class SAICMGSunroofSwitch(CoordinatorEntity, SwitchEntity):
    """Switch to control the sunroof (open/close)."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Sunroof switch entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Sunroof"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_sunroof_switch"
        self._attr_icon = "mdi:car-select"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_on(self):
        """Return true if the sunroof is open."""
        status = self.coordinator.data.get("status")
        if status:
            sunroof_status = getattr(status.basicVehicleStatus, "sunroofStatus", None)
            return sunroof_status == 1
        return False

    async def async_turn_on(self, **kwargs):
        """Open the sunroof."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.sunroof_long_interval

            await self._client.control_sunroof(self._vin, "open")
            LOGGER.info("Sunroof opened for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error opening sunroof for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error opening sunroof", e)

    async def async_turn_off(self, **kwargs):
        """Close the sunroof."""
        try:
            await self._client.control_sunroof(self._vin, "close")
            LOGGER.info("Sunroof closed for VIN: %s", self._vin)
            await self.coordinator.async_request_refresh()
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error closing sunroof for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error closing sunroof", e)
