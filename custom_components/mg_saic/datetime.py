# File: datetime.py
"""Holiday Mode Until: when Holiday Mode turns itself off (#460)."""

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC date and time entities."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    if not coordinator.data.get("info"):
        LOGGER.error(
            "Vehicle info is not available. Date and time entities cannot be set up."
        )
        return
    vin_info = coordinator.vin_info
    async_add_entities(
        [SAICMGHolidayModeUntil(coordinator, entry, vin_info, vin_info.vin)]
    )


class SAICMGHolidayModeUntil(CoordinatorEntity, DateTimeEntity):
    """When Holiday Mode turns itself off.

    Asked for by @HarryFlatter (#460) so Holiday Mode can be set for a trip
    or a dealer visit and not left on. It belongs to one holiday: setting it
    doesn't turn Holiday Mode on, and turning Holiday Mode off (by hand or at
    this time) clears it. Unknown when not set: Holiday Mode then stays on
    until it's turned off.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry, vin_info, vin):
        super().__init__(coordinator)
        self._vin = vin
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Holiday Mode Until"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_holiday_mode_until"
        self._attr_icon = "mdi:calendar-end"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        return self._device_info

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return getattr(self.coordinator, "holiday_mode_until", None)

    async def async_set_value(self, value):
        try:
            await self.coordinator.async_set_holiday_until(value)
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        self.async_write_ha_state()
