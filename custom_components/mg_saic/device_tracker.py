# File: device_tracker.py

from homeassistant.components.device_tracker import TrackerEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN, LOGGER
from .utils import create_device_info


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the MG SAIC device tracker."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]

    try:
        if not coordinator.data.get("info"):
            LOGGER.error("Failed to retrieve vehicle info or status.")
            return

        tracker = SAICMGDeviceTracker(
            coordinator, entry, "gpsPosition", "GPS Location", data_type="status"
        )
        async_add_entities([tracker], update_before_add=True)

    except Exception as e:
        LOGGER.error("Error setting up MG SAIC device tracker: %s", e)


class SAICMGDeviceTracker(CoordinatorEntity, TrackerEntity):
    """Representation of a MG SAIC device tracker."""

    def __init__(self, coordinator, entry, field, name, data_type):
        super().__init__(coordinator)
        self._field = field
        self._name = name
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_gps"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Store last known good coordinates
        self._last_lat = None
        self._last_lon = None
        self._last_valid_heading = None

    @property
    def unique_id(self):
        """Return the unique ID of the tracker."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the tracker."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def latitude(self):
        """Return the latitude of the device."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                lat = gps_position.wayPoint.position.latitude / 1e6
                lon = gps_position.wayPoint.position.longitude / 1e6

                # If the API returns 0,0 coordinates, fallback to last known valid data
                if (lat == 0.0 and lon == 0.0) and (
                    self._last_lat is not None and self._last_lon is not None
                ):
                    return self._last_lat
                else:
                    # Update last known good latitude if not zero
                    if lat != 0.0:
                        self._last_lat = lat
                    return lat
        # Fallback if no new data or data invalid
        return self._last_lat

    @property
    def longitude(self):
        """Return the longitude of the device."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                lat = gps_position.wayPoint.position.latitude / 1e6
                lon = gps_position.wayPoint.position.longitude / 1e6

                # If the API returns 0,0 coordinates, fallback to last known valid data
                if (lat == 0.0 and lon == 0.0) and (
                    self._last_lat is not None and self._last_lon is not None
                ):
                    return self._last_lon
                else:
                    # Update last known good longitude if not zero
                    if lon != 0.0:
                        self._last_lon = lon
                    return lon
        # Fallback if no new data or data invalid
        return self._last_lon

    @property
    def elevation(self):
        """Return the altitude of the device."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                return gps_position.wayPoint.position.altitude
        return None

    @property
    def hdop(self):
        """Return the HDOP of the GPS signal."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                return gps_position.wayPoint.hdop
        return None

    @property
    def satellites(self):
        """Return the number of satellites used for the fix."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                return gps_position.wayPoint.satellites
        return None

    @property
    def heading(self):
        """Return the heading as a cardinal direction."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                speed = gps_position.wayPoint.speed
                heading = gps_position.wayPoint.heading

                if speed is not None and heading is not None and speed > 1:
                    self._last_valid_heading = heading
                    return heading
                elif self._last_valid_heading is not None:
                    return self._last_valid_heading
        return None

    @property
    def extra_state_attributes(self):
        """Return the state attributes of the device."""
        data = self.coordinator.data.get(self._data_type)
        if data:
            gps_position = getattr(data, self._field, None)
            if gps_position and gps_position.wayPoint:
                numeric_heading = self.heading
                if numeric_heading is not None:
                    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
                    cardinal_heading = directions[
                        int((numeric_heading + 22.5) / 45) % 8
                    ]
                else:
                    cardinal_heading = "Unknown"

                return {
                    "elevation": gps_position.wayPoint.position.altitude,
                    "HDOP": gps_position.wayPoint.hdop,
                    "satellites": gps_position.wayPoint.satellites,
                    "heading": cardinal_heading,
                    "raw_heading": numeric_heading,
                }
        return {}

    @property
    def source_type(self):
        """Return the source type of the device."""
        return "gps"

    @property
    def device_info(self):
        """Return device info"""
        return self._device_info
