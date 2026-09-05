# File: lock.py

from homeassistant.components.lock import LockEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .api import CommandsLimitReachedException
from .const import (
    DOMAIN,
    LOGGER,
)
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC lock entity."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Lock cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    lock_entities = [
        SAICMGLockEntity(coordinator, client, entry, vin_info, vin),
    ]

    async_add_entities(lock_entities)


class SAICMGLockEntity(CoordinatorEntity, LockEntity):
    """Representation of the vehicle's lock."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the lock entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info

        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Lock"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_lock"
        self._attr_icon = "mdi:car-door-lock"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def is_locked(self):
        """Return true if the vehicle is locked."""
        status = self.coordinator.data.get("status")
        if status:
            lock_status = getattr(status.basicVehicleStatus, "lockStatus", None)
            return lock_status == 1
        return None

    @property
    def available(self):
        """Return True if the lock entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("status") is not None
        )

    async def async_lock(self, **kwargs):
        """Lock the vehicle."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.lock_unlock_long_interval

            await self._client.lock_vehicle(self._vin)
            LOGGER.info("Vehicle locked for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error locking vehicle for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error locking vehicle", e)

    async def async_unlock(self, **kwargs):
        """Unlock the vehicle."""
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.lock_unlock_long_interval

            await self._client.unlock_vehicle(self._vin)
            LOGGER.info("Vehicle unlocked for VIN: %s", self._vin)
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except CommandsLimitReachedException:
            await self.coordinator.notify_command_limit_reached(self._vin)
        except Exception as e:
            LOGGER.error("Error unlocking vehicle for VIN %s: %s", self._vin, e)
            self.coordinator.record_command_error("Error unlocking vehicle", e)
