# File: services.py

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .backends import Feature, backend_supports
from .const import (
    DOMAIN,
    LOGGER,
    ChargeCurrentLimitOption,
    BatterySoc,
)

SERVICE_CONTROL_CHARGING_PORT_LOCK = "control_charging_port_lock"
SERVICE_CONTROL_HEATED_SEATS = "control_heated_seats"
SERVICE_CONTROL_REAR_WINDOW_HEAT = "control_rear_window_heat"
SERVICE_CONTROL_SUNROOF = "control_sunroof"
SERVICE_LOCK_VEHICLE = "lock_vehicle"
SERVICE_UNLOCK_VEHICLE = "unlock_vehicle"
SERVICE_START_AC = "start_ac"
SERVICE_STOP_AC = "stop_ac"
SERVICE_OPEN_TAILGATE = "open_tailgate"
SERVICE_SET_BATTERY_HEATING_SCHEDULE = "set_battery_heating_schedule"
SERVICE_SET_SCHEDULED_CHARGING = "set_scheduled_charging"
SERVICE_SET_CHARGING_CURRENT_LIMIT = "set_charging_current_limit"
SERVICE_SET_TARGET_SOC = "set_target_soc"
SERVICE_START_CLIMATE = "start_climate"
SERVICE_START_BATTERY_HEATING = "start_battery_heating"
SERVICE_START_CHARGING = "start_charging"
SERVICE_START_FRONT_DEFROST = "start_front_defrost"
SERVICE_STOP_BATTERY_HEATING = "stop_battery_heating"
SERVICE_STOP_CHARGING = "stop_charging"
SERVICE_TRIGGER_ALARM = "trigger_alarm"
SERVICE_UPDATE_VEHICLE_DATA = "update_vehicle_data"


SERVICE_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("action"): cv.boolean,
    }
)

SERVICE_CONTROL_HEATED_SEAT_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("left_level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
        vol.Required("right_level"): vol.All(vol.Coerce(int), vol.Range(min=0, max=3)),
    }
)

SERVICE_PORT_LOCK_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("unlock"): cv.boolean,
    }
)

SERVICE_REAR_WINDOW_DEFROST_ACTION_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("action"): vol.In(["start", "stop"]),
    }
)

SERVICE_START_AC_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("temperature"): vol.Coerce(float),
    }
)

SERVICE_START_CLIMATE_SCHEMA = None

SERVICE_SET_CHARGING_CURRENT_LIMIT_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("current_limit"): vol.In(
            [e.limit for e in ChargeCurrentLimitOption]
        ),
    }
)

SERVICE_SET_TARGET_SOC_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("target_soc"): vol.In([40, 50, 60, 70, 80, 90, 100]),
    }
)

SERVICE_SET_BATTERY_HEATING_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("enable"): cv.boolean,
        vol.Optional("start_time"): cv.time,
    }
)

SERVICE_SET_SCHEDULED_CHARGING_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("mode"): vol.In(
            ["disabled", "until_target_soc", "until_scheduled_time"]
        ),
        vol.Optional("start_time"): cv.time,
        vol.Optional("end_time"): cv.time,
    }
)

SERVICE_SUNROOF_SCHEMA = vol.Schema(
    {
        vol.Required("vin"): cv.string,
        vol.Required("should_open"): cv.boolean,
    }
)

SERVICE_VIN_SCHEMA = vol.Schema({vol.Required("vin"): cv.string})


def _get_vehicle_resources(hass: HomeAssistant, vin: str):
    """Resolve the client and coordinator for a VIN."""
    domain_data = hass.data.get(DOMAIN, {})
    client = domain_data.get("clients_by_vin", {}).get(vin)
    coordinator = domain_data.get("coordinators_by_vin", {}).get(vin)

    if client is None or coordinator is None:
        raise ValueError(f"No integration resources found for VIN {vin}")

    return client, coordinator


def _record_command_error(hass: HomeAssistant, vin: str, source: str, error) -> None:
    """Record a service-call command failure on the vehicle's coordinator.

    Service handlers assign `coordinator` inside their try block, so it may be
    unbound in the except block — this re-resolves it safely. Best-effort and
    never raises, so it can't mask the original error.

    This is what feeds the Vehicle Reachability sensor: without it, commands
    issued via SERVICE CALLS never flagged a return-code-4 failure, so the
    sensor stayed "awake" while commands were failing (see #238).
    """
    try:
        _, coordinator = _get_vehicle_resources(hass, vin)
        coordinator.record_command_error(source, error)
    except Exception:  # noqa: BLE001 - supplementary, must never break the caller
        pass


def _backend_allows(client, vin: str, feature: Feature, service: str) -> bool:
    """Return True if *vin*'s backend supports *feature*, else log and refuse.

    Service calls can target any configured VIN, including vehicles on
    backends that do not implement the requested command family (e.g. MG
    India has no charging control — issue #169).  Refusing here, before any
    client call, guarantees an unsupported command can never reach a real
    car through the services layer.
    """
    if backend_supports(client, feature):
        return True
    LOGGER.error(
        "Service %s is not supported for VIN %s: this vehicle's backend "
        "(region) does not implement '%s'.",
        service,
        vin,
        feature.value,
    )
    return False


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up services for the MG SAIC integration."""

    # Dynamically define SERVICE_START_CLIMATE_SCHEMA based on vehicle's temperature limits
    global SERVICE_START_CLIMATE_SCHEMA
    SERVICE_START_CLIMATE_SCHEMA = vol.Schema(
        {
            vol.Required("vin"): cv.string,
            vol.Required("temperature"): vol.Coerce(float),
            vol.Required("fan_speed"): vol.Coerce(int),
            vol.Optional("ac_on", default=True): cv.boolean,
        }
    )

    async def handle_lock_vehicle(call: ServiceCall) -> None:
        """Handle the lock_vehicle service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.LOCK, "lock_vehicle"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.lock_unlock_long_interval

            await client.lock_vehicle(vin)
            LOGGER.info("Vehicle locked successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error locking vehicle for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.locking_vehicle", e)

    async def handle_unlock_vehicle(call: ServiceCall) -> None:
        """Handle the unlock_vehicle service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.LOCK, "unlock_vehicle"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.lock_unlock_long_interval

            await client.unlock_vehicle(vin)
            LOGGER.info("Vehicle unlocked successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error unlocking vehicle for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.unlocking_vehicle", e)

    async def handle_start_ac(call: ServiceCall) -> None:
        """Handle the start_ac service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CLIMATE, "start_ac"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.ac_long_interval

            # Retrieve desired temperature from service call
            temperature = call.data.get("temperature")
            if temperature is None:
                LOGGER.error("No temperature provided for start_ac service.")
                return

            # Clamp the temperature within allowable range
            clamped_temp = max(
                coordinator.min_temp, min(coordinator.max_temp, temperature)
            )

            # Calculate temperature_idx using coordinator's method
            temperature_idx = coordinator.get_ac_temperature_idx(clamped_temp)

            # Call the start_ac method from api.py
            await client.start_ac(
                vin=vin,
                temperature_idx=temperature_idx,
            )
            LOGGER.info(
                "AC started successfully for VIN: %s with temperature index %d",
                vin,
                temperature_idx,
            )

            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error starting AC for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.starting_ac", e)

    async def handle_stop_ac(call: ServiceCall) -> None:
        """Handle the stop_ac service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CLIMATE, "stop_ac"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.ac_long_interval

            await client.stop_ac(vin)
            LOGGER.info("AC stopped successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error stopping AC for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.stopping_ac", e)

    async def handle_start_climate(call: ServiceCall) -> None:
        """Handle the start_climate service call."""
        vin = call.data["vin"]
        temperature = call.data["temperature"]
        fan_speed = call.data["fan_speed"]
        ac_on = call.data["ac_on"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CLIMATE, "start_climate"):
                return
            min_temp = coordinator.min_temp
            max_temp = coordinator.max_temp

            # Clamp temperature within allowable range
            clamped_temp = max(min_temp, min(max_temp, round(temperature)))

            # Calculate temperature_idx using coordinator's method
            temperature_idx = coordinator.get_ac_temperature_idx(clamped_temp)

            # Clamp fan speed within allowable range
            fan_speed = max(1, min(5, fan_speed))

            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.ac_long_interval

            await client.start_climate(
                vin=vin,
                temperature_idx=temperature_idx,
                fan_speed=fan_speed,
                ac_on=ac_on,
            )

            LOGGER.info(
                "Climate started with AC ON: %s, temperature %s°C and fan speed %s for VIN: %s",
                ac_on,
                temperature,
                fan_speed,
                vin,
            )
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error starting AC with settings for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.starting_ac_with_settings", e)

    async def handle_open_tailgate(call: ServiceCall) -> None:
        """Handle the open_tailgate service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.TAILGATE, "open_tailgate"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.tailgate_long_interval

            await client.open_tailgate(vin)
            LOGGER.info("Tailgate opened successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error opening tailgate for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.opening_tailgate", e)

    async def handle_trigger_alarm(call: ServiceCall) -> None:
        """Handle the trigger_alarm service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.FIND_MY_CAR, "trigger_alarm"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.alarm_long_interval
            await client.trigger_alarm(vin)
            LOGGER.info("Alarm triggered successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error triggering alarm for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.triggering_alarm", e)

    async def handle_start_charging(call: ServiceCall) -> None:
        """Handle the start_charging service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CHARGING_CONTROL, "start_charging"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.charging_long_interval

            LOGGER.debug(f"Sending start charging command for VIN: {vin}")
            await client.send_vehicle_charging_control(vin, "start")
            LOGGER.info(f"Charging started successfully for VIN: {vin}")
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error(f"Error starting charging for VIN {vin}: {e}")

    async def handle_stop_charging(call: ServiceCall) -> None:
        """Handle the stop_charging service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CHARGING_CONTROL, "stop_charging"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.charging_long_interval

            LOGGER.debug(f"Sending stop charging command for VIN: {vin}")
            await client.send_vehicle_charging_control(vin, "stop")
            LOGGER.info(f"Charging stopped successfully for VIN: {vin}")
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error(f"Error stopping charging for VIN {vin}: {e}")

    async def handle_start_battery_heating(call: ServiceCall) -> None:
        """Handle the start_battery_heating service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.BATTERY_HEATING, "start_battery_heating"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.battery_heating_long_interval

            LOGGER.debug(f"Sending start battery heating command for VIN: {vin}")
            await client.send_vehicle_charging_ptc_heat(vin, "start")
            LOGGER.info(f"Battery heating started successfully for VIN: {vin}")
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error(f"Error starting battery heating for VIN {vin}: {e}")

    async def handle_stop_battery_heating(call: ServiceCall) -> None:
        """Handle the stop_battery_heating service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.BATTERY_HEATING, "stop_battery_heating"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.battery_heating_long_interval

            LOGGER.debug(f"Sending stop battery heating command for VIN: {vin}")
            await client.send_vehicle_charging_ptc_heat(vin, "stop")
            LOGGER.info(f"Battery heating stopped successfully for VIN: {vin}")
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error(f"Error stopping battery heating for VIN {vin}: {e}")

    async def handle_set_battery_heating_schedule(call: ServiceCall) -> None:
        """Handle the set_battery_heating_schedule service call."""
        vin = call.data["vin"]
        enable = call.data["enable"]
        start_time = call.data.get("start_time")
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.BATTERY_HEATING, "set_battery_heating_schedule"):
                return

            if enable:
                tz = dt_util.get_default_time_zone()
                if start_time is None:
                    # Fall back to the pending value from the time entity,
                    # then the schedule last reported by the vehicle.
                    start_time = coordinator.battery_heating_pending_time
                    if start_time is None:
                        schedule = coordinator.data.get("battery_heating_schedule")
                        if schedule is not None and getattr(schedule, "startTime", 0):
                            start_time = schedule.decode_start_time(tz)
                if start_time is None:
                    LOGGER.error(
                        "Cannot enable battery heating schedule for VIN %s: "
                        "no start_time provided and no previous schedule known.",
                        vin,
                    )
                    return
                coordinator.battery_heating_pending_time = start_time
                LOGGER.debug(
                    "Enabling battery heating schedule for VIN %s at %s",
                    vin,
                    start_time,
                )
                await client.enable_battery_heating_schedule(vin, start_time, tz)
                LOGGER.info(
                    "Battery heating schedule enabled at %s for VIN: %s",
                    start_time,
                    vin,
                )
            else:
                LOGGER.debug("Disabling battery heating schedule for VIN %s", vin)
                await client.disable_battery_heating_schedule(vin)
                LOGGER.info("Battery heating schedule disabled for VIN: %s", vin)

            await coordinator.async_request_refresh()
        except Exception as e:
            LOGGER.error(
                "Error setting battery heating schedule for VIN %s: %s", vin, e
            )

    async def handle_set_scheduled_charging(call: ServiceCall) -> None:
        """Handle the set_scheduled_charging service call."""
        from saic_ismart_client_ng.api.vehicle_charging import ScheduledChargingMode

        from .time import (
            DEFAULT_SCHEDULED_CHARGING_END,
            DEFAULT_SCHEDULED_CHARGING_START,
            decode_scheduled_charging_field,
        )

        vin = call.data["vin"]
        mode_key = call.data["mode"]
        mode = {
            "disabled": ScheduledChargingMode.DISABLED,
            "until_target_soc": ScheduledChargingMode.UNTIL_CONFIGURED_SOC,
            "until_scheduled_time": ScheduledChargingMode.UNTIL_CONFIGURED_TIME,
        }[mode_key]

        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.SCHEDULED_CHARGING, "set_scheduled_charging"):
                return

            charging_data = coordinator.data.get("charging")
            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)

            start_time = call.data.get("start_time")
            if start_time is None:
                start_time = coordinator.scheduled_charging_pending_start
            if start_time is None:
                start_time = decode_scheduled_charging_field(
                    chrg_mgmt_data, "bmsReserStHourDspCmd", "bmsReserStMintueDspCmd"
                )
            if start_time is None:
                start_time = DEFAULT_SCHEDULED_CHARGING_START

            end_time = call.data.get("end_time")
            if end_time is None:
                end_time = coordinator.scheduled_charging_pending_end
            if end_time is None:
                end_time = decode_scheduled_charging_field(
                    chrg_mgmt_data, "bmsReserSpHourDspCmd", "bmsReserSpMintueDspCmd"
                )
            if end_time is None:
                end_time = DEFAULT_SCHEDULED_CHARGING_END

            # Keep the time entities in sync with what was sent.
            coordinator.scheduled_charging_pending_start = start_time
            coordinator.scheduled_charging_pending_end = end_time

            await client.set_scheduled_charging(vin, start_time, end_time, mode)
            LOGGER.info(
                "Scheduled charging set to %s (%s - %s) for VIN: %s",
                mode_key,
                start_time,
                end_time,
                vin,
            )
            await coordinator.async_request_refresh()
        except Exception as e:
            LOGGER.error("Error setting scheduled charging for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.setting_scheduled_charging", e)

    async def handle_set_charging_current(call: ServiceCall):
        """Handle the service call to set charging current."""
        vin = call.data["vin"]
        current_limit = call.data["current_limit"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CURRENT_LIMIT, "set_charging_current"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.charging_current_long_interval

            # Map the string option to the local enum
            selected_code = ChargeCurrentLimitOption.to_code(current_limit)

            # Get the current target_soc from coordinator's data
            charging_data = coordinator.data.get("charging")
            if not charging_data:
                LOGGER.error(
                    "No charging data available to set charging current limit."
                )
                return

            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
            if not chrg_mgmt_data:
                LOGGER.error(
                    "No charging management data available to set charging current limit."
                )
                return

            target_soc_value = getattr(chrg_mgmt_data, "bmsOnBdChrgTrgtSOCDspCmd", None)
            if target_soc_value is None:
                LOGGER.error(
                    "Target SOC value is not available to set charging current limit."
                )
                return

            # Map the target_soc_value to BatterySoc enum
            target_soc_enum = {
                1: BatterySoc.SOC_40,
                2: BatterySoc.SOC_50,
                3: BatterySoc.SOC_60,
                4: BatterySoc.SOC_70,
                5: BatterySoc.SOC_80,
                6: BatterySoc.SOC_90,
                7: BatterySoc.SOC_100,
            }.get(target_soc_value, None)

            if target_soc_enum is None:
                LOGGER.error(f"Unknown target SOC value: {target_soc_value}")
                raise ValueError(f"Unknown target SOC value: {target_soc_value}")

            # Set the charging current limit with target_soc
            await client.set_current_limit(vin, target_soc_enum, selected_code)
            LOGGER.info(
                "Charging current limit set to %s successfully for VIN: %s",
                current_limit,
                vin,
            )
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Failed to set charging current limit for VIN %s: %s", vin, e)

    async def handle_set_target_soc(call: ServiceCall) -> None:
        """Handle the set_target_soc service call."""
        vin = call.data["vin"]
        target_soc = call.data["target_soc"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.TARGET_SOC, "set_target_soc"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.target_soc_long_interval

            await client.set_target_soc(vin, target_soc)
            LOGGER.info("%s Target SOC set for VIN: %s", target_soc, vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error setting target SOC for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.setting_target_soc", e)

    async def handle_control_rear_window_heat(call: ServiceCall) -> None:
        """Handle the control_rear_window_heat service call."""
        vin = call.data["vin"]
        action = call.data["action"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.REAR_WINDOW_HEAT, "control_rear_window_heat"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.rear_window_heat_long_interval

            await client.control_rear_window_heat(vin, action)
            LOGGER.info("Rear window heat %sed for VIN: %s", action, vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error controlling rear window heat for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.controlling_rear_window_heat", e)

    async def handle_control_heated_seats(call: ServiceCall) -> None:
        """Handle the control_heated_seats service call."""
        vin = call.data["vin"]
        left_level = call.data["left_level"]
        right_level = call.data["right_level"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.HEATED_SEATS, "control_heated_seats"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.heated_seats_long_interval

            await client.control_heated_seats(vin, left_level, right_level)
            LOGGER.info(
                "Heated seats set: Left = %d, Right = %d for VIN: %s",
                left_level,
                right_level,
                vin,
            )
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error controlling heated seats for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.controlling_heated_seats", e)

    async def handle_start_front_defrost(call: ServiceCall) -> None:
        """Handle the start_front_defrost service call."""
        vin = call.data["vin"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.FRONT_DEFROST, "start_front_defrost"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.front_defrost_long_interval

            await client.start_front_defrost(vin)
            LOGGER.info("Front defrost started successfully for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error starting front defrost for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.starting_front_defrost", e)

    async def handle_update_vehicle_data(call: ServiceCall) -> None:
        """Handle the update_vehicle_data service call."""
        vin = call.data["vin"]
        try:
            _, coordinator = _get_vehicle_resources(hass, vin)
            await coordinator.async_request_refresh()
            LOGGER.info("Data update triggered for VIN: %s", vin)
        except Exception as e:
            LOGGER.warning("Coordinator not found for VIN %s: %s", vin, e)

    async def handle_control_sunroof(call: ServiceCall) -> None:
        vin = call.data["vin"]
        should_open = call.data["should_open"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.SUNROOF, "control_sunroof"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.sunroof_long_interval

            await client.control_sunroof(vin, should_open)
            LOGGER.info("Sunroof control action completed for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error controlling sunroof for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.controlling_sunroof", e)

    async def handle_control_charging_port_lock(call: ServiceCall) -> None:
        vin = call.data["vin"]
        unlock = call.data["unlock"]
        try:
            client, coordinator = _get_vehicle_resources(hass, vin)
            if not _backend_allows(client, vin, Feature.CHARGING_PORT_LOCK, "control_charging_port_lock"):
                return
            immediate_interval = coordinator.after_action_delay
            long_interval = coordinator.charging_port_lock_long_interval

            await client.control_charging_port_lock(vin, unlock)
            LOGGER.info("Charging port lock control action completed for VIN: %s", vin)
            await coordinator.schedule_action_refresh(
                vin,
                immediate_interval,
                long_interval,
            )
        except Exception as e:
            LOGGER.error("Error controlling charging port lock for VIN %s: %s", vin, e)
            _record_command_error(hass, vin, "service.controlling_charging_port_lock", e)

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_CHARGING_PORT_LOCK,
        handle_control_charging_port_lock,
        schema=SERVICE_PORT_LOCK_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_HEATED_SEATS,
        handle_control_heated_seats,
        schema=SERVICE_CONTROL_HEATED_SEAT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_REAR_WINDOW_HEAT,
        handle_control_rear_window_heat,
        schema=SERVICE_REAR_WINDOW_DEFROST_ACTION_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONTROL_SUNROOF,
        handle_control_sunroof,
        schema=SERVICE_SUNROOF_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOCK_VEHICLE, handle_lock_vehicle, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_TAILGATE, handle_open_tailgate, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BATTERY_HEATING_SCHEDULE,
        handle_set_battery_heating_schedule,
        schema=SERVICE_SET_BATTERY_HEATING_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULED_CHARGING,
        handle_set_scheduled_charging,
        schema=SERVICE_SET_SCHEDULED_CHARGING_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHARGING_CURRENT_LIMIT,
        handle_set_charging_current,
        schema=SERVICE_SET_CHARGING_CURRENT_LIMIT_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_TARGET_SOC,
        handle_set_target_soc,
        schema=SERVICE_SET_TARGET_SOC_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_AC,
        handle_start_ac,
        schema=SERVICE_START_AC_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_CLIMATE,
        handle_start_climate,
        schema=SERVICE_START_CLIMATE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_BATTERY_HEATING,
        handle_start_battery_heating,
        schema=SERVICE_VIN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_CHARGING, handle_start_charging, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_FRONT_DEFROST,
        handle_start_front_defrost,
        schema=SERVICE_VIN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_AC, handle_stop_ac, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_BATTERY_HEATING,
        handle_stop_battery_heating,
        schema=SERVICE_VIN_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_STOP_CHARGING, handle_stop_charging, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_TRIGGER_ALARM, handle_trigger_alarm, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_UNLOCK_VEHICLE, handle_unlock_vehicle, schema=SERVICE_VIN_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_VEHICLE_DATA,
        handle_update_vehicle_data,
        schema=SERVICE_VIN_SCHEMA,
    )

    LOGGER.info("Services registered for MG SAIC integration.")


async def async_unload_services(hass: HomeAssistant) -> None:
    """Unload MG SAIC services."""
    hass.services.async_remove(DOMAIN, SERVICE_CONTROL_CHARGING_PORT_LOCK)
    hass.services.async_remove(DOMAIN, SERVICE_CONTROL_HEATED_SEATS)
    hass.services.async_remove(DOMAIN, SERVICE_CONTROL_REAR_WINDOW_HEAT)
    hass.services.async_remove(DOMAIN, SERVICE_CONTROL_SUNROOF)
    hass.services.async_remove(DOMAIN, SERVICE_LOCK_VEHICLE)
    hass.services.async_remove(DOMAIN, SERVICE_OPEN_TAILGATE)
    hass.services.async_remove(DOMAIN, SERVICE_SET_CHARGING_CURRENT_LIMIT)
    hass.services.async_remove(DOMAIN, SERVICE_SET_TARGET_SOC)
    hass.services.async_remove(DOMAIN, SERVICE_START_AC)
    hass.services.async_remove(DOMAIN, SERVICE_START_CLIMATE)
    hass.services.async_remove(DOMAIN, SERVICE_START_BATTERY_HEATING)
    hass.services.async_remove(DOMAIN, SERVICE_START_CHARGING)
    hass.services.async_remove(DOMAIN, SERVICE_START_FRONT_DEFROST)
    hass.services.async_remove(DOMAIN, SERVICE_STOP_AC)
    hass.services.async_remove(DOMAIN, SERVICE_STOP_BATTERY_HEATING)
    hass.services.async_remove(DOMAIN, SERVICE_STOP_CHARGING)
    hass.services.async_remove(DOMAIN, SERVICE_TRIGGER_ALARM)
    hass.services.async_remove(DOMAIN, SERVICE_UNLOCK_VEHICLE)
    hass.services.async_remove(DOMAIN, SERVICE_UPDATE_VEHICLE_DATA)

    LOGGER.info("Services unregistered for MG SAIC integration.")
