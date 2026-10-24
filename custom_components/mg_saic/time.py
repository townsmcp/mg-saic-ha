# File: time.py

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import CommandsLimitReachedException, VehicleNotLockedException
from .backends import Feature
from .logic import (  # noqa: F401 - decode re-exported for select/services
    charging_data_for_controls,
    decode_scheduled_charging_field,
    hold_scheduled_charging_time,
    scheduled_charging_car_time,
    scheduled_charging_pending,
    scheduled_charging_source,
)
from .const import DOMAIN, LOGGER
from .utils import create_device_info

# Default start time offered before the vehicle has ever reported a schedule.
DEFAULT_BATTERY_HEATING_START = time(hour=6, minute=0)

# Defaults offered before the vehicle has ever reported a charging schedule.
DEFAULT_SCHEDULED_CHARGING_START = time(hour=0, minute=0)
DEFAULT_SCHEDULED_CHARGING_END = time(hour=6, minute=0)


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC time entities."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Time entities cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    time_entities = []

    if (
        coordinator.vehicle_type in ["BEV", "PHEV"]
        and coordinator.has_battery_heating
        and coordinator.backend_supports(Feature.BATTERY_HEATING)
    ):
        time_entities.append(
            SAICMGBatteryHeatingScheduleTime(coordinator, client, entry, vin_info, vin)
        )
    else:
        LOGGER.debug(f"Battery heating schedule time not created for VIN {vin}.")

    if getattr(coordinator, "quiet_hours", False):
        time_entities.extend(
            [
                SAICMGQuietHoursTime(coordinator, entry, vin_info, vin, "start"),
                SAICMGQuietHoursTime(coordinator, entry, vin_info, vin, "end"),
            ]
        )

    if coordinator.vehicle_type in ["BEV", "PHEV"] and coordinator.backend_supports(
        Feature.SCHEDULED_CHARGING
    ):
        time_entities.extend(
            [
                SAICMGScheduledChargingTime(
                    coordinator, client, entry, vin_info, vin,
                    "Start", "start",
                    "bmsReserStHourDspCmd", "bmsReserStMintueDspCmd",
                    DEFAULT_SCHEDULED_CHARGING_START,
                ),
                SAICMGScheduledChargingTime(
                    coordinator, client, entry, vin_info, vin,
                    "End", "end",
                    "bmsReserSpHourDspCmd", "bmsReserSpMintueDspCmd",
                    DEFAULT_SCHEDULED_CHARGING_END,
                ),
            ]
        )

    async_add_entities(time_entities)


class SAICMGBatteryHeatingScheduleTime(CoordinatorEntity, TimeEntity):
    """Time entity for the scheduled battery heating start time."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Battery Heating Schedule time entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Battery Heating Schedule Time"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_battery_heating_schedule_time"
        self._attr_icon = "mdi:clock-time-four-outline"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def native_value(self):
        """Return the currently scheduled start time.

        Prefers the value reported by the vehicle, falling back to a locally
        pending value the user set while the schedule was disabled.
        """
        schedule = self.coordinator.data.get("battery_heating_schedule")
        if schedule is not None:
            try:
                tz = dt_util.get_default_time_zone()
                decoded = schedule.decode_start_time(tz)
                if decoded is not None and getattr(schedule, "startTime", 0):
                    return decoded
            except Exception as e:
                LOGGER.debug(
                    "Could not decode battery heating schedule time for VIN %s: %s",
                    self._vin,
                    e,
                )
        pending = self.coordinator.battery_heating_pending_time
        return pending if pending is not None else DEFAULT_BATTERY_HEATING_START

    @property
    def available(self):
        """Return True if the entity is available."""
        return self.coordinator.last_update_success

    async def async_set_value(self, value: time) -> None:
        """Set the scheduled battery heating start time.

        If the schedule is currently enabled, the new time is pushed to the
        vehicle immediately. Otherwise it is held locally until the schedule
        switch is turned on.
        """
        self.coordinator.battery_heating_pending_time = value

        schedule = self.coordinator.data.get("battery_heating_schedule")
        is_enabled = bool(schedule is not None and getattr(schedule, "is_enabled", False))

        if is_enabled:
            try:
                await self._client.enable_battery_heating_schedule(
                    self._vin, value, dt_util.get_default_time_zone()
                )
                LOGGER.info(
                    "Battery heating schedule time updated to %s for VIN: %s",
                    value,
                    self._vin,
                )
                await self.coordinator.async_request_refresh()
            except CommandsLimitReachedException:
                await self.coordinator.notify_command_limit_reached(self._vin)
            except VehicleNotLockedException:
                await self.coordinator.notify_vehicle_not_locked(self._vin)
            except Exception as e:
                LOGGER.error(
                    "Error updating battery heating schedule time for VIN %s: %s",
                    self._vin,
                    e,
                )
                self.coordinator.record_command_error(
                    "Error updating battery heating schedule time", e
                )
        else:
            # Schedule disabled — just reflect the pending value in the UI.
            self.async_write_ha_state()


class SAICMGScheduledChargingTime(CoordinatorEntity, TimeEntity):
    """Time entity for the scheduled charging window start or end.

    Setting a value stores it locally and does NOT send a command to the
    vehicle. The pending window is applied when the Scheduled Charging Mode
    select is changed (mirroring the heated-seat level pattern), so adjusting
    both times costs a single remote command.
    """

    def __init__(
        self,
        coordinator,
        client,
        entry,
        vin_info,
        vin,
        label,
        key,
        hour_attr,
        minute_attr,
        default_value,
    ):
        """Initialize a Scheduled Charging time entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._key = key
        self._hour_attr = hour_attr
        self._minute_attr = minute_attr
        self._default_value = default_value
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Scheduled Charging {label}"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_scheduled_charging_{key}"
        self._attr_icon = (
            "mdi:battery-clock" if key == "start" else "mdi:battery-clock-outline"
        )
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    def _pending_value(self):
        return scheduled_charging_pending(self.coordinator, self._key)

    @property
    def native_value(self):
        """Return the scheduled charging time.

        A time set here takes precedence until it has been sent and the car
        has reported since (#452); otherwise the value last reported by the
        vehicle is shown, then a sensible default.
        """
        pending = self._pending_value()
        if pending is not None:
            return pending

        charging_data = charging_data_for_controls(self.coordinator)
        chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
        decoded = decode_scheduled_charging_field(
            chrg_mgmt_data, self._hour_attr, self._minute_attr
        )
        if decoded is not None:
            return decoded
        return self._default_value

    @property
    def extra_state_attributes(self):
        """Where the time shown comes from, and what the car has (#452).

        source: car (the car's own time), not_sent (set here, waiting to be
        sent with the mode), sent (sent, waiting for the car to report it),
        default (nothing from the car yet).
        """
        car = scheduled_charging_car_time(self.coordinator, self._key)
        return {
            "source": scheduled_charging_source(self.coordinator, self._key),
            "car_time": car.strftime("%H:%M") if car is not None else None,
        }

    @property
    def available(self):
        """Return True if the entity is available."""
        return (
            self.coordinator.last_update_success
            and charging_data_for_controls(self.coordinator) is not None
        )

    async def async_set_value(self, value: time) -> None:
        """Store the new time locally (applied via the mode select)."""
        hold_scheduled_charging_time(self.coordinator, self._key, value)
        LOGGER.debug(
            "Stored pending scheduled charging %s time %s for VIN %s "
            "(apply via the Scheduled Charging Mode select).",
            self._key,
            value,
            self._vin,
        )
        self.async_write_ha_state()


class SAICMGQuietHoursTime(CoordinatorEntity, TimeEntity):
    """Quiet Charging Start / Quiet Charging End (#269).

    When Quiet Charging turns on and back off. Local time, kept in the
    integration's options so it survives a restart. Only present when Quiet
    charging is ticked in the integration's options. (Quiet Hours Start /
    End in 1.4.0-beta1 to beta4.)
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, vin_info, vin, which):
        super().__init__(coordinator)
        self._which = which
        label = "Start" if which == "start" else "End"
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Quiet Charging {label}"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_quiet_charging_{which}"
        self._attr_icon = "mdi:weather-night" if which == "start" else "mdi:weather-sunny"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        return self._device_info

    @property
    def native_value(self) -> time:
        if self._which == "start":
            return self.coordinator.quiet_hours_start
        return self.coordinator.quiet_hours_end

    @property
    def available(self):
        """A local setting: available whenever the entry is."""
        return True

    async def async_set_value(self, value: time) -> None:
        await self.coordinator.async_set_quiet_hours_time(self._which, value)
        self.async_write_ha_state()
