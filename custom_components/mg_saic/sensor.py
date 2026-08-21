# File: sensor.py

from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfElectricPotential,
    UnitOfLength,
    UnitOfPressure,
    UnitOfEnergy,
    UnitOfTime,
    UnitOfPower,
    UnitOfSpeed,
)
from .backends import Feature, REGION_INDIA
from datetime import datetime, timezone

from .const import (
    DOMAIN,
    LOGGER,
    TEMP_SPIKE_BASE_TOLERANCE_C,
    TEMP_SPIKE_MAX_RATE_C_PER_S,
    MILEAGE_UINT16_SATURATION,
    VEHICLE_REACHABILITY_AWAKE,
    VEHICLE_REACHABILITY_LIKELY_ASLEEP,
    VEHICLE_REACHABILITY_UNREACHABLE,
    DATA_FRESHNESS_LIVE,
    DATA_FRESHNESS_CACHED,
    DATA_FRESHNESS_FAILED,
    PRESSURE_TO_BAR,
    DATA_DECIMAL_CORRECTION,
    DATA_DECIMAL_CORRECTION_SOC,
    CHARGING_CURRENT_FACTOR,
    CHARGING_VOLTAGE_FACTOR,
    DATA_100_DECIMAL_CORRECTION,
)
from .utils import create_device_info
from .trip_stats import compute_since_charge_efficiency


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up MG SAIC sensors."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]

    try:
        if not coordinator.data.get("info"):
            LOGGER.error("Failed to retrieve vehicle info.")
            return

        vin_info = coordinator.vin_info
        vehicle_type = coordinator.vehicle_type

        sensors = [
            # Basic Vehicle Sensors
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Ancillary Battery Voltage",
                "batteryVoltage",
                "basicVehicleStatus",
                SensorDeviceClass.VOLTAGE,
                UnitOfElectricPotential.VOLT,
                "mdi:car-battery",
                "measurement",
                DATA_DECIMAL_CORRECTION,
                "status",
            ),
            SAICMGVehicleDetailSensor(
                coordinator,
                entry,
                "Brand",
                "brandName",
                "info",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Exterior Temperature",
                "exteriorTemperature",
                "basicVehicleStatus",
                SensorDeviceClass.TEMPERATURE,
                UnitOfTemperature.CELSIUS,
                "mdi:thermometer",
                "measurement",
                1.0,
                "status",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Interior Temperature",
                "interiorTemperature",
                "basicVehicleStatus",
                SensorDeviceClass.TEMPERATURE,
                UnitOfTemperature.CELSIUS,
                "mdi:thermometer",
                "measurement",
                1.0,
                "status",
            ),
            SAICMGLastKeySeenSensor(
                coordinator,
                entry,
                "Last Key Seen",
                "mdi:key",
            ),
            SAICMGTimestampSensor(
                coordinator,
                entry,
                "Last Powered On",
                "last_powered_on",
                SensorDeviceClass.TIMESTAMP,
                "mdi:power-on",
                "last_powered_on_time",
            ),
            SAICMGTimestampSensor(
                coordinator,
                entry,
                "Last Powered Off",
                "last_powered_off",
                SensorDeviceClass.TIMESTAMP,
                "mdi:power-off",
                "last_powered_off_time",
            ),
            SAICMGLastUpdateSensor(
                coordinator,
                entry,
                "Last Update Time",
                SensorDeviceClass.TIMESTAMP,
                None,
                "mdi:update",
                None,
                None,
            ),
            SAICMGTimestampSensor(
                coordinator,
                entry,
                "Last Vehicle Activity",
                "last_vehicle_activity",
                SensorDeviceClass.TIMESTAMP,
                "mdi:car-clock",
                "last_vehicle_activity",
            ),
            SAICMGMileageSensor(
                coordinator,
                entry,
                "Mileage",
                "mileage",
                "basicVehicleStatus",
                "rvsChargeStatus",
                SensorDeviceClass.DISTANCE,
                UnitOfLength.KILOMETERS,
                "mdi:counter",
                "total_increasing",
                DATA_DECIMAL_CORRECTION,
                "status",
            ),
            SAICMGNextUpdateSensor(
                coordinator,
                entry,
                "Next Update Time",
                SensorDeviceClass.TIMESTAMP,
                None,
                "mdi:update",
                None,
                None,
            ),
            SAICMGVehicleDetailSensor(
                coordinator,
                entry,
                "Model",
                "modelName",
                "info",
            ),
            SAICMGVehicleDetailSensor(
                coordinator,
                entry,
                "Model Year",
                "modelYear",
                "info",
            ),
            SAICMGVINSensor(
                coordinator,
                entry,
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Power Mode",
                "powerMode",
                "basicVehicleStatus",
                None,
                None,
                "mdi:power-settings",
                None,
                1.0,
                "status",
            ),
            SAICMGVehicleSpeedSensor(
                coordinator,
                entry,
                "Speed",
                "speed",
                "gpsPosition",
                SensorDeviceClass.SPEED,
                UnitOfSpeed.KILOMETERS_PER_HOUR,
                "mdi:speedometer",
                "measurement",
                DATA_DECIMAL_CORRECTION,
                "status",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Tyre Pressure Front Left",
                "frontLeftTyrePressure",
                "basicVehicleStatus",
                SensorDeviceClass.PRESSURE,
                UnitOfPressure.BAR,
                "mdi:car-tire-alert",
                "measurement",
                PRESSURE_TO_BAR,
                "status",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Tyre Pressure Front Right",
                "frontRightTyrePressure",
                "basicVehicleStatus",
                SensorDeviceClass.PRESSURE,
                UnitOfPressure.BAR,
                "mdi:car-tire-alert",
                "measurement",
                PRESSURE_TO_BAR,
                "status",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Tyre Pressure Rear Left",
                "rearLeftTyrePressure",
                "basicVehicleStatus",
                SensorDeviceClass.PRESSURE,
                UnitOfPressure.BAR,
                "mdi:car-tire-alert",
                "measurement",
                PRESSURE_TO_BAR,
                "status",
            ),
            SAICMGVehicleSensor(
                coordinator,
                entry,
                "Tyre Pressure Rear Right",
                "rearRightTyrePressure",
                "basicVehicleStatus",
                SensorDeviceClass.PRESSURE,
                UnitOfPressure.BAR,
                "mdi:car-tire-alert",
                "measurement",
                PRESSURE_TO_BAR,
                "status",
            ),
        ]

        if getattr(entry, "data", {}).get("region") == REGION_INDIA:
            if getattr(vin_info, "series", None):
                sensors.append(
                    SAICMGVehicleDetailSensor(
                        coordinator,
                        entry,
                        "Series",
                        "series",
                        "info",
                    )
                )
            if getattr(vin_info, "colorName", None):
                sensors.append(
                    SAICMGVehicleDetailSensor(
                        coordinator,
                        entry,
                        "Colour",
                        "colorName",
                        "info",
                    )
                )

        if vehicle_type in ["PHEV", "HEV", "ICE"]:
            # PHEV, HEV, ICE Sensors
            sensors.append(
                SAICMGVehicleSensor(
                    coordinator,
                    entry,
                    "Fuel Level",
                    "fuelLevelPrc",
                    "basicVehicleStatus",
                    SensorDeviceClass.BATTERY,
                    PERCENTAGE,
                    "mdi:gas-station",
                    "measurement",
                    1.0,
                    "status",
                ),
            )

            sensors.append(
                SAICMGVehicleSensor(
                    coordinator,
                    entry,
                    "Fuel Range",
                    "fuelRange",
                    "basicVehicleStatus",
                    SensorDeviceClass.DISTANCE,
                    UnitOfLength.KILOMETERS,
                    "mdi:gas-station",
                    "measurement",
                    DATA_DECIMAL_CORRECTION,
                    "status",
                ),
            )

        if vehicle_type in ["BEV", "PHEV", "HEV"]:
            # BEV, PHEV, HEV Sensors
            sensors.append(
                SAICMGElectricRangeSensor(
                    coordinator,
                    entry,
                    "Electric Range",
                    "fuelRangeElec",
                    "basicVehicleStatus",
                    "rvsChargeStatus",
                    SensorDeviceClass.DISTANCE,
                    UnitOfLength.KILOMETERS,
                    "mdi:car-electric",
                    "measurement",
                    DATA_DECIMAL_CORRECTION,
                    "status",
                ),
            )

            # SOC may come from ordinary status or the charging endpoint.
            if (
                vehicle_type in ["BEV", "PHEV"]
                and coordinator.backend_supports(Feature.STATE_OF_CHARGE)
                and (
                    vehicle_type == "BEV"
                    or coordinator.backend_supports(Feature.CHARGING_DATA)
                )
            ):
                sensors.append(
                    SAICMGSOCSensor(
                        coordinator,
                        entry,
                        "State of Charge",
                        "extendedData1",
                        "bmsPackSOCDsp",
                        "basicVehicleStatus",
                        SensorDeviceClass.BATTERY,
                        PERCENTAGE,
                        "mdi:battery",
                        "measurement",
                        "charging",
                    )
                )

            # Battery capacity is only available from the separate charging
            # endpoint. HEVs do not expose that endpoint, and India does not
            # advertise it even though India BEVs report SOC in status.
            if vehicle_type in ["BEV", "PHEV"] and coordinator.backend_supports(
                Feature.CHARGING_DATA
            ):
                sensors.append(
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Total Battery Capacity",
                        "totalBatteryCapacity",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        "mdi:battery-high",
                        "total",
                        DATA_DECIMAL_CORRECTION,
                        "rvsChargeStatus",
                        "charging",
                    )
                )

        if vehicle_type in ["BEV", "PHEV"] and coordinator.backend_supports(
            Feature.CHARGING_DATA
        ):
            # BEV, PHEV Sensors
            sensors.extend(
                [
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Added Electric Range",
                        "chrgngAddedElecRng",
                        SensorDeviceClass.DISTANCE,
                        UnitOfLength.KILOMETERS,
                        "mdi:map-marker-distance",
                        "measurement",
                        DATA_DECIMAL_CORRECTION,
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGChargingCurrentSensor(
                        coordinator,
                        entry,
                        "Charging Current",
                        "bmsPackCrnt",
                        SensorDeviceClass.CURRENT,
                        "A",
                        "mdi:current-ac",
                        "measurement",
                        CHARGING_CURRENT_FACTOR,
                        "chrgMgmtData",
                        "charging",
                    ),
                ]
            )

            if coordinator.supports_charging_current_limit:
                sensors.append(
                    SAICMGChargingCurrentLimitSensor(
                        coordinator,
                        entry,
                        "Charging Current Limit",
                        "bmsAltngChrgCrntDspCmd",
                        None,
                        None,
                        "mdi:current-ac",
                        None,
                        1.0,
                        "chrgMgmtData",
                        "charging",
                    )
                )

            sensors.extend(
                [
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Charging Duration",
                        "chargingDuration",
                        SensorDeviceClass.DURATION,
                        UnitOfTime.MINUTES,
                        "mdi:timer-outline",
                        "measurement",
                        DATA_100_DECIMAL_CORRECTION,
                        "rvsChargeStatus",
                        "charging",
                    ),
                    SAICMGChargingPowerSensor(
                        coordinator,
                        entry,
                        "Charging Power",
                        SensorDeviceClass.POWER,
                        UnitOfPower.KILO_WATT,
                        "mdi:flash",
                        "measurement",
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Charging Status",
                        "bmsChrgSts",
                        None,
                        None,
                        "mdi:battery-charging",
                        None,
                        1.0,
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Charging Voltage",
                        "bmsPackVol",
                        SensorDeviceClass.VOLTAGE,
                        UnitOfElectricPotential.VOLT,
                        "mdi:flash",
                        "measurement",
                        CHARGING_VOLTAGE_FACTOR,
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Estimated Range After Charging",
                        "bmsEstdElecRng",
                        SensorDeviceClass.DISTANCE,
                        UnitOfLength.KILOMETERS,
                        "mdi:map-marker-distance",
                        "measurement",
                        1.0,
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGInstantPowerSensor(
                        coordinator,
                        entry,
                        "Instant Power",
                        SensorDeviceClass.POWER,
                        UnitOfPower.KILO_WATT,
                        "mdi:lightning-bolt-circle",
                        "measurement",
                        "chrgMgmtData",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Mileage Since Last Charge",
                        "mileageSinceLastCharge",
                        SensorDeviceClass.DISTANCE,
                        UnitOfLength.KILOMETERS,
                        "mdi:map-marker-distance",
                        "measurement",
                        DATA_DECIMAL_CORRECTION,
                        "rvsChargeStatus",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Power Usage Since Last Charge",
                        "powerUsageSinceLastCharge",
                        SensorDeviceClass.ENERGY,
                        UnitOfEnergy.KILO_WATT_HOUR,
                        "mdi:flash",
                        "total_increasing",
                        DATA_DECIMAL_CORRECTION,
                        "rvsChargeStatus",
                        "charging",
                    ),
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Remaining Charging Time",
                        "chrgngRmnngTime",
                        SensorDeviceClass.DURATION,
                        UnitOfTime.MINUTES,
                        "mdi:timer-sand",
                        "measurement",
                        1.0,
                        "chrgMgmtData",
                        "charging",
                    ),
                ]
            )

            if coordinator.supports_target_soc:
                sensors.append(
                    SAICMGChargingSensor(
                        coordinator,
                        entry,
                        "Target SOC",
                        "bmsOnBdChrgTrgtSOCDspCmd",
                        SensorDeviceClass.BATTERY,
                        PERCENTAGE,
                        "mdi:battery-charging-100",
                        "measurement",
                        1.0,
                        "chrgMgmtData",
                        "charging",
                    )
                )

        if coordinator.has_heated_seats:
            sensors.extend(
                [
                    SAICMGHeatedSeatLevelSensor(
                        coordinator,
                        entry,
                        "Front Left Heated Seat Level",
                        "frontLeftSeatHeatLevel",
                        None,
                        None,
                        "mdi:car-seat-heater",
                        None,
                        None,
                        "basicVehicleStatus",
                        "status",
                    ),
                    SAICMGHeatedSeatLevelSensor(
                        coordinator,
                        entry,
                        "Front Right Heated Seat Level",
                        "frontRightSeatHeatLevel",
                        None,
                        None,
                        "mdi:car-seat-heater",
                        None,
                        None,
                        "basicVehicleStatus",
                        "status",
                    ),
                ]
            )

        if coordinator.has_battery_heating and coordinator.backend_supports(
            Feature.BATTERY_HEATING
        ):
            sensors.append(
                SAICMGChargingSensor(
                    coordinator,
                    entry,
                    "Battery Heating Status",
                    "bmsPTCHeatResp",
                    None,
                    None,
                    "mdi:heat-wave",
                    None,
                    1.0,
                    "chrgMgmtData",
                    "charging",
                ),
            )

        if coordinator.has_steering_wheel_heat and coordinator.backend_supports(
            Feature.STEERING_WHEEL_HEAT
        ):
            sensors.append(
                SAICMGSteeringWheelHeatSensor(
                    coordinator,
                    entry,
                    "Steering Wheel Heat",
                    "steeringHeatLevel",
                    None,
                    None,
                    "mdi:steering",
                    None,
                    "basicVehicleStatus",
                    "status",
                ),
            )

        # Add sensors
        # Trip & efficiency statistics (#301). "Last Trip *" populate when a
        # drive ends (power-off); the electric/fuel split follows the drivetrain.
        sensors.append(
            SAICMGLastTripSensor(
                coordinator,
                entry,
                "Last Trip Distance",
                "distance_km",
                SensorDeviceClass.DISTANCE,
                UnitOfLength.KILOMETERS,
                "mdi:map-marker-distance",
                "measurement",
            )
        )
        if vehicle_type in ["BEV", "PHEV"]:
            sensors.append(
                SAICMGLastTripSensor(
                    coordinator,
                    entry,
                    "Last Trip Efficiency",
                    "efficiency_km_per_kWh",
                    ENERGY_DISTANCE_DEVICE_CLASS,
                    "km/kWh",
                    "mdi:gauge",
                    "measurement",
                    with_attributes=True,
                )
            )
            if coordinator.backend_supports(Feature.CHARGING_DATA):
                sensors.append(
                    SAICMGEfficiencySinceChargeSensor(coordinator, entry)
                )
        if vehicle_type in ["ICE", "HEV", "PHEV"]:
            sensors.append(
                SAICMGLastTripSensor(
                    coordinator,
                    entry,
                    "Last Trip Fuel Economy",
                    "fuel_consumption_L_per_100km",
                    None,
                    "L/100km",
                    "mdi:gas-station",
                    "measurement",
                    with_attributes=True,
                )
            )
        sensors.append(SAICMGVehicleReachabilitySensor(coordinator, entry, vin_info, vin_info.vin))
        sensors.append(SAICMGDataFreshnessSensor(coordinator, entry, vin_info, vin_info.vin))
        sensors.append(SAICMGClimateModeSensor(coordinator, entry, vin_info, vin_info.vin))

        async_add_entities(sensors, update_before_add=True)

    except Exception as e:
        LOGGER.error("Error setting up MG SAIC sensors: %s", e)


# GENERAL VEHICLE DETAIL SENSORS
class SAICMGMileageSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Mileage, uses data from both VehicleStatusResp and ChrgMgmtDataResp."""

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        status_type,
        charging_status_type,
        device_class,
        unit,
        icon,
        state_class,
        factor,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._status_type = status_type
        self._charging_status_type = charging_status_type
        self._factor = factor
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)
        self._vehicle_type = coordinator.vehicle_type

        # Retain last valid mileage so the sensor never drops to Unknown or 0
        # when the API is unavailable or returns an invalid response. Mileage is
        # monotonically increasing — it should never go backwards or to zero.
        self._last_valid_mileage: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available.

        The sensor remains available as long as we have a last known value,
        even if the current API poll failed — this prevents utility meters and
        other helpers that depend on this sensor from resetting to 0.

        For HEV vehicles, charging data is never fetched (the coordinator only
        fetches it for BEV/PHEV), so availability must not depend on it.
        Mileage for HEV comes from basicVehicleStatus which is always present.

        Likewise, some backends (e.g. India) never fetch charging data at all
        because they have no charging endpoint (INDIA_FEATURES omits
        CHARGING_DATA). For those, a BEV/PHEV must not require charging data
        either — the odometer comes from basicVehicleStatus, so requiring
        charging would leave the sensor permanently unavailable (#283).
        """
        if self._last_valid_mileage is not None:
            return True
        # No retained value yet — fall back to standard availability check
        if self._vehicle_type == "ICE":
            return (
                self.coordinator.last_update_success
                and self.coordinator.data.get("status") is not None
            )
        elif self._vehicle_type == "HEV":
            # HEV mileage comes from status only — charging data is not fetched
            # for HEV vehicles so must not be required here.
            return (
                self.coordinator.last_update_success
                and self.coordinator.data.get("status") is not None
            )
        elif self._vehicle_type in ["PHEV", "BEV"]:
            base_available = (
                self.coordinator.last_update_success
                and self.coordinator.data.get("status") is not None
            )
            # Only require charging data when the backend actually provides a
            # charging endpoint. Backends without one (India) deliver the
            # odometer via status alone, so gating on charging would deadlock
            # the sensor into permanent unavailability (#283).
            if not self.coordinator.backend_supports(Feature.CHARGING_DATA):
                return base_available
            return base_available and self.coordinator.data.get("charging") is not None
        else:
            return False

    @property
    def native_value(self):
        # First, try to get mileage from VehicleStatusResp
        data = self.coordinator.data.get("status")
        mileage = None
        if data:
            status_data = getattr(data, self._status_type, None)
            if status_data:
                mileage = getattr(status_data, self._field, None)
                # Reject zero, None, and any negative value (includes -128 sentinel).
                # Mileage is always a positive, monotonically increasing odometer
                # reading — any value <= 0 is invalid API data.
                # Also reject the uint16 saturation value (65535): basicVehicleStatus
                # mileage is a 16-bit field that sticks at 65535 once the odometer
                # passes 6553.5 km (#280). Rejecting it here falls through to the
                # wider ChrgMgmtData.mileage field below, which holds the real value.
                if (
                    mileage is None
                    or mileage <= 0
                    or mileage == MILEAGE_UINT16_SATURATION
                ):
                    if mileage == MILEAGE_UINT16_SATURATION:
                        LOGGER.debug(
                            "Mileage from status is saturated (%s); falling back "
                            "to charging-data odometer",
                            MILEAGE_UINT16_SATURATION,
                        )
                    mileage = None
                else:
                    mileage = mileage * self._factor

        # If mileage is None or invalid, try to get from ChrgMgmtDataResp
        if mileage is None:
            charging_data = self.coordinator.data.get("charging")
            if charging_data:
                charging_status_data = getattr(
                    charging_data, self._charging_status_type, None
                )
                if charging_status_data:
                    mileage = getattr(charging_status_data, self._field, None)
                    if mileage is None or mileage <= 0:
                        mileage = None
                    else:
                        mileage = mileage * self._factor

        if mileage is not None:
            # Only update retained value if the new reading is >= the last known
            # value — prevents a bad API response from moving mileage backwards.
            if (
                self._last_valid_mileage is None
                or mileage >= self._last_valid_mileage
            ):
                self._last_valid_mileage = mileage
            return self._last_valid_mileage

        # API gave nothing valid — return last known value to hold steady
        if self._last_valid_mileage is not None:
            LOGGER.debug(
                "Mileage sensor %s: API returned no valid data, "
                "retaining last known value %.1f",
                self._name,
                self._last_valid_mileage,
            )
            return self._last_valid_mileage

        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGVehicleSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MG SAIC vehicle sensor."""

    # Fields where 0 is a legitimate value and must NOT be treated as invalid.
    # These are either enums (powerMode=0 means "Off") or percentage-like values
    # where zero is a real reading.
    _ZERO_VALID_FIELDS = {"powerMode", "fuelLevelPrc", "lastKeySeen"}

    # Fields that produce mapped string values — retain the last mapped string
    # rather than a numeric value.
    _MAPPED_FIELDS = {"powerMode"}

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        status_type,
        device_class,
        unit,
        icon,
        state_class,
        factor,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._status_type = status_type
        self._factor = factor
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Per-field retention for temperature fields (keyed by field name so a
        # single class instance cannot cross-contaminate different sensors).
        self._last_valid_temperature: dict[str, float] = {}
        self._last_valid_temperature_ts: dict[str, datetime] = {}
        self._temp_spike_skipped: dict[str, bool] = {}

        # Generic last-known-good value for all other retainable numeric fields.
        # Mapped/string fields use _last_valid_mapped instead.
        self._last_valid_value: float | None = None
        self._last_valid_mapped: str | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        # If we have any retained value, keep the sensor available so dependant
        # automations/helpers do not lose their reference.
        if self._field in ["interiorTemperature", "exteriorTemperature"]:
            if self._last_valid_temperature.get(self._field) is not None:
                return True
        elif self._field in self._MAPPED_FIELDS:
            if self._last_valid_mapped is not None:
                return True
        else:
            if self._last_valid_value is not None:
                return True

        required_data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and required_data is not None

    @property
    def native_value(self):
        data = self.coordinator.data.get(self._data_type)
        if data:
            if self._data_type == "status":
                status_data = getattr(data, self._status_type, None)
                if status_data:
                    raw_value = getattr(status_data, self._field, None)
                    if raw_value is not None:
                        # -128 is the SAIC sentinel for "no valid data".
                        if raw_value == -128:
                            if self._field in [
                                "interiorTemperature",
                                "exteriorTemperature",
                            ]:
                                LOGGER.debug(
                                    "Sensor %s has invalid temperature value -128, "
                                    "retaining last valid value",
                                    self._name,
                                )
                                return self._last_valid_temperature.get(self._field)
                            else:
                                # Return last known numeric value if we have one,
                                # otherwise None — either way avoids -12.8 etc.
                                if self._last_valid_value is not None:
                                    LOGGER.debug(
                                        "Sensor %s: sentinel -128 received, "
                                        "retaining last known value %.2f",
                                        self._name,
                                        self._last_valid_value,
                                    )
                                    return self._last_valid_value
                                return None

                        # --- Temperature fields ---
                        if self._field in [
                            "interiorTemperature", "exteriorTemperature"
                        ]:
                            # Reject physically implausible readings. SAIC emits
                            # -128 as its "no data" sentinel (already handled
                            # above) but also occasionally other out-of-range
                            # garbage; retain the last valid reading rather than
                            # display nonsense.
                            if not (-60 <= raw_value <= 100):
                                LOGGER.debug(
                                    "Sensor %s: implausible temperature %s — "
                                    "retaining last valid value",
                                    self._name,
                                    raw_value,
                                )
                                return self._last_valid_temperature.get(self._field)
                            computed = raw_value * self._factor
                            # Transient-spike guard (#277). Reject a reading
                            # whose change from the last accepted value is
                            # implausible for the time elapsed (rate-based, so it
                            # also catches a small delta over a tiny interval —
                            # e.g. two readings 15 ms apart walking 19°C -> 13°C).
                            # Only ONE reading is skipped; the next is always
                            # accepted, so a genuine change is delayed at most one
                            # poll and never permanently hidden.
                            last = self._last_valid_temperature.get(self._field)
                            if last is not None and computed == last:
                                # Unchanged reading — return it without disturbing
                                # the change timestamp (so "elapsed" measures time
                                # since the value last actually changed, not since
                                # this property was last evaluated).
                                return computed
                            last_ts = self._last_valid_temperature_ts.get(self._field)
                            now = datetime.now(timezone.utc)
                            if (
                                last is not None
                                and last_ts is not None
                                and not self._temp_spike_skipped.get(self._field)
                            ):
                                elapsed = max((now - last_ts).total_seconds(), 0.0)
                                max_change = (
                                    TEMP_SPIKE_BASE_TOLERANCE_C
                                    + TEMP_SPIKE_MAX_RATE_C_PER_S * elapsed
                                )
                                if abs(computed - last) > max_change:
                                    LOGGER.debug(
                                        "Sensor %s: implausible temperature change "
                                        "%.1f°C→%.1f°C in %.2fs (max %.1f°C) — "
                                        "skipping one reading",
                                        self._name,
                                        last,
                                        computed,
                                        elapsed,
                                        max_change,
                                    )
                                    self._temp_spike_skipped[self._field] = True
                                    return last
                            self._temp_spike_skipped[self._field] = False
                            self._last_valid_temperature[self._field] = computed
                            self._last_valid_temperature_ts[self._field] = now
                            return computed

                        # --- Mapped / enum fields ---
                        if self._field == "powerMode":
                            mapped = {
                                0: "Off",
                                1: "Accessory",
                                2: "On",
                                3: "Start",
                            }.get(raw_value, f"Unknown ({raw_value})")
                            self._last_valid_mapped = mapped
                            return mapped

                        # --- Numeric fields ---
                        # Treat 0 as invalid for fields where it cannot occur
                        # in practice (e.g. tyre pressures, battery voltage,
                        # fuel range). Fields listed in _ZERO_VALID_FIELDS skip
                        # this guard.
                        if raw_value == 0 and self._field not in self._ZERO_VALID_FIELDS:
                            if self._last_valid_value is not None:
                                LOGGER.debug(
                                    "Sensor %s: zero value received (likely invalid), "
                                    "retaining last known value %.2f",
                                    self._name,
                                    self._last_valid_value,
                                )
                                return self._last_valid_value
                            return None

                        computed = raw_value * self._factor
                        self._last_valid_value = computed
                        return computed

            elif self._data_type == "charging":
                charging_data = getattr(data, self._status_type, None)
                if charging_data:
                    raw_value = getattr(charging_data, self._field, None)
                    if raw_value is not None:
                        if raw_value == -128:
                            if self._last_valid_value is not None:
                                return self._last_valid_value
                            return None
                        computed = raw_value * self._factor
                        self._last_valid_value = computed
                        return computed

            elif self._data_type == "info":
                vin_info = data[0]
                raw_value = getattr(vin_info, self._field, None)
                if raw_value is not None:
                    return raw_value

        # Fall through — return whatever we last retained
        if self._field in ["interiorTemperature", "exteriorTemperature"]:
            retained = self._last_valid_temperature.get(self._field)
            if retained is not None:
                LOGGER.debug(
                    "Sensor %s: no data from API, retaining last temperature %.1f",
                    self._name,
                    retained,
                )
            return retained
        if self._field in self._MAPPED_FIELDS:
            return self._last_valid_mapped
        if self._last_valid_value is not None:
            LOGGER.debug(
                "Sensor %s: no data from API, retaining last value %.2f",
                self._name,
                self._last_valid_value,
            )
            return self._last_valid_value
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGVehicleDetailSensor(CoordinatorEntity, SensorEntity):
    """Representation of a sensor for MG SAIC vehicle details.

    NOTE: No value retention applied — Brand, Model, Model Year are static
    metadata fields that are either present or not.  Retaining a stale name
    would never cause problems in practice, but it also adds no real benefit.
    """

    def __init__(self, coordinator, entry, name, field, data_type):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        required_data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and required_data is not None

    @property
    def native_value(self):
        data = self.coordinator.data.get(self._data_type)
        if data:
            vin_info = data[0]
            raw_value = getattr(vin_info, self._field, None)
            if raw_value is not None:
                # Apply profile override for fields known to be wrong in the API.
                # Currently only modelYear is overridden (e.g. MGS6 reports 2024
                # but launched in 2025 — there is no 2024 model year variant).
                if self._field == "modelYear":
                    override = getattr(self.coordinator, "model_year_override", None)
                    if override is not None:
                        return override
                return raw_value
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


# STATUS SENSORS
class SAICMGVINSensor(CoordinatorEntity, SensorEntity):
    """Sensor exposing the vehicle VIN.

    State: masked VIN — first 2 characters visible, middle 10 replaced with
    asterisks, last 5 visible. e.g. ``LS**********46986``.
    This keeps the manufacturer/region prefix and the unique suffix visible
    (enough to identify a specific car) without exposing the full VIN in
    dashboard screenshots, logs, or voice assistants.

    Extra state attribute ``vin_full``: the complete unmasked VIN for use
    in automations and HA services that require it as a parameter.
    """

    _attr_icon = "mdi:card-account-details"

    def __init__(self, coordinator, entry):
        """Initialise the VIN sensor."""
        super().__init__(coordinator)
        vin_info = coordinator.vin_info
        self._vin = vin_info.vin
        self._attr_unique_id = f"{entry.entry_id}_{vin_info.vin}_vin"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def name(self):
        """Return the sensor name."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} VIN"

    @property
    def native_value(self) -> str:
        """Return masked VIN as the sensor state.

        Format: first 2 chars + 10 asterisks + last 5 chars = 17 chars.
        Falls back to full VIN if the VIN is shorter than 17 characters
        (should never happen in practice but handled gracefully).
        """
        vin = self._vin
        if len(vin) == 17:
            return f"{vin[:2]}{'*' * 10}{vin[-5:]}"
        # Non-standard length — mask everything except last 4
        if len(vin) > 4:
            return f"{'*' * (len(vin) - 4)}{vin[-4:]}"
        return vin

    @property
    def extra_state_attributes(self) -> dict:
        """Expose the full unmasked VIN as an attribute.

        Use ``{{ state_attr('sensor.<name>_vin', 'vin_full') }}`` in
        automations and scripts that need to pass the VIN to HA services.
        """
        return {"vin_full": self._vin}

    @property
    def available(self) -> bool:
        """Always available — VIN is static metadata set at startup."""
        return True

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


# STATUS SENSORS
class SAICMGHeatedSeatLevelSensor(CoordinatorEntity, SensorEntity):
    """Sensor to monitor the current heating level of a heated seat.

    Retention note: 0 maps to "Off" which IS a valid/expected state, so we
    retain the last mapped string value (including "Off") rather than treating
    0 as a sentinel.  The sensor only falls back to the retained value when the
    API returns None or raises an exception.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        unit,
        icon,
        state_class,
        factor=None,
        data_source="basicVehicleStatus",
        data_type="status",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._factor = factor
        self._data_source = data_source
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_seat_heat_level"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last mapped string value.  "Off" (raw=0) is a legitimate
        # reading so we always update the retained value when raw is not None.
        self._last_valid_level: str | None = None

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_level is not None:
            return True
        data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and data is not None

    @property
    def native_value(self):
        """Return the mapped heating level value."""
        try:
            data = self.coordinator.data.get(self._data_type)
            if data:
                vehicle_status = getattr(data, self._data_source, None)
                if vehicle_status:
                    raw_value = getattr(vehicle_status, self._field, None)
                    if raw_value is not None:
                        mapped = {0: "Off", 1: "Low", 2: "Medium", 3: "High"}.get(
                            raw_value, f"Unknown ({raw_value})"
                        )
                        self._last_valid_level = mapped
                        return mapped
        except Exception as e:
            LOGGER.error("Error retrieving heated seat level for %s: %s", self._name, e)

        # Return last retained value (including "Off") when API gives nothing
        if self._last_valid_level is not None:
            LOGGER.debug(
                "Heated seat sensor %s: no data from API, retaining '%s'",
                self._name,
                self._last_valid_level,
            )
            return self._last_valid_level
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGSteeringWheelHeatSensor(CoordinatorEntity, SensorEntity):
    """Sensor to monitor the current state of the steering wheel heater.

    The iSmart app exposes steering wheel heat as a simple On/Off toggle.
    The API field steeringHeatLevel reflects the current state:
      0 = Off
      1 = On
    steeringWheelHeatFailureReason carries a fault code (0 = no fault).

    Retention: "Off" (raw=0) is a valid state — retained just like the
    heated seat sensor so the entity does not go Unknown on a bad poll.

    Control (switch) is NOT yet implemented — the RvcParamsId for steering
    wheel heat is not defined in saic-python-client-ng 0.9.3.  The param ID
    needs to be confirmed via mitmproxy capture of the iSmart app before a
    switch entity can be safely added.  See GitHub issue tracker for progress.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        unit,
        icon,
        state_class,
        data_source="basicVehicleStatus",
        data_type="status",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._data_source = data_source
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_steering_wheel_heat"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last mapped string value — "Off" (raw=0) is a legitimate
        # reading and is stored just like the heated seat sensor.
        self._last_valid_state: str | None = None

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_state is not None:
            return True
        data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and data is not None

    @property
    def native_value(self):
        """Return the current steering wheel heat state."""
        try:
            data = self.coordinator.data.get(self._data_type)
            if data:
                vehicle_status = getattr(data, self._data_source, None)
                if vehicle_status:
                    raw_value = getattr(vehicle_status, self._field, None)
                    if raw_value is not None:
                        # Level is 0 = Off, and any positive value = On. Some
                        # cars report a heat level rather than a plain 1 (the
                        # MG4 EV URBAN reports 3 when active — #243), so treat
                        # anything above 0 as On rather than "Unknown".
                        try:
                            mapped = "On" if int(raw_value) > 0 else "Off"
                        except (TypeError, ValueError):
                            mapped = f"Unknown ({raw_value})"
                        self._last_valid_state = mapped
                        return mapped
        except Exception as e:
            LOGGER.error(
                "Error retrieving steering wheel heat state for %s: %s",
                self._name,
                e,
            )

        # Return last retained value when API gives nothing
        if self._last_valid_state is not None:
            LOGGER.debug(
                "Steering wheel heat sensor %s: no data from API, retaining '%s'",
                self._name,
                self._last_valid_state,
            )
            return self._last_valid_state
        return None

    @property
    def extra_state_attributes(self):
        """Expose the failure reason code as an extra attribute."""
        try:
            data = self.coordinator.data.get(self._data_type)
            if data:
                vehicle_status = getattr(data, self._data_source, None)
                if vehicle_status:
                    failure_reason = getattr(
                        vehicle_status, "steeringWheelHeatFailureReason", None
                    )
                    if failure_reason is not None:
                        return {"failure_reason": failure_reason}
        except Exception:
            pass
        return {}

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGElectricRangeSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Electric Range, uses data from both RvsChargeStatus and VehicleStatusResp."""

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        status_type,
        charging_status_type,
        device_class,
        unit,
        icon,
        state_class,
        factor,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._status_type = status_type
        self._charging_status_type = charging_status_type
        self._factor = factor
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last valid range so the sensor does not drop to Unknown when
        # the API is temporarily unavailable. A range of 0 from the API is
        # treated as invalid (the car is not actually dead) and ignored.
        self._last_valid_range: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_range is not None:
            return True
        return self.coordinator.last_update_success and (
            self.coordinator.data.get("charging") is not None
            or self.coordinator.data.get("status") is not None
        )

    @property
    def native_value(self):
        """Return the electric range value, prioritizing charging data."""
        electric_range = None

        # For models where the API's fuelRangeElec field is known to be unreliable
        # (profile flag reliable_fuel_range_elec=False), skip the normal live-range
        # fields entirely and use imcuVehElecRng from chrgMgmtData instead.
        # This is the case for the MG HS PHEV (AS33P):
        #   fuelRangeElec = -128 (sentinel, always)
        #   bmsEstdElecRng = 120 km (fixed full-charge estimate — NOT live)
        #   imcuVehElecRng = 111 km (live, tracks SOC — this is what we want)
        # bmsEstdElecRng is correctly used by the "Estimated Range After Charging"
        # sensor, so we must not also use it here or both sensors read the same
        # fixed value regardless of current SOC.
        reliable_fuel_range = getattr(
            self.coordinator, "reliable_fuel_range_elec", True
        )

        if reliable_fuel_range:
            # Standard path: try fuelRangeElec from RvsChargeStatus first
            charging_data = self.coordinator.data.get("charging")
            if charging_data:
                charging_status_data = getattr(
                    charging_data, self._charging_status_type, None
                )
                if charging_status_data:
                    raw_value = getattr(charging_status_data, self._field, None)
                    if raw_value not in (None, 0, -128):
                        electric_range = raw_value * self._factor

            # Then try fuelRangeElec from basicVehicleStatus
            if electric_range is None:
                status_data = self.coordinator.data.get("status")
                if status_data:
                    basic_status_data = getattr(status_data, self._status_type, None)
                    if basic_status_data:
                        raw_value = getattr(basic_status_data, self._field, None)
                        if raw_value not in (None, 0, -128):
                            electric_range = raw_value * self._factor

            # Last resort for reliable models: bmsEstdElecRng as a secondary source
            if electric_range is None:
                charging_data = self.coordinator.data.get("charging")
                if charging_data:
                    chrg_mgmt = getattr(charging_data, "chrgMgmtData", None)
                    if chrg_mgmt:
                        raw_estd = getattr(chrg_mgmt, "bmsEstdElecRng", None)
                        if raw_estd not in (None, 0, -128):
                            electric_range = float(raw_estd)
                            LOGGER.debug(
                                "Electric range sensor %s: bmsEstdElecRng fallback "
                                "= %s km",
                                self._name,
                                electric_range,
                            )
        else:
            # Unreliable-fuelRangeElec path (e.g. AS33P / MG HS PHEV):
            # Use imcuVehElecRng — the live vehicle electric range that tracks SOC.
            # Do NOT use bmsEstdElecRng here; that is the fixed full-charge estimate
            # and is already shown by the "Estimated Range After Charging" sensor.
            charging_data = self.coordinator.data.get("charging")
            if charging_data:
                chrg_mgmt = getattr(charging_data, "chrgMgmtData", None)
                if chrg_mgmt:
                    raw_live = getattr(chrg_mgmt, "imcuVehElecRng", None)
                    if raw_live not in (None, 0, -128):
                        electric_range = float(raw_live)
                        LOGGER.debug(
                            "Electric range sensor %s: using imcuVehElecRng "
                            "(live SOC-tracking) = %s km",
                            self._name,
                            electric_range,
                        )

        if electric_range is not None:
            self._last_valid_range = electric_range
            return electric_range

        # API gave nothing valid — return last known value to hold steady
        if self._last_valid_range is not None:
            LOGGER.debug(
                "Electric range sensor %s: API returned no valid data, "
                "retaining last known value %.1f",
                self._name,
                self._last_valid_range,
            )
            return self._last_valid_range

        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGInstantPowerSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Instant Power when the vehicle is powered on and driving.

    Retention note: 0 kW IS a valid reading (vehicle on but not accelerating /
    regenerating).  Retention is therefore only applied when the API returns
    None or raises — not when it returns 0.  When the vehicle is not in power
    modes 2 or 3 the sensor intentionally returns 0; that explicit 0 is also
    NOT retained (it would override the last real driving value unhelpfully).
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        device_class,
        unit,
        icon,
        state_class,
        data_source="chrgMgmtData",
        data_type="charging",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._device_class = device_class
        self._unit = unit
        self._state_class = state_class
        self._icon = icon
        self._data_source = data_source
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_instant_power"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last computed power value.  Updated only when a real
        # calculation succeeds; the "not driving → 0" path does not update it.
        self._last_valid_power: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_power is not None:
            return True
        # Car unreachable / poll returned no data: report 'unknown'
        # (entity available, value None) rather than 'unavailable'. A stale
        # live reading would be misleading, so we don't retain one here; the
        # Vehicle Reachability sensor signals that the data isn't current.
        # See #238.
        return self.coordinator.last_update_success

    @property
    def native_value(self):
        """Return the state of the sensor."""
        try:
            charging_data = getattr(
                self.coordinator.data.get("charging"), self._data_source, None
            )
            if charging_data:
                # Determine whether there is real power flow to report.
                # Two active cases:
                #   1. Vehicle is driving (powerMode 2=On, 3=Start) — traction power
                #   2. V2X discharging (bmsChrgSts 13) — export power, even though
                #      powerMode will be 0 (Off) during a stationary V2X session.
                status_data = self.coordinator.data.get("status")
                power_mode = getattr(
                    getattr(status_data, "basicVehicleStatus", None),
                    "powerMode",
                    None,
                )
                bms_chrg_sts = getattr(charging_data, "bmsChrgSts", None)
                is_driving = power_mode in [2, 3]
                is_v2x = bms_chrg_sts == 13

                if is_driving or is_v2x:
                    # Get raw current and voltage
                    raw_current = getattr(charging_data, "bmsPackCrnt", None)
                    raw_voltage = getattr(charging_data, "bmsPackVol", None)

                    if raw_current is not None and raw_voltage is not None:
                        # SAIC encoding: raw < 20000 => charging (positive current),
                        # raw > 20000 => discharging/traction (negative current).
                        # Formula 1000 - (raw * factor) naturally gives:
                        #   charging:   positive kW  ✓
                        #   traction/V2X export: negative kW  ✓
                        # Note: values very close to 20000 (e.g. 20033 while coasting)
                        # are real small discharge readings — do not filter them out.
                        decoded_current = 1000 - (raw_current * CHARGING_CURRENT_FACTOR)
                        decoded_voltage = raw_voltage * CHARGING_VOLTAGE_FACTOR

                        # Power in kW — negative value indicates traction/V2X export.
                        power = round(decoded_current * decoded_voltage / 1000.0, 2)

                        # Update retention (0 kW is a valid reading)
                        self._last_valid_power = power
                        return power
                    else:
                        LOGGER.warning(
                            "Instant Power: Current or Voltage not available in charging data."
                        )
                        # Fall through to retention below
                else:
                    # No active power flow — report 0 explicitly but do NOT
                    # update the retained value.
                    return 0
            else:
                LOGGER.error("No charging data available for %s", self._name)

        except Exception as e:
            LOGGER.error(
                "Error retrieving instant power for sensor %s: %s", self._name, e
            )

        # API gave nothing usable — hold the last real power reading
        if self._last_valid_power is not None:
            LOGGER.debug(
                "Instant power sensor %s: no data from API, "
                "retaining last known value %.2f kW",
                self._name,
                self._last_valid_power,
            )
            return self._last_valid_power
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGSOCSensor(CoordinatorEntity, SensorEntity):
    """Sensor for State of Charge."""

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field_basic,
        field_charging,
        status_type,
        device_class,
        unit,
        icon,
        state_class,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field_basic = field_basic
        self._field_charging = field_charging
        self._status_type = status_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field_basic}_soc"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last valid SOC so the sensor does not drop to Unknown when the
        # API is temporarily unavailable.
        self._last_valid_soc: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_soc is not None:
            return True
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self):
        # First, try to get SOC from charging data (bmsPackSOCDsp)
        charging = self.coordinator.data.get("charging")
        soc = None
        if charging:
            charging_data = getattr(charging, "chrgMgmtData", None)
            if charging_data:
                soc = getattr(charging_data, self._field_charging, None)
                if soc is not None:
                    # -128 is the SAIC sentinel for "no valid data" — reject it
                    # before applying the decimal factor (which would produce -12.8)
                    if soc == -128:
                        soc = None
                    else:
                        soc = soc * DATA_DECIMAL_CORRECTION_SOC

        # If SOC is None or invalid, try to get from basic vehicle status
        if soc is None:
            status = self.coordinator.data.get("status")
            if status:
                status_data = getattr(status, self._status_type, None)
                if status_data:
                    soc = getattr(status_data, self._field_basic, None)
                    if soc in (-128, -1):
                        soc = None

        if soc is not None:
            self._last_valid_soc = soc
            return soc

        # API gave nothing valid — return last known value to hold steady
        if self._last_valid_soc is not None:
            LOGGER.debug(
                "SOC sensor %s: API returned no valid data, "
                "retaining last known value %.1f",
                self._name,
                self._last_valid_soc,
            )
            return self._last_valid_soc

        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


# CHARGING SENSORS
class SAICMGChargingCurrentSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MG SAIC charging current sensor.

    Retention note: 0 A IS a legitimate value (not charging / plugged but idle).
    The early-return path that explicitly sets 0 when bmsChrgSts is 0 or 5 does
    NOT update the retained value.  Retention is only updated on a successful
    current calculation, and is only returned as fallback when the API gives
    nothing usable.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        unit,
        icon,
        state_class,
        factor=None,
        data_source="chrgMgmtData",
        data_type="charging",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._device_class = device_class
        self._unit = unit
        self._state_class = state_class
        self._icon = icon
        self._factor = factor
        self._data_source = data_source
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_charge"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last computed current.  Not updated by the "not charging → 0"
        # explicit path so that the retained value reflects the last real session.
        self._last_valid_current: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_current is not None:
            return True
        # Car unreachable / poll returned no data: report 'unknown'
        # (entity available, value None) rather than 'unavailable'. A stale
        # live reading would be misleading, so we don't retain one here; the
        # Vehicle Reachability sensor signals that the data isn't current.
        # See #238.
        return self.coordinator.last_update_success

    @property
    def native_value(self):
        """Return the state of the sensor."""
        try:
            charging_data = getattr(
                self.coordinator.data.get("charging"), self._data_source, None
            )
            if charging_data:
                charging_status = getattr(charging_data, "bmsChrgSts", None)

                if charging_status in [0, 5]:
                    # Explicitly inactive (unplugged or connecting) — return 0 without updating retention
                    # Note: status 13 (V2X_DISCHARGING) is NOT suppressed here — real current flows.
                    return 0

                raw_value = getattr(charging_data, self._field, None)

                # -128 sentinel — fall through to retention
                if raw_value == -128:
                    if self._last_valid_current is not None:
                        LOGGER.debug(
                            "Charging current sensor %s: sentinel -128, "
                            "retaining %.2f A",
                            self._name,
                            self._last_valid_current,
                        )
                        return self._last_valid_current
                    return None

                if raw_value is not None and self._factor is not None:
                    # SAIC encoding: raw < 20000 => charging (positive A),
                    # raw > 20000 => discharging/V2X (negative A).
                    # Formula 1000 - (raw * factor) gives the correct sign
                    # for both cases without needing separate branch logic.
                    calculated_value = round(1000 - (raw_value * self._factor), 2)

                    # During active AC/DC charging or charge-finished state,
                    # bmsPackCrnt oscillates around 20000 and can produce small
                    # negative values (e.g. -1.90A at raw=20038). These are
                    # measurement noise — clamp to 0 rather than show negative.
                    # V2X discharge (status 13) is intentionally excluded so
                    # genuine negative discharge current is preserved.
                    if calculated_value < 0 and charging_status in {1, 2, 3, 9, 10, 12}:
                        calculated_value = 0.0

                    self._last_valid_current = calculated_value
                    return calculated_value
                else:
                    return None
            else:
                LOGGER.error("No charging data available for %s", self._name)

        except Exception as e:
            LOGGER.error(
                "Error retrieving charging current sensor %s: %s",
                self._name,
                e,
                exc_info=True,
            )

        # API gave nothing usable — hold the last real reading
        if self._last_valid_current is not None:
            LOGGER.debug(
                "Charging current sensor %s: no data from API, "
                "retaining last known value %.2f A",
                self._name,
                self._last_valid_current,
            )
            return self._last_valid_current
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGChargingPowerSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Charging Power, calculated from voltage and current.

    Retention note: 0 kW IS legitimate (plugged but not actively charging).
    Same retention strategy as SAICMGChargingCurrentSensor — the explicit
    "not charging → 0" path does not update the retained value.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        device_class,
        unit,
        icon,
        state_class,
        data_source="chrgMgmtData",
        data_type="charging",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._device_class = device_class
        self._unit = unit
        self._state_class = state_class
        self._icon = icon
        self._data_source = data_source
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_charging_power"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last computed charging power.  Not updated by the explicit
        # "not charging → 0" path.
        self._last_valid_power: float | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_power is not None:
            return True
        # Car unreachable / poll returned no data: report 'unknown'
        # (entity available, value None) rather than 'unavailable'. A stale
        # live reading would be misleading, so we don't retain one here; the
        # Vehicle Reachability sensor signals that the data isn't current.
        # See #238.
        return self.coordinator.last_update_success

    @property
    def native_value(self):
        """Return the state of the sensor."""
        try:
            charging_data = getattr(
                self.coordinator.data.get("charging"), self._data_source, None
            )
            if charging_data:
                charging_status = getattr(charging_data, "bmsChrgSts", None)

                if charging_status in [0, 5]:
                    # Explicitly inactive (unplugged or connecting) — return 0 without updating retention
                    # Note: status 13 (V2X_DISCHARGING) is NOT suppressed — real power flows.
                    return 0

                raw_current = getattr(charging_data, "bmsPackCrnt", None)
                raw_voltage = getattr(charging_data, "bmsPackVol", None)

                # Reject -128 sentinel on either input
                if raw_current == -128 or raw_voltage == -128:
                    if self._last_valid_power is not None:
                        LOGGER.debug(
                            "Charging power sensor %s: sentinel -128 in input, "
                            "retaining %.2f kW",
                            self._name,
                            self._last_valid_power,
                        )
                        return self._last_valid_power
                    return None

                if raw_current is not None and raw_voltage is not None:
                    # SAIC encoding: raw < 20000 => charging (positive current/power),
                    # raw > 20000 => discharging/V2X (negative current/power).
                    # Formula 1000 - (raw * factor) gives the correct sign for both.
                    decoded_current = 1000 - (raw_current * CHARGING_CURRENT_FACTOR)
                    decoded_voltage = raw_voltage * CHARGING_VOLTAGE_FACTOR
                    power = round(decoded_current * decoded_voltage / 1000.0, 2)

                    # During active AC/DC charging or charge-finished state,
                    # bmsPackCrnt oscillates around 20000 and can produce small
                    # negative power values. Clamp to 0.
                    # V2X discharge (status 13) excluded — negative power is correct.
                    if power < 0 and charging_status in {1, 2, 3, 9, 10, 12}:
                        power = 0.0

                    self._last_valid_power = power
                    return power
                else:
                    return None
            else:
                LOGGER.error("No charging data available for %s", self._name)

        except Exception as e:
            LOGGER.error("Error retrieving charging power sensor %s: %s", self._name, e)

        # API gave nothing usable — hold the last real reading
        if self._last_valid_power is not None:
            LOGGER.debug(
                "Charging power sensor %s: no data from API, "
                "retaining last known value %.2f kW",
                self._name,
                self._last_valid_power,
            )
            return self._last_valid_power
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGChargingSensor(CoordinatorEntity, SensorEntity):
    """Representation of a MG SAIC charging sensor.

    Retention strategy per field:

    Fields where 0 IS a legitimate value (returned explicitly when not charging):
      bmsPackVol, bmsPackCrnt, lastChargeEndingPower, bmsChrgOtptCrntReq,
      chargingDuration, chrgngRmnngTime, chrgngAddedElecRng
      → The explicit "not charging → 0" return path does NOT update retention.
        Retention only activates when the API gives nothing at all.

    Mapped/enum fields (bmsOnBdChrgTrgtSOCDspCmd, bmsChrgSts, bmsPTCHeatResp):
      → Retain last mapped string/int value; None from mapping is not retained.

    Numeric fields (bmsEstdElecRng, mileageSinceLastCharge, powerUsageSinceLastCharge,
      totalBatteryCapacity, etc.):
      → Retain last numeric result; -128 sentinel rejected before factor applied.

    totalBatteryCapacity uses coordinator.known_battery_capacity_kwh when set
      (no retention needed — the coordinator value is always authoritative).

    lastChargeEndingPower: if the coordinator has charging_capacity_correction set
      (from VEHICLE_PROFILES), the corrected result is returned so models that
      report inflated energy values (e.g. MG HS PHEV × 1/3) show correctly.
    """

    # Fields where charging_status in _INACTIVE_CHARGING_STATUSES → explicit 0 return.
    # These fields must NOT treat that explicit 0 as a retained value.
    # Status 13 (V2X_DISCHARGING) is intentionally excluded from _INACTIVE_CHARGING_STATUSES
    # — during V2X discharge, voltage and current carry real non-zero readings.
    _NOT_CHARGING_ZERO_FIELDS = {
        "bmsPackVol",
        "bmsPackCrnt",
        "lastChargeEndingPower",
        "bmsChrgOtptCrntReq",
        "chargingDuration",
        "chrgngRmnngTime",
        "chrgngAddedElecRng",
    }
    # Status codes where there is genuinely no charge/discharge activity and the
    # _NOT_CHARGING_ZERO_FIELDS above should return 0 explicitly.
    # V2X_DISCHARGING (13) is deliberately absent — it has live current/voltage data.
    _INACTIVE_CHARGING_STATUSES = frozenset({0, 5})

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        unit,
        icon,
        state_class,
        factor=None,
        data_source="chrgMgmtData",
        data_type="charging",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._device_class = device_class
        self._unit = unit
        self._state_class = state_class
        self._icon = icon
        self._factor = factor
        self._data_source = data_source
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_charge"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Generic last-known-good value.  Holds a float for numeric fields or a
        # string for mapped fields.  None until a valid value has been seen.
        self._last_valid_value: float | str | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_value is not None:
            return True
        # Car unreachable / poll returned no data: report 'unknown'
        # (entity available, value None) rather than 'unavailable'. A stale
        # live reading would be misleading, so we don't retain one here; the
        # Vehicle Reachability sensor signals that the data isn't current.
        # See #238.
        return self.coordinator.last_update_success

    @property
    def native_value(self):
        """Return the state of the sensor."""
        # Total Battery Capacity: prefer coordinator's known-good value when set.
        if self._field == "totalBatteryCapacity":
            known_capacity = getattr(
                self.coordinator, "known_battery_capacity_kwh", None
            )
            if known_capacity is not None:
                return known_capacity

        try:
            charging_data = getattr(
                self.coordinator.data.get("charging"), self._data_source, None
            )

            if charging_data:
                charging_status = getattr(charging_data, "bmsChrgSts", None)

                # --- Fields that return explicit 0 when not charging ---
                if self._field in self._NOT_CHARGING_ZERO_FIELDS:
                    if charging_status in self._INACTIVE_CHARGING_STATUSES:
                        # Explicit "not charging" zero — do NOT update retention
                        return 0
                    raw_value = getattr(charging_data, self._field, None)
                    if raw_value == -128:
                        if self._last_valid_value is not None:
                            return self._last_valid_value
                        return None
                    if raw_value is not None:
                        result = raw_value * self._factor
                        # lastChargeEndingPower / powerUsageSinceLastCharge: some
                        # models (e.g. HS PHEV) report these energy fields inflated
                        # by ~3× relative to the true kWh value. Apply the profile's
                        # charging_capacity_correction factor when set so the
                        # displayed value matches the real battery.
                        if self._field in (
                            "lastChargeEndingPower",
                            "powerUsageSinceLastCharge",
                        ):
                            correction = getattr(
                                self.coordinator, "charging_capacity_correction", None
                            )
                            if correction is not None:
                                result = result * correction
                        self._last_valid_value = result
                        return result
                    return None

                # --- Target SOC mapping ---
                elif self._field == "bmsOnBdChrgTrgtSOCDspCmd":
                    raw_value = getattr(charging_data, self._field, None)
                    mapped = {
                        1: 40,
                        2: 50,
                        3: 60,
                        4: 70,
                        5: 80,
                        6: 90,
                        7: 100,
                    }.get(raw_value)
                    if mapped is not None:
                        self._last_valid_value = mapped
                        return mapped
                    # Unknown code — fall through to retention
                    if self._last_valid_value is not None:
                        return self._last_valid_value
                    return None

                # --- Charging status string mapping ---
                elif self._field == "bmsChrgSts":
                    raw_value = getattr(charging_data, self._field, None)
                    mapped = {
                        0: "Unplugged",
                        1: "Charging (AC)",
                        2: "Charging Finished",
                        3: "Charging",
                        4: "Fault Charging",
                        5: "Connecting",
                        6: "Unrecognized Connection",
                        7: "Plugged In",
                        8: "Charging Stopped",
                        9: "Scheduled Charging",
                        10: "Charging (DC)",
                        11: "Super Offboard Charging",
                        12: "Charging",
                        13: "V2X Discharging",
                    }.get(raw_value, f"Unknown ({raw_value})" if raw_value is not None else None)
                    if mapped is not None:
                        self._last_valid_value = mapped
                        return mapped
                    if self._last_valid_value is not None:
                        return self._last_valid_value
                    return None

                # --- Battery heating status mapping ---
                elif self._field == "bmsPTCHeatResp":
                    raw_value = getattr(charging_data, self._field, None)
                    mapped = {
                        0: "Off",
                        1: "On",
                        2: "Error",
                    }.get(raw_value, f"Unknown ({raw_value})" if raw_value is not None else None)
                    if mapped is not None:
                        self._last_valid_value = mapped
                        return mapped
                    if self._last_valid_value is not None:
                        return self._last_valid_value
                    return None

                # --- Generic numeric fields ---
                else:
                    raw_value = getattr(charging_data, self._field, None)
                    if raw_value == -128:
                        if self._last_valid_value is not None:
                            LOGGER.debug(
                                "Charging sensor %s: sentinel -128, "
                                "retaining last known value",
                                self._name,
                            )
                            return self._last_valid_value
                        return None
                    if raw_value is not None:
                        result = raw_value * self._factor if self._factor is not None else raw_value
                        self._last_valid_value = result
                        return result
                    # raw_value is None — fall through to retention below

            else:
                LOGGER.error("No charging data available for %s", self._name)

        except Exception as e:
            LOGGER.error("Error retrieving charging sensor %s: %s", self._name, e)

        # API gave nothing usable — return last retained value
        if self._last_valid_value is not None:
            LOGGER.debug(
                "Charging sensor %s: no data from API, retaining last known value",
                self._name,
            )
            return self._last_valid_value
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGChargingCurrentLimitSensor(CoordinatorEntity, SensorEntity):
    """Sensor to show the charging current limit.

    Retention note: code 0 maps to "0A (Ignore)" which is a valid state —
    retention stores the mapped string and returns it on API failure.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        device_class,
        unit,
        icon,
        state_class,
        factor=None,
        data_source="chrgMgmtData",
        data_type="charging",
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._device_class = device_class
        self._unit = unit
        self._icon = icon
        self._state_class = state_class
        self._factor = factor
        self._data_source = data_source
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}_current_limit"

        self._device_info = create_device_info(coordinator, entry.entry_id)

        # Retain last mapped string.  Code 0 → "0A (Ignore)" is valid, so we
        # always store the result when the API gives a known code.
        self._last_valid_limit: str | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_limit is not None:
            return True
        required_data = self.coordinator.data.get(self._data_type)
        return self.coordinator.last_update_success and required_data is not None

    @property
    def native_value(self):
        """Return the current charging limit."""
        try:
            charging_data = getattr(
                self.coordinator.data.get("charging"), self._data_source, None
            )
            if charging_data:
                current_limit_code = getattr(charging_data, self._field, None)
                if current_limit_code is not None:
                    mapped = {
                        0: "0A (Ignore)",
                        1: "6A",
                        2: "8A",
                        3: "16A",
                        4: "Max",
                    }.get(current_limit_code, f"Unknown ({current_limit_code})")
                    self._last_valid_limit = mapped
                    return mapped

        except Exception as e:
            LOGGER.error(
                "Error retrieving charging current limit for %s: %s", self._name, e
            )

        # Return last retained value when API gives nothing
        if self._last_valid_limit is not None:
            LOGGER.debug(
                "Charging current limit sensor %s: no data from API, "
                "retaining '%s'",
                self._name,
                self._last_valid_limit,
            )
            return self._last_valid_limit
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


# LAST UPDATE TIME DATA SENSOR
class SAICMGLastUpdateSensor(CoordinatorEntity, SensorEntity):
    """Sensor to display the timestamp of the last successful data update.

    NOTE: No value retention — retaining a stale "last update" timestamp would
    be actively misleading (it would imply the data is more recent than it is).
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        device_class,
        unit,
        icon,
        state_class,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._device_class = device_class
        self._unit = unit
        self._icon = icon
        self._state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._vin_info = vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_last_update_time"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return f"{self._vin_info.brandName} {self._vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        return (
            self.coordinator.last_update_success
            and hasattr(self.coordinator, "last_update_time")
            and self.coordinator.last_update_time is not None
        )

    @property
    def native_value(self):
        """Return the timestamp of the last successful data update."""
        return self.coordinator.last_update_time

    @property
    def device_class(self):
        return self._device_class

    @property
    def icon(self):
        return self._icon

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def state_class(self):
        return self._state_class

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


# NEXT UPDATE TIME DATA SENSOR
class SAICMGNextUpdateSensor(CoordinatorEntity, SensorEntity):
    """Sensor to display the timestamp of the next scheduled data update.

    NOTE: No value retention — same rationale as SAICMGLastUpdateSensor.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        device_class,
        unit,
        icon,
        state_class,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._device_class = device_class
        self._unit = unit
        self._icon = icon
        self._state_class = state_class
        self._data_type = data_type
        vin_info = self.coordinator.vin_info
        self._vin_info = vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_next_update_time"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        return f"{self._vin_info.brandName} {self._vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        return (
            self.coordinator.last_update_success
            and hasattr(self.coordinator, "next_update_time")
            and self.coordinator.next_update_time is not None
        )

    @property
    def native_value(self):
        """Return the timestamp of the next scheduled data update."""
        return self.coordinator.next_update_time

    @property
    def device_class(self):
        return self._device_class

    @property
    def icon(self):
        return self._icon

    @property
    def native_unit_of_measurement(self):
        return self._unit

    @property
    def state_class(self):
        return self._state_class

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGLastKeySeenSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Last Key Seen.

    The SAIC API field lastKeySeen contains a raw integer that appears to be a
    key fob identifier or pairing code (e.g. 7859) rather than a timestamp or
    elapsed time.  Evidence from real vehicle logs:
      - The same value (7859) appears consistently whether the car is parked or
        actively being driven (powerMode=2), ruling out elapsed-seconds semantics.
      - The vehicleModelConfiguration entry KEYPOS indicates key-position tracking
        is a per-model capability; when KEYPOS=0 (e.g. HS PHEV) the field is 0.

    This sensor therefore displays the raw integer value as-is.  When the value
    is 0 (key not present / model does not support key tracking) it returns None
    so the sensor shows as Unknown rather than a misleading zero.

    Value retention is applied so the sensor stays available between polls.
    """

    def __init__(self, coordinator, entry, name, icon):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._attr_icon = icon
        self._attr_device_class = None
        self._attr_native_unit_of_measurement = None
        self._attr_state_class = None
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_lastKeySeen"
        self._device_info = create_device_info(coordinator, entry.entry_id)
        self._last_valid_value: int | None = None

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        if self._last_valid_value is not None:
            return True
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.get("status") is not None
        )

    @property
    def native_value(self):
        """Return the raw key ID integer, or None when 0 / unavailable."""
        try:
            status_data = self.coordinator.data.get("status")
            if not status_data:
                return self._last_valid_value

            basic = getattr(status_data, "basicVehicleStatus", None)
            if not basic:
                return self._last_valid_value

            raw = getattr(basic, "lastKeySeen", None)
            if raw is None:
                return self._last_valid_value
            if raw == 0:
                # 0 = key not present or model does not support key tracking
                return None
            self._last_valid_value = raw
            return raw

        except Exception as e:
            LOGGER.error("Error reading Last Key Seen sensor: %s", e)
            return self._last_valid_value

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGTimestampSensor(CoordinatorEntity, SensorEntity):
    """Representation of a timestamp sensor for MG SAIC vehicles.

    NOTE: No value retention — retaining a stale timestamp (last powered on/off,
    last vehicle activity) would be misleading rather than helpful.
    """

    def __init__(self, coordinator, entry, name, field, device_class, icon, attribute):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._attr_device_class = device_class
        self._attr_icon = icon
        self._attribute = attribute  # Attribute name to fetch from the coordinator
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        """Return the unique ID of the sensor."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        return self.coordinator.last_update_success and self.native_value is not None

    @property
    def native_value(self):
        """Return the timestamp value from the coordinator."""
        try:
            return getattr(self.coordinator, self._attribute, None)
        except AttributeError as e:
            LOGGER.error(
                "Error retrieving attribute '%s' for sensor '%s': %s",
                self._attribute,
                self._name,
                e,
            )
            return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGVehicleSpeedSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Vehicle Speed.

    NOTE: No value retention intentionally — a stale speed value displayed as
    "current speed" would be actively misleading (e.g. showing 80 km/h when
    the car is parked).  HA showing Unknown when data is unavailable is the
    correct behaviour here.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        field,
        status_type,
        device_class,
        unit,
        icon,
        state_class,
        factor,
        data_type,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._name = name
        self._field = field
        self._status_type = status_type
        self._factor = factor
        self._data_type = data_type
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        vin_info = self.coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_{field}"

        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        """Return the name of the sensor."""
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def available(self):
        """Return True if the entity is available."""
        # Car unreachable / poll returned no data: report 'unknown'
        # (entity available, value None) rather than 'unavailable'. A stale
        # live reading would be misleading, so we don't retain one here; the
        # Vehicle Reachability sensor signals that the data isn't current.
        # See #238.
        return self.coordinator.last_update_success

    @property
    def native_value(self):
        """Return the speed value."""
        try:
            data = self.coordinator.data.get(self._data_type)
            if data:
                gps = getattr(data, self._status_type, None)
                if gps and gps.wayPoint:
                    speed = gps.wayPoint.speed
                    if speed is not None:
                        return speed * self._factor
        except AttributeError as e:
            LOGGER.error("Error retrieving speed for sensor '%s': %s", self._name, e)
        return None

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info


class SAICMGVehicleReachabilitySensor(CoordinatorEntity, SensorEntity):
    """Multi-state sensor indicating whether the vehicle is reachable / awake.

    States: awake / likely_asleep / unreachable. See coordinator
    .vehicle_reachability for how the state is inferred. Battery voltage is
    exposed only as an attribute (evidence), never as the state driver, because
    the vehicle mis-reports its own aux voltage (issue #235).
    """

    _attr_icon = "mdi:sleep"
    _attr_device_class = SensorDeviceClass.ENUM
    # Lets Home Assistant translate the raw state values (which must stay
    # lowercase snake_case, since automations/templates match on them) into
    # friendly display labels via translations/<lang>.json -> entity.sensor.
    _attr_translation_key = "vehicle_reachability"
    _attr_options = [
        VEHICLE_REACHABILITY_AWAKE,
        VEHICLE_REACHABILITY_LIKELY_ASLEEP,
        VEHICLE_REACHABILITY_UNREACHABLE,
    ]

    def __init__(self, coordinator, entry, vin_info, vin):
        """Initialize the reachability sensor."""
        super().__init__(coordinator)
        self._vin = vin
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Reachability"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_vehicle_reachability"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def available(self):
        """Always available.

        This sensor exists to report that the vehicle can't be reached, so it
        must stay available precisely when polls are failing — otherwise it
        goes unavailable alongside every other sensor at the exact moment the
        user needs it (reported by @SteveMSJ on #238).
        """
        return True

    @property
    def native_value(self):
        """Return the current reachability state."""
        return self.coordinator.vehicle_reachability

    @property
    def extra_state_attributes(self):
        """Supporting evidence — none of this drives the state."""
        attrs = {}

        # Reported aux battery voltage (stored x10). Labelled clearly as
        # vehicle-reported and possibly inaccurate — it is evidence only.
        status = self.coordinator.data.get("status") if self.coordinator.data else None
        basic = getattr(status, "basicVehicleStatus", None)
        raw_voltage = getattr(basic, "batteryVoltage", None) if basic else None
        if isinstance(raw_voltage, (int, float)):
            attrs["reported_battery_voltage"] = round(raw_voltage / 10, 2)
        attrs["reported_battery_voltage_note"] = (
            "vehicle-reported; may be inaccurate (see issue #235)"
        )

        # Hours since the car last reported activity.
        last_activity = getattr(self.coordinator, "last_vehicle_activity", None)
        if last_activity is not None:
            from datetime import datetime, timezone

            delta = datetime.now(timezone.utc) - last_activity
            attrs["hours_since_activity"] = round(delta.total_seconds() / 3600, 1)
            attrs["last_activity"] = last_activity.isoformat()

        # Whether the last live command reported the car unreachable (code 4).
        attrs["last_command_unreachable"] = bool(
            getattr(self.coordinator, "_last_command_unreachable", False)
        )

        # Data age (how old the current status is) and whether holiday mode is on.
        last_update = getattr(self.coordinator, "last_update_time", None)
        if last_update is not None:
            from datetime import datetime, timezone

            age = datetime.now(timezone.utc) - last_update
            attrs["data_age_hours"] = round(age.total_seconds() / 3600, 1)
        attrs["holiday_mode"] = bool(getattr(self.coordinator, "holiday_mode", False))

        return attrs


class SAICMGDataFreshnessSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic sensor showing how current the last poll's data was (#238).

    A separate axis from the Reachability sensor: Reachability describes the
    car's state (awake / likely_asleep / unreachable), while this describes the
    data's state. The car can be reachable yet still return cached data.

    States: live / cached / failed. See coordinator.data_freshness.
    """

    _attr_icon = "mdi:database-clock-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Raw state values stay lowercase snake_case (automations/templates match on
    # them); translations/<lang>.json -> entity.sensor.data_freshness provides
    # the friendly display labels.
    _attr_translation_key = "data_freshness"
    _attr_options = [
        DATA_FRESHNESS_LIVE,
        DATA_FRESHNESS_CACHED,
        DATA_FRESHNESS_FAILED,
    ]

    def __init__(self, coordinator, entry, vin_info, vin):
        """Initialize the data freshness sensor."""
        super().__init__(coordinator)
        self._vin = vin
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Data Freshness"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_data_freshness"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def available(self):
        """Always available.

        Like the Reachability sensor, this must keep reporting precisely when
        polls are failing — that's when its 'failed' state is most useful — so
        it never goes unavailable alongside the telemetry sensors.
        """
        return True

    @property
    def native_value(self):
        """Return the freshness of the most recent poll (or None before the
        first cycle completes)."""
        return self.coordinator.data_freshness

    @property
    def extra_state_attributes(self):
        """When the current data was last refreshed — supporting evidence."""
        attrs = {}
        last_update = getattr(self.coordinator, "last_update_time", None)
        if last_update is not None:
            attrs["last_update"] = last_update.isoformat()
        return attrs


class SAICMGClimateModeSensor(CoordinatorEntity, SensorEntity):
    """The car's actual climate mode, decoded from remoteClimateStatus.

    Unlike the simple HVAC Status binary sensor (on / not-on), this reports
    which mode the car is actually running — off / cool / fan_only / heat /
    defrost — giving automations and voice assistants a detailed read-back that
    matches what was requested via the climate entity, A/C switch or mode
    select. Uses the same per-model status maps as the climate entity, so all
    of them agree.
    """

    _attr_icon = "mdi:air-conditioner"
    _attr_device_class = SensorDeviceClass.ENUM
    # Lets Home Assistant translate the raw (lowercase snake_case) state values
    # into friendly labels via translations/<lang>.json -> entity.sensor.
    _attr_translation_key = "climate_mode"
    _attr_options = ["off", "cool", "fan_only", "heat", "defrost", "on_local", "unknown"]

    def __init__(self, coordinator, entry, vin_info, vin):
        """Initialize the Climate Mode sensor."""
        super().__init__(coordinator)
        self._vin = vin
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Climate Mode"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_climate_mode_sensor"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def native_value(self):
        """Return the decoded climate mode (None when no status is available)."""
        return self.coordinator.climate_mode_from_status()


# ── Trip & efficiency statistics (#301) ──────────────────────────────────────

# HA's energy_distance device class (added 2025.2) makes km/kWh sensors
# user-switchable to mi/kWh or kWh/100km, exactly like the mileage sensor's
# km<->mi conversion. Requires HA >= 2025.2 (enforced via hacs.json).
ENERGY_DISTANCE_DEVICE_CLASS = SensorDeviceClass.ENERGY_DISTANCE

# Keys copied into the efficiency sensors' attributes so a single entity
# carries the full breakdown of the last trip.
_TRIP_ATTR_KEYS = (
    "distance_km",
    "distance_mi",
    "duration_s",
    "soc_used_pct",
    "energy_kWh",
    "efficiency_km_per_kWh",
    "efficiency_mi_per_kWh",
    "consumption_kWh_per_100km",
    "consumption_kWh_per_100mi",
    "fuel_used_pct",
    "fuel_used_litres",
    "fuel_consumption_L_per_100km",
    "fuel_economy_mpg_uk",
    "fuel_economy_mpg_us",
    "charged_during_park",
    "refuelled_during_park",
    "start_ts",
    "end_ts",
    "retrospective",
    "timing",
)


class SAICMGLastTripSensor(CoordinatorEntity, SensorEntity):
    """A single value from the last completed trip (#301).

    Reads ``coordinator.trip_stats.last_trip`` — populated when a drive ends
    (power-off transition). Optionally exposes the full trip breakdown as
    attributes so one entity carries distance, energy, SOC used, etc.
    """

    def __init__(
        self,
        coordinator,
        entry,
        name,
        key,
        device_class,
        unit,
        icon,
        state_class=None,
        with_attributes=False,
    ):
        super().__init__(coordinator)
        self._name = name
        self._key = key
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._with_attributes = with_attributes
        vin_info = coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_last_trip_{key}"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def device_info(self):
        return self._device_info

    def _trip(self):
        stats = getattr(self.coordinator, "trip_stats", None)
        return stats.last_trip if stats is not None else None

    @property
    def available(self):
        # Stay available and show "unknown" (rather than "unavailable") when
        # this trip has no value for the key — e.g. no efficiency because a
        # charge spanned the trip, or SOC was missing. Cleaner than dropping the
        # entity out of dashboards/history; the sensor is working, it just has
        # no value for this trip.
        return True

    @property
    def native_value(self):
        trip = self._trip()
        if trip is None:
            return None
        return trip.get(self._key)

    @property
    def extra_state_attributes(self):
        if not self._with_attributes:
            return None
        trip = self._trip()
        if not trip:
            return None
        return {k: trip.get(k) for k in _TRIP_ATTR_KEYS if trip.get(k) is not None}


class SAICMGEfficiencySinceChargeSensor(CoordinatorEntity, SensorEntity):
    """Electric efficiency since the last charge (#301).

    Derived from the API's own ``mileageSinceLastCharge`` /
    ``powerUsageSinceLastCharge`` (rvsChargeStatus), so it needs no persistence
    or trip tracking — the car provides both distance and energy since the last
    charge directly.
    """

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._name = "Efficiency Since Last Charge"
        self._attr_icon = "mdi:gauge"
        self._attr_device_class = ENERGY_DISTANCE_DEVICE_CLASS
        self._attr_native_unit_of_measurement = "km/kWh"
        self._attr_state_class = "measurement"
        vin_info = coordinator.vin_info
        self._unique_id = f"{entry.entry_id}_{vin_info.vin}_efficiency_since_charge"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def unique_id(self):
        return self._unique_id

    @property
    def name(self):
        vin_info = self.coordinator.vin_info
        return f"{vin_info.brandName} {vin_info.modelName} {self._name}"

    @property
    def device_info(self):
        return self._device_info

    def _compute(self):
        charging = self.coordinator.data.get("charging")
        rcs = getattr(charging, "rvsChargeStatus", None) if charging else None
        if rcs is None:
            return None
        dist_raw = getattr(rcs, "mileageSinceLastCharge", None)
        energy_raw = getattr(rcs, "powerUsageSinceLastCharge", None)
        if dist_raw is None or energy_raw is None:
            return None
        energy = energy_raw * DATA_DECIMAL_CORRECTION
        # Correct PHEV energy inflation (e.g. HS PHEV) so the efficiency is real
        # — mirrors the Power Usage Since Last Charge sensor. Distance untouched.
        correction = getattr(self.coordinator, "charging_capacity_correction", None)
        if correction is not None:
            energy = energy * correction
        return compute_since_charge_efficiency(
            dist_raw * DATA_DECIMAL_CORRECTION, energy
        )

    @property
    def available(self):
        # Show "unknown" (not "unavailable") when there's nothing to compute
        # yet — e.g. while charging or right after a charge, when 0 km have been
        # driven since the last charge. The sensor is working; it just has no
        # value at this moment, so keep it present rather than dropping it out.
        return True

    @property
    def native_value(self):
        result = self._compute()
        return None if result is None else result["efficiency_km_per_kWh"]

    @property
    def extra_state_attributes(self):
        return self._compute()
