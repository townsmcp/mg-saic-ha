# File: binary_sensor.py

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .backends import Feature
from .const import (
    DOMAIN,
    LOGGER,
)
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC binary sensors."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]

    try:
        if not coordinator.data.get("info"):
            LOGGER.error("Failed to retrieve vehicle info.")
            return

        vin_info = coordinator.vin_info

        # Determine if vehicle is RHD
        lrd_value = None
        for config_item in vin_info.vehicleModelConfiguration:
            if config_item.itemCode == "LRD":
                lrd_value = config_item.itemValue
                break

        is_rhd = lrd_value == "1"

        # Set door and window names based on LHD/RHD. The SAIC API reports the
        # front doors and windows as "driver"/"passenger" (driverWindow,
        # passengerWindow), NOT as left/right — so the physical side depends on
        # which side the car is driven from. On a RHD car the driver sits front
        # right, so driverWindow is the FRONT RIGHT window. Previously the front
        # windows were hardcoded to the LHD assumption (driver = left), which
        # inverted them on RHD cars (issues #235 and the related report — rear
        # windows were unaffected because they use explicit left/right fields).
        if is_rhd:
            driver_door_name = "Door Front Right"
            passenger_door_name = "Door Front Left"
            driver_window_name = "Window Front Right"
            passenger_window_name = "Window Front Left"
        else:
            driver_door_name = "Door Front Left"
            passenger_door_name = "Door Front Right"
            driver_window_name = "Window Front Left"
            passenger_window_name = "Window Front Right"

        binary_sensors = [
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Bonnet Status",
                "bonnetStatus",
                BinarySensorDeviceClass.DOOR,
                "mdi:car",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Boot Status",
                "bootStatus",
                BinarySensorDeviceClass.DOOR,
                "mdi:car-back",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Dipped Beam Status",
                "dippedBeamStatus",
                BinarySensorDeviceClass.LIGHT,
                "mdi:car-light-dimmed",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                driver_door_name,
                "driverDoor",
                BinarySensorDeviceClass.DOOR,
                "mdi:car-door",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                passenger_door_name,
                "passengerDoor",
                BinarySensorDeviceClass.DOOR,
                "mdi:car-door",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Engine Status",
                "engineStatus",
                BinarySensorDeviceClass.POWER,
                "mdi:engine",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "HVAC Status",
                "remoteClimateStatus",
                BinarySensorDeviceClass.RUNNING,
                "mdi:air-conditioner",
                "status",
            ),
            SAICMGVentilationBinarySensor(coordinator, entry),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Lock Status",
                "lockStatus",
                BinarySensorDeviceClass.LOCK,
                "mdi:car-key",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Main Beam Status",
                "mainBeamStatus",
                BinarySensorDeviceClass.LIGHT,
                "mdi:car-light-high",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Side Light Status",
                "sideLightStatus",
                BinarySensorDeviceClass.LIGHT,
                "mdi:car-parking-lights",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                "Wheel Tyre Monitor Status",
                "wheelTyreMonitorStatus",
                BinarySensorDeviceClass.PROBLEM,
                "mdi:car-tire-alert",
                "status",
            ),
            SAICMGBinarySensor(
                coordinator,
                entry,
                driver_window_name,
                "driverWindow",
                BinarySensorDeviceClass.WINDOW,
                "mdi:car-door",
                "status",
            ),
        ]

        # Front passenger window — suppressed on models that only track the
        # driver window (e.g. MG3 Hybrid / ZP22: the car returns a phantom
        # passengerWindow reading, stuck at 1, and the iSmart app itself shows
        # only the driver window). coordinator.has_front_passenger_window comes
        # from the per-model VEHICLE_PROFILES entry. See #258.
        if coordinator.has_front_passenger_window:
            binary_sensors.append(
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    passenger_window_name,
                    "passengerWindow",
                    BinarySensorDeviceClass.WINDOW,
                    "mdi:car-door",
                    "status",
                )
            )

        # Rear doors — only present on 4-door vehicles (not e.g. Cyberster EC32).
        # coordinator.has_rear_doors comes from the per-model VEHICLE_PROFILES
        # entry in const.py, not the SAIC API's own DOOR bitmask — that field
        # proved unreliable for the related WINDOW bitmask (issue #203), so
        # both are now handled the same, explicit, way.
        if coordinator.has_rear_doors:
            binary_sensors.extend([
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    "Door Rear Left",
                    "rearLeftDoor",
                    BinarySensorDeviceClass.DOOR,
                    "mdi:car-door",
                    "status",
                ),
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    "Door Rear Right",
                    "rearRightDoor",
                    BinarySensorDeviceClass.DOOR,
                    "mdi:car-door",
                    "status",
                ),
            ])

        # Rear windows — only present when the car has tracked rear windows.
        # coordinator.has_rear_windows comes from the per-model VEHICLE_PROFILES
        # entry (const.py), not the SAIC API's WINDOW bitmask. That API field
        # was found to be unreliable: MG4 and MGS5 (genuine 4-window cars)
        # report WINDOW='0000' identically to the Cyberster's soft-top, which
        # incorrectly suppressed their rear window entities (issue #203).
        if coordinator.has_rear_windows:
            binary_sensors.extend([
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    "Window Rear Left",
                    "rearLeftWindow",
                    BinarySensorDeviceClass.WINDOW,
                    "mdi:car-door",
                    "status",
                ),
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    "Window Rear Right",
                    "rearRightWindow",
                    BinarySensorDeviceClass.WINDOW,
                    "mdi:car-door",
                    "status",
                ),
            ])

        # Add charging-related binary sensors
        if coordinator.vehicle_type in ["BEV", "PHEV"] and coordinator.backend_supports(
            Feature.CHARGING_DATA
        ):
            charging_binary_sensors = [
                SAICMGChargingBinarySensor(
                    coordinator,
                    entry,
                    "Charging Gun State",
                    "chargingGunState",
                    BinarySensorDeviceClass.PLUG,
                    "mdi:ev-plug-type2",
                    "rvsChargeStatus",
                    "charging",
                ),
            ]

            binary_sensors.extend(charging_binary_sensors)

        # Sunroof status sensor — gated behind has_sunroof (off by default).
        # NOTE: on tested models (MGS6 EV) the SAIC API reports sunroofStatus=0
        # permanently regardless of the actual roof position, so this sensor is
        # non-functional there and effectively experimental. Retained for
        # existing users and possible future re-test. See switch.py for detail.
        if coordinator.has_sunroof:
            binary_sensors.append(
                SAICMGBinarySensor(
                    coordinator,
                    entry,
                    "Sunroof Status",
                    "sunroofStatus",
                    BinarySensorDeviceClass.WINDOW,
                    "mdi:car-door",
                    "status",
                ),
            )

        async_add_entities(binary_sensors, update_before_add=True)

    except Exception as e:
        LOGGER.error("Error setting up MG SAIC binary sensors: %s", e)


# GENERAL VEHICLE BINARY SENSORS
class SAICMGBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a MG SAIC binary sensor."""

    def __init__(self, coordinator, entry, name, field, device_class, icon, data_type):
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._device_class = device_class
        self._icon = icon
        self._data_type = data_type
        # Last known good state, retained so the entity does not drop to
        # 'unavailable' when a poll returns no data (e.g. the car was
        # unreachable and the status fetch failed with return code 4).
        # Mirrors the numeric-sensor retention in sensor.py. See #238.
        self._last_valid_state: bool | None = None
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_binary_sensor"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        """Return the unique ID of the binary sensor."""
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available.

        Once we have a retained last-known state, keep the entity available even
        when the current poll returned no data (e.g. the car was unreachable and
        the status fetch failed with return code 4). Staleness is surfaced
        separately by the Vehicle Reachability sensor, so dependent automations
        keep their reference instead of the entity flapping to 'unavailable'.
        Mirrors the retention already applied to the numeric sensors in
        sensor.py (reported by @SteveMSJ, #238).
        """
        if self._last_valid_state is not None:
            return True
        required_data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and required_data is not None

    @property
    def is_on(self):
        data = self.coordinator.data.get(self._data_type)
        if data:
            if self._data_type == "status":
                status_data = getattr(data, "basicVehicleStatus", None)
                if status_data:
                    value = getattr(status_data, self._field, None)
                    if value is not None:
                        if self._field == "lockStatus":
                            state = value == 0
                        else:
                            state = bool(value)
                        self._last_valid_state = state
                        return state
            elif self._data_type == "charging":
                charging_status_data = getattr(data, "rvsChargeStatus", None)
                if charging_status_data:
                    value = getattr(charging_status_data, self._field, None)
                    if value is not None:
                        state = bool(value)
                        self._last_valid_state = state
                        return state
        # No fresh reading this cycle — retain the last known state rather than
        # falsely reporting 'off' (or dropping to 'unavailable') while the car
        # is temporarily unreachable. Returns None only before the first-ever
        # successful reading, which correctly shows as unknown.
        return self._last_valid_state

    @property
    def device_class(self):
        """Return the device class of this binary sensor."""
        return self._device_class

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return self._icon

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info


class SAICMGVentilationBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor: is the vehicle currently ventilating?

    The vehicle exposes NO reliable ventilation status field. Captures showed
    remoteClimateStatus=2 reports the A/C (it stays 0 when ventilating from
    cold), and the window status cannot distinguish "ventilated" from "fully
    open". So this sensor reflects the last ventilate command sent FROM HOME
    ASSISTANT (optimistic state, tracked in the coordinator):

    - ON  after the Ventilate Windows button is pressed
    - OFF after an Open/Close Windows command, or once the windows report
      closed (having first been seen open, so the brief lag before the car
      actions ventilate doesn't switch it off prematurely)

    KNOWN GAP: ventilation triggered from the iSmart app (not from Home
    Assistant) is not reflected here — HA has no way to detect it. In that case
    the window sensors will show the windows open, but this sensor stays off.
    """

    def __init__(self, coordinator, entry):
        """Initialize the ventilation binary sensor."""
        super().__init__(coordinator)
        self._attr_device_class = BinarySensorDeviceClass.RUNNING
        self._attr_icon = "mdi:weather-windy"
        vin_info = self.coordinator.vin_info
        self._attr_unique_id = f"{entry.entry_id}_{vin_info.vin}_ventilation_binary_sensor"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} Ventilation"

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info

    @property
    def available(self):
        """Return True if the coordinator has data."""
        return self.coordinator.last_update_success

    @property
    def is_on(self):
        """True while a ventilate command sent from HA is considered active."""
        return bool(getattr(self.coordinator, "ventilation_active", False))


# CHARGING SENSORS
class SAICMGChargingBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Representation of a MG SAIC charging binary sensor."""

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        icon,
        data_source,
        data_type,
    ):
        """Initialize the charging binary sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._device_class = device_class
        self._icon = icon
        self._data_source = data_source
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_binary_sensor"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Last known good state, retained so the plug/charging state does not
        # drop to 'unavailable' when a poll returns no charging data (e.g. the
        # car was unreachable). Gun/plug state is persistent (not a live
        # measurement), so holding the last value is appropriate — mirrors the
        # status binary sensors. See #238.
        self._last_valid_state: bool | None = None

    @property
    def unique_id(self):
        """Return the unique ID of the binary sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the binary sensor."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_state is not None:
            return True
        required_data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and required_data is not None

    @property
    def is_on(self):
        """Return true if the charging gun is connected."""
        charging_data = self.coordinator.data.get(self._data_type)
        if charging_data:
            data_source = getattr(charging_data, self._data_source, None)
            if data_source:
                value = getattr(data_source, self._field, None)
                if value is not None:
                    state = bool(value)
                    self._last_valid_state = state
                    return state
        # No fresh reading — retain the last known state rather than dropping to
        # 'unavailable' while the car is temporarily unreachable. See #238.
        return self._last_valid_state

    @property
    def device_class(self):
        """Return the device class of this binary sensor."""
        return self._device_class

    @property
    def icon(self):
        """Return the icon to use in the frontend."""
        return self._icon

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info
