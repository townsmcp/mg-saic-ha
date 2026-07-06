# File: button.py

import asyncio
from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .api import CommandsLimitReachedException
from .const import (
    DOMAIN,
    LOGGER,
)
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC buttons."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Buttons cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    buttons = [
        SAICMGTriggerAlarmButton(coordinator, client, entry, vin_info, vin),
        SAICMGUpdateDataButton(coordinator, client, entry, vin_info, vin),
        SAICMGOpenBootButton(coordinator, client, entry, vin_info, vin),
    ]

    # Window control buttons — only added when the user has enabled window
    # control for this vehicle in the integration options. Off by default, since
    # the exact command is only confirmed for the MGS6; users of other models
    # can opt in and report whether it works on their car.
    if getattr(coordinator, "has_window_control", False):
        buttons.extend(
            [
                SAICMGVentilateWindowsButton(coordinator, client, entry, vin_info, vin),
                SAICMGOpenWindowsButton(coordinator, client, entry, vin_info, vin),
                SAICMGCloseWindowsButton(coordinator, client, entry, vin_info, vin),
            ]
        )

    async_add_entities(buttons)


class SAICMGButton(CoordinatorEntity, ButtonEntity):
    """Base class for MG SAIC buttons."""

    def __init__(self, coordinator, client, entry, vin_info, vin, name, icon):
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} {name}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{vin}_{name.replace(' ', '_').lower()}_button"
        )
        self._attr_icon = icon

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    async def schedule_data_refresh(self):
        """Schedule a data refresh for the coordinator associated with the VIN."""
        coordinators_by_vin = self.hass.data[DOMAIN].get("coordinators_by_vin", {})
        coordinator = coordinators_by_vin.get(self._vin)
        if coordinator:

            async def delayed_refresh():
                await asyncio.sleep(15)  # Wait for 15 seconds
                await coordinator.async_request_refresh()

            self.hass.async_create_task(delayed_refresh())
        else:
            LOGGER.warning("Coordinator not found for VIN %s", self._vin)


class SAICMGTriggerAlarmButton(CoordinatorEntity, ButtonEntity):
    """Button to trigger the vehicle alarm."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the alarm trigger button."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Trigger Alarm"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_trigger_alarm_button"
        self._attr_icon = "mdi:alarm-light"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    async def async_press(self):
        """Handle the button press."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.alarm_long_interval

            await self._client.trigger_alarm(self._vin)
            LOGGER.info("Alarm triggered for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error triggering alarm for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error triggering alarm", e)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info


class SAICMGUpdateDataButton(CoordinatorEntity, ButtonEntity):
    """Button to manually update vehicle data."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the update data button."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Update Vehicle Data"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_update_vehicle_data_button"
        self._attr_icon = "mdi:update"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    async def async_press(self):
        """Handle the button press."""
        try:
            await self.coordinator.async_request_refresh()
            LOGGER.info("Data update triggered for VIN: %s", self._vin)
        except Exception as e:
            LOGGER.error("Error triggering data update for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error triggering data update", e)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info


class SAICMGOpenBootButton(CoordinatorEntity, ButtonEntity):
    """Button to open (release the latch of) the vehicle boot.

    Uses a ButtonEntity rather than a cover action so that the control is
    always pressable regardless of current boot state. The SAIC API performs
    a one-shot latch release — it does not support remote closing — so a
    momentary button is the correct UX.
    """

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the open boot button."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Open Boot"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_open_boot_button"
        self._attr_icon = "mdi:car-back"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    async def async_press(self):
        """Release the boot latch."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.tailgate_long_interval

            await self._client.open_tailgate(self._vin)
            LOGGER.info("Boot opened for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error opening boot for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error opening boot", e)


class SAICMGWindowButtonBase(CoordinatorEntity, ButtonEntity):
    """Base for window control buttons (ventilate / open / close).

    These issue one-shot SAIC WINDOWS commands (rvcReqType=3) acting on all
    four door windows together (the car does not accept single-window control
    via this API). A momentary ButtonEntity is used rather than a cover, because
    the commands are fire-and-forget with slow status feedback — a cover would
    misleadingly imply real-time position and stop control the API lacks. This
    also matches the existing Open Boot button's approach.

    Subclasses set _window_action ("ventilate" | "open" | "close").
    """

    _window_action: str = ""

    def __init__(self, coordinator, client, entry, vin_info, vin, name, icon):
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} {name}"
        self._attr_unique_id = (
            f"{entry.entry_id}_{vin}_{name.replace(' ', '_').lower()}_button"
        )
        self._attr_icon = icon
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    async def async_press(self):
        """Send the window command for this button's action."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.sunroof_long_interval

            await self._client.control_windows(self._vin, self._window_action)
            LOGGER.info(
                "Windows %s command sent for VIN: %s",
                self._window_action,
                self._vin,
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
                "Error sending windows %s for VIN %s: %s",
                self._window_action,
                self._vin,
                e,
            )
            self.coordinator.record_command_error(
                f"Error controlling windows ({self._window_action})", e
            )


class SAICMGVentilateWindowsButton(SAICMGWindowButtonBase):
    """Crack all four windows open a few cm, mirroring the app's Ventilation."""

    _window_action = "ventilate"

    def __init__(self, coordinator, client, entry, vin_info, vin):
        super().__init__(
            coordinator, client, entry, vin_info, vin,
            "Ventilate Windows", "mdi:car-door",
        )


class SAICMGOpenWindowsButton(SAICMGWindowButtonBase):
    """Fully open all four door windows."""

    _window_action = "open"

    def __init__(self, coordinator, client, entry, vin_info, vin):
        super().__init__(
            coordinator, client, entry, vin_info, vin,
            "Open Windows", "mdi:arrow-down-box",
        )


class SAICMGCloseWindowsButton(SAICMGWindowButtonBase):
    """Close all four door windows."""

    _window_action = "close"

    def __init__(self, coordinator, client, entry, vin_info, vin):
        super().__init__(
            coordinator, client, entry, vin_info, vin,
            "Close Windows", "mdi:arrow-up-box",
        )
