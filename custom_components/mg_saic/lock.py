# File: lock.py

from homeassistant.components.lock import LockEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .logic import status_for_controls
from .api import CommandsLimitReachedException, VehicleNotLockedException
from .const import (
    DOMAIN,
    LOGGER,
)
from .errors import (
    is_request_rejected,
    is_vehicle_unreachable,
    raise_india_command_error,
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
        status = status_for_controls(self.coordinator)
        if status:
            lock_status = getattr(status.basicVehicleStatus, "lockStatus", None)
            return lock_status == 1
        return None

    @property
    def available(self):
        """Return True if the lock entity is available."""
        return (
            self.coordinator.last_update_success
            and status_for_controls(self.coordinator) is not None
        )

    async def async_lock(self, **kwargs):
        """Lock the vehicle."""
        await self._send(locking=True)

    async def async_unlock(self, **kwargs):
        """Unlock the vehicle."""
        await self._send(locking=False)

    async def _send(self, *, locking):
        """Send lock or unlock, and check the car before reporting a failure.

        SAIC can report a command as failed when the car carried it out
        (#488, @HarryFlatter's HS PHEV, 17 Nov 2026): an unlock was answered
        with return code 4 ("the remote control instruction failed") for 15 s,
        then code 8 "Please confirm that the vehicle status or the overall
        vehicle network status is normal.(253)" for another 15 s, so a
        "command rejected" notification fired -- but the car had unlocked, and
        he got in and drove off. The confirmation never got back through
        SAIC's servers.

        So after a code 4 or code 8, the car's status is read once, straight
        away (an MG locks itself again a minute or two after an unlock if no
        door is opened, and when it drives off), and the failure is only
        reported if the car's lock state isn't what was asked for.
        """
        verb = "lock" if locking else "unlock"
        source = f"Error {verb}ing vehicle"
        try:
            if locking:
                await self._client.lock_vehicle(self._vin)
            else:
                await self._client.unlock_vehicle(self._vin)
        except VehicleNotLockedException as err:
            await self.coordinator.notify_vehicle_not_locked(self._vin)
            raise_india_command_error(getattr(self, "_client", None), err)
            return
        except Exception as err:
            if _worth_checking_the_car(err) and await self._car_shows(locking):
                LOGGER.info(
                    "SAIC couldn't confirm the %s for VIN %s (%s), but the car "
                    "reports it %sed, so it went through.",
                    verb, self._vin, err, verb,
                )
                self.coordinator.note_command_ok()
                self.async_write_ha_state()
                return
            if isinstance(err, CommandsLimitReachedException):
                await self.coordinator.notify_command_limit_reached(self._vin)
            else:
                LOGGER.error("%s for VIN %s: %s", source, self._vin, err)
                self.coordinator.record_command_error(source, err)
            raise_india_command_error(getattr(self, "_client", None), err)
            return
        LOGGER.info("Vehicle %sed for VIN: %s", verb, self._vin)
        await self.coordinator.schedule_action_refresh(
            self._vin,
            self.coordinator.after_action_delay,
            self.coordinator.lock_unlock_long_interval,
        )

    async def _car_shows(self, locked):
        """Read the car's status now: True if it is locked (or unlocked) as asked.

        False when the read fails or the car doesn't report a lock state, so
        the failure is reported as before.
        """
        try:
            await self.coordinator.async_refresh()
        except Exception as e:  # noqa: BLE001 - only deciding what to report
            LOGGER.debug("Status check after a failed lock command failed: %s", e)
            return False
        if not getattr(self.coordinator, "last_update_success", False):
            return False
        data = getattr(self.coordinator, "data", None) or {}
        basic = getattr(data.get("status"), "basicVehicleStatus", None)
        lock_status = getattr(basic, "lockStatus", None)
        if lock_status is None:
            return False
        return (lock_status == 1) == locked


def _worth_checking_the_car(error):
    """Return code 4 (no answer from the car) or 8 (refused / not confirmed):
    the car may still have done it. Any other failure is reported as is."""
    return (
        isinstance(error, CommandsLimitReachedException)
        or is_vehicle_unreachable(error)
        or is_request_rejected(error)
    )
