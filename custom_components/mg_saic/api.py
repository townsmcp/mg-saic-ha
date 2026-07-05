# File: api.py

import asyncio
from saic_ismart_client_ng import SaicApi
from saic_ismart_client_ng.model import SaicApiConfiguration
from saic_ismart_client_ng.api.vehicle_charging import (
    ScheduledChargingMode,
    TargetBatteryCode,
    ChargeCurrentLimitCode as ExternalChargeCurrentLimitCode,
)
from .const import (
    DEFAULT_TENANT_ID,
    LOGGER,
    REGION_API_CODES,
    REGION_BASE_URIS,
    BatterySoc,
    ChargeCurrentLimitOption,
)
from .logic import normalize_sunroof_action


class CommandsLimitReachedException(Exception):
    """Raised when the SAIC API returns return code 8 (too many remote commands).

    The vehicle will not accept further remote commands until it is started
    with the physical key. This resets the remote command counter.
    """
    pass


class SAICMGAPIClient:
    def __init__(
        self,
        username,
        password,
        vin=None,
        username_is_email=True,
        region=None,
        country_code=None,
        custom_base_uri=None,
        region_code=None,
        tenant_id=None,
    ):
        self.username = username
        self.password = password
        self.vin = vin
        self.saic_api = None
        self.username_is_email = username_is_email
        self.country_code = country_code
        self._login_lock = asyncio.Lock()
        if region is None:
            LOGGER.debug("No region specified, defaulting to Europe.")
        self.region_name = region if region is not None else "Europe"
        # Custom endpoint support (e.g. markets on separate SAIC infrastructure).
        # When set, custom_base_uri overrides the region-derived base URI.
        self.custom_base_uri = custom_base_uri
        self.region_code = region_code
        self.tenant_id = tenant_id

    # GENERAL API HANDLING
    async def _ensure_initialized(self):
        """Ensure that the APIs are initialized and logged in."""
        if not self.saic_api or not self.saic_api.is_logged_in:
            async with self._login_lock:
                if not self.saic_api or not self.saic_api.is_logged_in:
                    await self.login()

    async def _make_api_call(self, api_call, *args, **kwargs):
        """Wrap API calls to handle token expiration, re-login, and command limits."""
        await self._ensure_initialized()
        try:
            return await api_call(*args, **kwargs)
        except Exception as e:
            error_message = str(e).lower()
            if (
                "invalid session" in error_message
                or "token expired" in error_message
                or "not logged in" in error_message
            ):
                LOGGER.warning(
                    "Token expired or session invalid, attempting to re-login."
                )
                async with self._login_lock:
                    if not self.saic_api.is_logged_in:
                        await self.login()
                try:
                    return await api_call(*args, **kwargs)
                except Exception as retry_e:
                    LOGGER.error(f"API call failed after re-login: {retry_e}")
                    raise
            elif "return code: 8" in str(e) or "too frequent" in error_message:
                LOGGER.warning(
                    "Remote command limit reached (return code 8). "
                    "Vehicle must be started with the physical key to reset the counter."
                )
                raise CommandsLimitReachedException(str(e))
            else:
                LOGGER.error(f"API call failed: {e}")
                raise

    async def login(self):
        """Authenticate with the API."""
        # Get the base_url for this region (a custom base URI takes precedence)
        base_uri = self.custom_base_uri or REGION_BASE_URIS.get(self.region_name)
        if not base_uri:
            raise ValueError(f"Base URL not defined for region: {self.region_name}")

        # Resolve the REGION header value and tenant ID. Both previously fell
        # back silently to the library's EU defaults for every region.
        region_code = self.region_code or REGION_API_CODES.get(self.region_name, "eu")
        tenant_id = self.tenant_id or DEFAULT_TENANT_ID

        config = SaicApiConfiguration(
            username=self.username,
            password=self.password,
            base_uri=base_uri,
            region=region_code,
            tenant_id=tenant_id,
            phone_country_code=self.country_code
            if not self.username_is_email
            else None,
            username_is_email=self.username_is_email,
        )
        LOGGER.debug(
            "Logging in with base URL: %s, region: %s (code: %s), tenant: %s",
            base_uri,
            self.region_name,
            region_code,
            tenant_id,
        )

        self.saic_api = await asyncio.to_thread(SaicApi, config)

        try:
            await self.saic_api.login()
            if not self.saic_api.is_logged_in:
                raise Exception("Login failed")
            LOGGER.debug("Login successful, initializing vehicle APIs.")
        except Exception as e:
            LOGGER.error("Failed to log in to MG SAIC API: %s", e)
            self.saic_api = None
            raise

    # GET VEHICLE DATA

    async def get_charging_info(self, vin: str | None = None):
        """Retrieve charging information for *vin* (defaults to self.vin).

        Accepts an explicit vin so that a shared client instance (one per
        account, shared across all VINs on that account) can fetch data for
        any of the account's vehicles rather than always using the VIN it was
        originally constructed with.  Coordinators must pass their own VIN
        explicitly to avoid all cars on the same account returning the same
        data.
        """
        target_vin = vin or self.vin
        try:
            charging_status = await self._make_api_call(
                self.saic_api.get_vehicle_charging_management_data, target_vin
            )
            return charging_status
        except Exception as e:
            LOGGER.error("Error retrieving charging information for VIN %s: %s", target_vin, e)
            return None

    async def get_vehicle_info(self):
        """Retrieve vehicle information."""
        try:
            vehicle_list_resp = await self._make_api_call(self.saic_api.vehicle_list)
            return vehicle_list_resp.vinList
        except Exception as e:
            LOGGER.error("Error retrieving vehicle info: %s", e)
            return None

    async def get_vehicle_status(self, vin: str | None = None):
        """Retrieve vehicle status for *vin* (defaults to self.vin).

        Accepts an explicit vin so that a shared client instance (one per
        account, shared across all VINs on that account) can fetch data for
        any of the account's vehicles rather than always using the VIN it was
        originally constructed with.  Coordinators must pass their own VIN
        explicitly to avoid all cars on the same account returning the same
        data.
        """
        target_vin = vin or self.vin
        try:
            vehicle_status = await self._make_api_call(
                self.saic_api.get_vehicle_status, target_vin
            )
            return vehicle_status
        except Exception as e:
            LOGGER.error("Error retrieving vehicle status for VIN %s: %s", target_vin, e)
            return None

    # ACTIONS

    # ALARM CONTROL
    async def trigger_alarm(
        self, vin: str, with_horn=True, with_lights=True, should_stop=False
    ):
        """Trigger or stop the alarm (Find My Car feature)."""
        try:
            await self._make_api_call(
                self.saic_api.control_find_my_car,
                vin=vin,
                should_stop=should_stop,
                with_horn=with_horn,
                with_lights=with_lights,
            )
        except Exception as e:
            LOGGER.error(f"Error triggering alarm for VIN {vin}: {e}")
            raise

    # MESSAGE QUEUE / EVENT POLLING

    async def get_alarm_messages(self, page_num: int = 1, page_size: int = 10):
        """Retrieve alarm messages from the SAIC message queue.

        Used to detect vehicle events (engine start, shutdown, charging)
        without polling the full vehicle status endpoint on a fixed interval.
        Returns a MessageResp object with a .messages list of MessageEntity.
        """
        try:
            result = await self._make_api_call(
                self.saic_api.get_alarm_list,
                page_num=page_num,
                page_size=page_size,
            )
            return result
        except Exception as e:
            LOGGER.warning("Error retrieving alarm messages: %s", e)
            return None

    async def delete_message(self, message_id: "str | int") -> None:
        """Delete a single alarm message by ID from the SAIC message queue.

        Removing processed messages (particularly vehicle-start type 323)
        prevents unbounded queue growth on the SAIC server and avoids
        re-processing stale events after an HA restart.

        The SAIC backend accepts either str or int message IDs; pass through
        whatever was returned on the MessageEntity.messageId field.

        Mirrors the deletion pattern from saic-python-mqtt-gateway
        src/handlers/message.py — delete_message is called for each consumed
        vehicle-start message except the most-recent (watermark) one.

        Args:
            message_id: the messageId value from the MessageEntity.
        """
        try:
            await self._make_api_call(
                self.saic_api.delete_message,
                message_id=message_id,
            )
            LOGGER.debug("Deleted alarm message ID %s", message_id)
        except Exception as e:
            # Non-fatal: a failed delete just means the message stays in the
            # queue.  It will be deduplicated by the watermark logic on the
            # next poll, so polling correctness is unaffected.
            LOGGER.warning(
                "Could not delete alarm message ID %s: %s", message_id, e
            )

    async def delete_all_alarms(self) -> None:
        """Delete all alarm messages for this account from the SAIC queue.

        Use sparingly — intended for maintenance / queue-clear scenarios, not
        for routine per-message cleanup (use delete_message for that).
        """
        try:
            await self._make_api_call(self.saic_api.delete_all_alarms)
            LOGGER.info("Deleted all alarm messages for account")
        except Exception as e:
            LOGGER.warning("Could not delete all alarm messages: %s", e)

    async def set_alarm_switches(self, vin: str) -> None:
        """Register alarm switch subscriptions with the SAIC API.

        Tells the SAIC server to queue alarm messages for this account/VIN
        when key vehicle events occur. Uses all alarm types supported by the
        saic-python-client-ng AlarmType enum. Call once during coordinator setup.
        """
        try:
            from saic_ismart_client_ng.api.vehicle.alarm import AlarmType
            alarm_switches = list(AlarmType)
            await self._make_api_call(
                self.saic_api.set_alarm_switches,
                alarm_switches=alarm_switches,
                vin=vin,
            )
            LOGGER.debug(
                "Registered alarm switches for VIN %s: %s",
                vin,
                [a.name for a in alarm_switches],
            )
        except Exception as e:
            LOGGER.warning(
                "Could not register alarm switches for VIN %s: %s — "
                "message-driven updates may not function.",
                vin,
                e,
            )

    # CHARGING CONTROL
    async def send_vehicle_charging_control(self, vin, action):
        """Send a charging control command to the vehicle."""
        try:
            LOGGER.debug(f"Charging control - VIN: {vin}, action: {action}")
            # Use the control_charging method from the saic-python-client-ng library
            if action == "start":
                await self._make_api_call(
                    self.saic_api.control_charging, vin=vin, stop_charging=False
                )
            else:
                await self._make_api_call(
                    self.saic_api.control_charging, vin=vin, stop_charging=True
                )
            LOGGER.info(f"Charging {action} command sent successfully for VIN: {vin}")
        except Exception as e:
            LOGGER.error(f"Error sending charging {action} command for VIN {vin}: {e}")
            raise

    async def send_vehicle_charging_ptc_heat(self, vin, action):
        """Send a battery heating control command to the vehicle."""
        try:
            LOGGER.debug(f"Battery heating control - VIN: {vin}, action: {action}")
            if action == "start":
                await self._make_api_call(
                    self.saic_api.control_battery_heating, vin=vin, enable=True
                )
            else:
                await self._make_api_call(
                    self.saic_api.control_battery_heating, vin=vin, enable=False
                )
            LOGGER.info(
                f"Battery heating {action} command sent successfully for VIN: {vin}"
            )
        except Exception as e:
            LOGGER.error(
                f"Error sending battery heating {action} command for VIN {vin}: {e}"
            )
            raise

    async def get_battery_heating_schedule(self, vin):
        """Retrieve the scheduled battery heating configuration."""
        try:
            return await self._make_api_call(
                self.saic_api.get_vehicle_battery_heating_schedule, vin
            )
        except Exception as e:
            LOGGER.error(
                f"Error retrieving battery heating schedule for VIN {vin}: {e}"
            )
            raise

    async def enable_battery_heating_schedule(self, vin, start_time, tz=None):
        """Enable scheduled battery heating at start_time in the given timezone."""
        try:
            LOGGER.debug(
                f"Enabling battery heating schedule - VIN: {vin}, "
                f"start_time: {start_time}, tz: {tz}"
            )
            await self._make_api_call(
                self.saic_api.enable_schedule_battery_heating,
                vin=vin,
                start_time=start_time,
                tz=tz,
            )
            LOGGER.info(
                f"Battery heating schedule enabled for VIN: {vin} at {start_time}"
            )
        except Exception as e:
            LOGGER.error(
                f"Error enabling battery heating schedule for VIN {vin}: {e}"
            )
            raise

    async def disable_battery_heating_schedule(self, vin):
        """Disable scheduled battery heating."""
        try:
            await self._make_api_call(
                self.saic_api.disable_schedule_battery_heating, vin
            )
            LOGGER.info(f"Battery heating schedule disabled for VIN: {vin}")
        except Exception as e:
            LOGGER.error(
                f"Error disabling battery heating schedule for VIN {vin}: {e}"
            )
            raise

    async def set_scheduled_charging(self, vin, start_time, end_time, mode):
        """Set the scheduled charging window and mode.

        mode is a saic_ismart_client_ng ScheduledChargingMode. Times are sent
        as raw hours/minutes exactly as shown in the iSmart app (no timezone
        conversion is applied by the SAIC API).
        """
        try:
            LOGGER.debug(
                f"Setting scheduled charging - VIN: {vin}, start: {start_time}, "
                f"end: {end_time}, mode: {mode.name}"
            )
            await self._make_api_call(
                self.saic_api.set_schedule_charging,
                vin,
                start_time=start_time,
                end_time=end_time,
                mode=mode,
            )
            LOGGER.info(
                f"Scheduled charging set for VIN {vin}: {mode.name} "
                f"({start_time} - {end_time})"
            )
        except Exception as e:
            LOGGER.error(f"Error setting scheduled charging for VIN {vin}: {e}")
            raise

    async def set_current_limit(
        self,
        vin: str,
        target_soc: BatterySoc,
        current_limit_code: ChargeCurrentLimitOption,
    ):
        """Set the charging current limit."""
        try:
            LOGGER.debug(
                "Setting charging current limit for VIN %s to %s (%s)",
                vin,
                current_limit_code.limit,
                current_limit_code,
            )

            # Map local enum to external enum
            external_charge_current_limit = self.map_to_external_charge_current_limit(
                current_limit_code
            )

            # Call the API method with the target_soc and new charge_current_limit
            response = await self._make_api_call(
                self.saic_api.set_target_battery_soc,
                vin,
                target_soc,
                external_charge_current_limit,
            )

            LOGGER.info("Charging current limit set successfully: %s", response)
            return response

        except ValueError as e:
            LOGGER.error("Invalid charging current limit: %s", current_limit_code)
            raise
        except Exception as e:
            LOGGER.error("Error setting charging current limit for VIN %s: %s", vin, e)
            raise

    def map_to_external_charge_current_limit(
        self, local_limit: ChargeCurrentLimitOption
    ) -> ExternalChargeCurrentLimitCode:
        """Map local charging current limit to external ChargeCurrentLimitCode."""
        mapping = {
            ChargeCurrentLimitOption.C_IGNORE: ExternalChargeCurrentLimitCode.C_IGNORE,
            ChargeCurrentLimitOption.C_6A: ExternalChargeCurrentLimitCode.C_6A,
            ChargeCurrentLimitOption.C_8A: ExternalChargeCurrentLimitCode.C_8A,
            ChargeCurrentLimitOption.C_16A: ExternalChargeCurrentLimitCode.C_16A,
            ChargeCurrentLimitOption.C_MAX: ExternalChargeCurrentLimitCode.C_MAX,
        }
        external_code = mapping.get(local_limit)
        if external_code is None:
            LOGGER.error(f"Mapping not found for local limit: {local_limit}")
            raise ValueError(f"Mapping not found for local limit: {local_limit}")
        return external_code

    async def set_target_soc(self, vin, target_soc_percentage):
        """Set the target SOC of the vehicle."""
        try:
            # Map percentage to BatterySoc enum
            percentage_to_enum = {
                40: BatterySoc.SOC_40,
                50: BatterySoc.SOC_50,
                60: BatterySoc.SOC_60,
                70: BatterySoc.SOC_70,
                80: BatterySoc.SOC_80,
                90: BatterySoc.SOC_90,
                100: BatterySoc.SOC_100,
            }
            battery_soc = percentage_to_enum.get(target_soc_percentage)
            if battery_soc is None:
                raise ValueError(
                    f"Invalid target SOC percentage: {target_soc_percentage}"
                )
            # Call the method with the enum value
            await self._make_api_call(
                self.saic_api.set_target_battery_soc, vin, battery_soc
            )
            LOGGER.info(
                "Set target SOC to %d%% for VIN: %s", target_soc_percentage, vin
            )
        except Exception as e:
            LOGGER.error("Error setting target SOC for VIN %s: %s", vin, e)
            raise

    # CLIMATE CONTROL
    async def control_heated_seats(self, vin, left_side_level=0, right_side_level=0):
        """Control the heated seats."""
        try:
            # Call the API method with the levels for each side
            await self._make_api_call(
                self.saic_api.control_heated_seats,
                vin=vin,
                left_side_level=left_side_level,
                right_side_level=right_side_level,
            )
            LOGGER.info(
                "Heated seats updated: Left = %d, Right = %d for VIN: %s",
                left_side_level,
                right_side_level,
                vin,
            )
        except Exception as e:
            LOGGER.error("Error controlling heated seats for VIN %s: %s", vin, e)
            raise

    async def _send_raw_rvc_command(self, vin, req_type_value, param_pairs):
        """Send a raw SAIC vehicle control command.

        req_type_value is the wire value (str) of the rvcReqType, e.g. "5" for
        HEATED_SEATS or "8" for the (library-unknown) steering wheel heater.
        param_pairs is a list of (param_id_int, value_int) tuples.

        Used for commands not exposed by the saic client library's helpers, or
        where a paramId/reqType is not present in the library's enums (confirmed
        via decrypted iSmart app traffic). VehicleControlReq / RvcParams read
        `.value` off the objects they're given, so small shims are used to carry
        the raw integer/string values.
        """
        from saic_ismart_client_ng.api.vehicle.schema import (
            RvcParams,
            VehicleControlReq,
        )

        class _Raw:
            __slots__ = ("value",)

            def __init__(self, value):
                self.value = value

        params = [RvcParams(_Raw(pid), bytes([val])) for pid, val in param_pairs]
        request = VehicleControlReq(
            rvc_params=params,
            rvc_req_type=_Raw(req_type_value),
            vin=vin,  # send_vehicle_control_command hashes this internally
        )
        await self._make_api_call(
            self.saic_api.send_vehicle_control_command, request, vin
        )

    async def control_heated_seat(self, vin, seat, level):
        """Control a single heated seat, independently of the others.

        The iSmart app sends each seat as its own command with its own paramId
        (confirmed via decrypted traffic on the MGS6 EV), rather than the
        library's control_heated_seats() which bundles both front seats together.
        Sending per-seat avoids having to re-send the other seat's level and
        matches the app's own behaviour.

        Seat -> paramId (rvcReqType=5, HEATED_SEATS):
          front_left  = 17, front_right = 18, rear_left = 25, rear_right = 26

        Levels: front seats 0=off,1=low,2=med,3=high. Rear seats are on/off in
        the app but the app sends level 3 for "on" and 0 for "off" (confirmed),
        so rear "on" maps to 3 (handled by the caller).
        """
        from .const import HEATED_SEAT_PARAM_IDS, HEATED_SEATS_REQ_TYPE_VALUE

        if seat not in HEATED_SEAT_PARAM_IDS:
            raise ValueError(f"Unknown seat: {seat}")

        param_id = HEATED_SEAT_PARAM_IDS[seat]
        try:
            LOGGER.debug(
                "Heated seat control - VIN: %s, seat: %s (paramId %s), level: %s",
                vin,
                seat,
                param_id,
                level,
            )
            await self._send_raw_rvc_command(
                vin,
                HEATED_SEATS_REQ_TYPE_VALUE,
                [(param_id, int(level))],
            )
            LOGGER.info(
                "Heated seat %s set to level %s for VIN: %s", seat, level, vin
            )
        except Exception as e:
            LOGGER.error(
                "Error controlling heated seat %s for VIN %s: %s", seat, vin, e
            )
            raise

    async def control_steering_wheel_heat(self, vin, enable):
        """Control the heated steering wheel (on/off).

        This command is NOT exposed by the saic client library. It was captured
        from decrypted iSmart app traffic on the MGS6 EV:
          rvcReqType = 8 (not in the library's RvcReqType enum)
          paramId 24 = 1 (on) / 0 (off)
        """
        from .const import (
            STEERING_WHEEL_HEAT_REQ_TYPE_VALUE,
            STEERING_WHEEL_HEAT_PARAM_ID,
        )

        value = 1 if enable else 0
        try:
            LOGGER.debug(
                "Steering wheel heat control - VIN: %s, enable: %s", vin, enable
            )
            await self._send_raw_rvc_command(
                vin,
                STEERING_WHEEL_HEAT_REQ_TYPE_VALUE,
                [(STEERING_WHEEL_HEAT_PARAM_ID, value)],
            )
            LOGGER.info(
                "Steering wheel heat %s for VIN: %s",
                "enabled" if enable else "disabled",
                vin,
            )
        except Exception as e:
            LOGGER.error(
                "Error controlling steering wheel heat for VIN %s: %s", vin, e
            )
            raise

    async def control_rear_window_heat(self, vin, action):
        """Control the rear window heat."""
        try:
            if action.lower() == "start":
                enable = True
            elif action.lower() == "stop":
                enable = False
            else:
                raise ValueError(
                    f"Invalid action '{action}'. Expected 'start' or 'stop'."
                )

            await self._make_api_call(
                self.saic_api.control_rear_window_heat, vin, enable=enable
            )
            LOGGER.info("Rear window heat %sed successfully.", action)
        except Exception as e:
            LOGGER.error("Error controlling rear window heat: %s", e)
            raise

    async def start_ac(self, vin, temperature_idx=None):
        """Start the vehicle AC with an optional temperature index."""
        try:
            if temperature_idx is not None:
                if not isinstance(temperature_idx, int):
                    raise TypeError(
                        f"temperature_idx must be int, got {type(temperature_idx)}"
                    )
                await self._make_api_call(
                    self.saic_api.start_ac,
                    vin,
                    temperature_idx=temperature_idx,
                )
                LOGGER.info(
                    f"AC started with temperature index {temperature_idx} for VIN: {vin}."
                )
            else:
                await self._make_api_call(self.saic_api.start_ac, vin)
                LOGGER.info(f"AC started without temperature index for VIN: {vin}.")
        except Exception as e:
            LOGGER.error(f"Error starting AC for VIN {vin}: {e}")
            raise

    async def start_climate(
        self,
        vin: str,
        temperature_idx: int,
        fan_speed: int,
        ac_on: bool,
    ):
        """Start the vehicle AC with temperature and fan speed settings."""
        try:
            # Log the mapping for debugging
            LOGGER.debug(
                f"Climate params - Idx: {temperature_idx}, Fan speed: {fan_speed}, AC On: {ac_on}"
            )

            await self._make_api_call(
                self.saic_api.control_climate,
                vin=vin,
                fan_speed=fan_speed,
                ac_on=ac_on,
                temperature_idx=temperature_idx,
            )
            LOGGER.info(
                "Climate started with AC ON: %s, Temperature index set to %s and fan speed %s for VIN: %s",
                ac_on,
                temperature_idx,
                fan_speed,
                vin,
            )
        except Exception as e:
            LOGGER.error("Error starting AC with settings for VIN %s: %s", vin, e)
            raise

    async def start_front_defrost(self, vin):
        """Start the front defrost."""
        try:
            await self._make_api_call(self.saic_api.start_front_defrost, vin)
            LOGGER.info("Front defrost started successfully.")
        except Exception as e:
            LOGGER.error("Error starting front defrost: %s", e)
            raise

    async def stop_ac(self, vin):
        """Stop the vehicle AC."""
        try:
            await self._make_api_call(self.saic_api.stop_ac, vin)
            LOGGER.info("AC stopped successfully.")
        except Exception as e:
            LOGGER.error("Error stopping AC: %s", e)
            raise

    # LOCKS CONTROL
    async def control_charging_port_lock(self, vin: str, unlock: bool):
        """Control the charging port lock (lock/unlock)."""
        try:
            await self._make_api_call(
                self.saic_api.control_charging_port_lock, vin=vin, unlock=unlock
            )
            LOGGER.info(
                "Charging port %s successfully for VIN: %s",
                "unlocked" if unlock else "locked",
                vin,
            )
        except Exception as e:
            LOGGER.error("Error controlling charging port lock for VIN %s: %s", vin, e)
            raise

    async def lock_vehicle(self, vin):
        """Lock the vehicle."""
        try:
            await self._make_api_call(self.saic_api.lock_vehicle, vin)
            LOGGER.info("Vehicle locked successfully.")
        except Exception as e:
            LOGGER.error("Error locking vehicle: %s", e)
            raise

    async def open_tailgate(self, vin):
        """Open the vehicle tailgate."""
        try:
            await self._make_api_call(self.saic_api.open_tailgate, vin)
            LOGGER.info("Tailgate opened successfully.")
        except Exception as e:
            LOGGER.error("Error opening tailgate: %s", e)
            raise

    async def unlock_vehicle(self, vin):
        """Unlock the vehicle."""
        try:
            await self._make_api_call(self.saic_api.unlock_vehicle, vin)
            LOGGER.info("Vehicle unlocked successfully.")
        except Exception as e:
            LOGGER.error("Error unlocking vehicle: %s", e)
            raise

    # WINDOWS CONTROL
    async def control_sunroof(self, vin, action):
        """Control the sunroof (open/close)."""
        try:
            LOGGER.debug(f"Sunroof control - VIN: {vin}, action: {action}")
            should_open, action_name = normalize_sunroof_action(action)
            await self._make_api_call(
                self.saic_api.control_sunroof, vin=vin, should_open=should_open
            )
            LOGGER.info(
                "Sunroof %s command sent successfully for VIN: %s",
                action_name,
                vin,
            )
        except Exception as e:
            LOGGER.error("Error controlling sunroof for VIN %s: %s", vin, e)
            raise

    async def control_windows(self, vin, action):
        """Control the four door windows (open / close / ventilate).

        Sends the SAIC WINDOWS command (rvcReqType=3) directly, rather than the
        library's control_windows() helper, because that helper uses a different
        open value than the one the MGS6 actually uses.

        Verified against decrypted iSmart app traffic on the MGS6 EV (MIS3E),
        cross-checked with the resulting window status in the response:
          rvcReqType = 3
          paramId 8  (WINDOW_SUNROOF)    = 0   (sunroof always left untouched)
          paramId 9-12 (all door windows) = 1  (command acts on all four together)
          paramId 13 (WINDOW_OPEN_CLOSE) = 0 close / 1 ventilate / 2 full open

        The car does not accept single-window control via this API, and its
        status field cannot distinguish "ventilated" from "fully open".

        action: "ventilate" | "open" | "close"
        """
        from saic_ismart_client_ng.api.vehicle.schema import (
            RvcParams,
            RvcParamsId,
            RvcReqType,
            VehicleControlReq,
        )
        from .const import (
            WINDOW_ACTION_CLOSE,
            WINDOW_ACTION_OPEN,
            WINDOW_ACTION_VENTILATE,
        )

        action_map = {
            "ventilate": WINDOW_ACTION_VENTILATE,  # 1 — crack a few cm (app "Ventilation")
            "open": WINDOW_ACTION_OPEN,            # 2 — full open (confirmed on MGS6)
            "close": WINDOW_ACTION_CLOSE,          # 0 — close (confirmed on MGS6)
        }
        action_key = str(action).lower()
        if action_key not in action_map:
            raise ValueError(f"Unknown window action: {action}")

        open_close_byte = bytes([action_map[action_key]])

        try:
            LOGGER.debug("Windows control - VIN: %s, action: %s", vin, action_key)

            params = [
                RvcParams(RvcParamsId.WINDOW_SUNROOF, b"\x00"),
                RvcParams(RvcParamsId.WINDOW_DRIVER, b"\x01"),
                RvcParams(RvcParamsId.WINDOW_2, b"\x01"),
                RvcParams(RvcParamsId.WINDOW_3, b"\x01"),
                RvcParams(RvcParamsId.WINDOW_4, b"\x01"),
                RvcParams(RvcParamsId.WINDOW_OPEN_CLOSE, open_close_byte),
            ]
            request = VehicleControlReq(
                rvc_params=params,
                rvc_req_type=RvcReqType.WINDOWS,
                vin=vin,  # send_vehicle_control_command hashes this internally
            )
            await self._make_api_call(
                self.saic_api.send_vehicle_control_command, request, vin
            )
            LOGGER.info(
                "Windows %s command sent successfully for VIN: %s", action_key, vin
            )
        except Exception as e:
            LOGGER.error("Error controlling windows for VIN %s: %s", vin, e)
            raise

    # SESSION MANAGEMENT
    async def close(self):
        """Close the client session."""
        if self.saic_api is None:
            return

        try:
            if hasattr(self.saic_api, "close"):
                await self.saic_api.close()
                LOGGER.info("Closed MG SAIC API session.")
            else:
                LOGGER.debug(
                    "MG SAIC API session has no close method — nothing to close."
                )
        except Exception as e:
            LOGGER.error("Error closing MG SAIC API session: %s", e)
