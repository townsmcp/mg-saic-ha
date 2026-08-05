# File: number.py

from homeassistant.components.number import (
    NumberEntity,
    NumberMode,
    NumberDeviceClass,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from .backends import Feature
from .const import (
    DOMAIN,
    LOGGER,
)
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC number entities."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Numbers cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    number_entities = []

    # Check if the vehicle supports setting Target SOC
    vehicle_type = coordinator.vehicle_type
    if (
        vehicle_type in ["BEV", "PHEV"]
        and coordinator.supports_target_soc
        and coordinator.backend_supports(Feature.TARGET_SOC)
    ):
        number_entities.append(
            SAICMGTargetSOCNumber(coordinator, client, entry, vin_info, vin)
        )

    # Climate Target Temperature — a standalone, display-only number that
    # mirrors the climate entity's setpoint (shared via the coordinator).
    # Created for every vehicle: it makes the setpoint reachable by automations
    # and voice assistants, and changing it never sends a command on its own.
    number_entities.append(
        SAICMGClimateTargetTempNumber(coordinator, client, entry, vin_info, vin)
    )

    async_add_entities(number_entities)


class SAICMGTargetSOCNumber(CoordinatorEntity, NumberEntity):
    """Representation of a Target SOC number entity."""

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the Target SOC number entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._last_valid_value = None
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Target SOC"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_target_soc"
        self._attr_native_min_value = 40
        self._attr_native_max_value = 100
        self._attr_native_step = 10
        self._attr_native_unit_of_measurement = PERCENTAGE
        self._attr_mode = NumberMode.SLIDER

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def native_value(self):
        """Return the current target SOC value."""
        charging_data = self.coordinator.data.get("charging")
        if charging_data:
            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
            if chrg_mgmt_data:
                soc_cmd = getattr(chrg_mgmt_data, "bmsOnBdChrgTrgtSOCDspCmd", None)
                # Map the SOC command to percentage
                soc_mapping = {
                    1: 40,
                    2: 50,
                    3: 60,
                    4: 70,
                    5: 80,
                    6: 90,
                    7: 100,
                }
                soc_value = soc_mapping.get(soc_cmd)
                if soc_value is not None:
                    self._last_valid_value = soc_value
                    return soc_value
        # Return the last valid value if current data is invalid
        return self._last_valid_value

    @property
    def icon(self):
        """Return the icon based on the current SOC value."""
        if self.native_value is not None:
            if self.native_value >= 100:
                return "mdi:battery-charging-100"
            elif self.native_value >= 90:
                return "mdi:battery-charging-90"
            elif self.native_value >= 80:
                return "mdi:battery-charging-80"
            elif self.native_value >= 70:
                return "mdi:battery-charging-70"
            elif self.native_value >= 60:
                return "mdi:battery-charging-60"
            elif self.native_value >= 50:
                return "mdi:battery-charging-50"
            elif self.native_value >= 40:
                return "mdi:battery-charging-40"
            else:
                return "mdi:battery-charging-outline"
        else:
            # Return a default icon when native_value is None
            return "mdi:battery-charging-outline"

    @property
    def available(self):
        """Return True if the number entity is available."""
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("charging") is not None
        )

    async def async_set_native_value(self, value: float) -> None:
        """Set the target SOC to the specified value."""
        target_soc = int(value)
        try:
            immediate_interval = self.coordinator.after_action_delay
            long_interval = self.coordinator.target_soc_long_interval

            await self._client.set_target_soc(self._vin, target_soc)
            LOGGER.info("Set Target SOC to %d%% for VIN: %s", target_soc, self._vin)
            # Schedule data refresh
            await self.coordinator.schedule_action_refresh(
                self._vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error setting Target SOC for VIN %s: %s", self._vin, e)


class SAICMGClimateTargetTempNumber(CoordinatorEntity, NumberEntity):
    """Standalone A/C target-temperature control.

    Shares state with the climate entity via coordinator.requested_target_temp,
    so changing it in either place updates both. Display-only: changing it does
    NOT send a command — the value rides along with the next actual A/C command
    (preserving SAIC's 3-command limit). Exposing the setpoint as a plain number
    makes it reachable by automations and voice assistants that cannot set a
    climate entity's attributes directly.
    """

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the climate target temperature number entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = (
            f"{vin_info.brandName} {vin_info.modelName} Climate Target Temperature"
        )
        self._attr_unique_id = f"{entry.entry_id}_{vin}_climate_target_temp"
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
        self._attr_device_class = NumberDeviceClass.TEMPERATURE
        self._attr_mode = NumberMode.BOX
        self._attr_icon = "mdi:thermometer"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def native_min_value(self):
        """Minimum settable temperature (per-model)."""
        return float(self.coordinator.min_temp)

    @property
    def native_max_value(self):
        """Maximum settable temperature (per-model)."""
        return float(self.coordinator.max_temp)

    @property
    def native_value(self):
        """Current shared target temperature."""
        return float(self.coordinator.requested_target_temp)

    async def async_set_native_value(self, value):
        """Store the target temperature locally (no command is sent)."""
        clamped = int(
            max(
                self.coordinator.min_temp,
                min(self.coordinator.max_temp, round(value)),
            )
        )
        self.coordinator.requested_target_temp = clamped
        self.async_write_ha_state()
        # Keep the climate entity (and any other views) in sync.
        self.coordinator.async_update_listeners()
