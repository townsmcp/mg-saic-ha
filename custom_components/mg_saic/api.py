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
    STOP_AC_VERIFY_DELAY_SECONDS,
    BatterySoc,
    ChargeCurrentLimitOption,
)
from .errors import (
    is_request_rejected,
    is_session_expired,
    is_vehicle_not_locked,
    is_vehicle_unreachable,
    saic_message_of,
)
from .logic import normalize_sunroof_action
from .cached_status import (
    CACHED_STATUS_PATH,
    CACHED_STATUS_REQ_TYPE,
    CachedVehicleStatus,
)


class CommandsLimitReachedException(Exception):
    """Raised when SAIC rejects a command with return code 8.

    Historically read as "remote command limit reached -- start the car with
    the key". But SAIC uses code 8 for several different rejections (e.g.
    "vehicle not locked", handled separately below), and a code 8 on
    2026-09-24 07:07 was followed by the same command succeeding 74 seconds
    later with no key start. What the user is told now comes from SAIC's own
    message (SAICMGAPIClient.last_rejection_message), not this class name.
    """
    pass


# Kept under its old name for callers; the logic lives in errors.py.
saic_message = saic_message_of


class VehicleNotLockedException(Exception):
    """Raised when the SAIC API rejects a command because the car isn't locked.

    SAIC also uses return code 8 for this (#374, @stfvrg) — the server's
    accompanying message is "Vehicle not locked. Please lock it and try
    again." rather than a rate-limit message, and unlike a real command-limit
    hit, the same command succeeds immediately once the vehicle is locked, no
    physical key start required. _make_api_call distinguishes the two by the
    message text, not just the return code, so this is never raised for an
    actual rate-limit rejection.
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
        # SAIC's message for the most recent code-8 rejection (see
        # CommandsLimitReachedException) -- quoted in the notification.
        self.last_rejection_message = None
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
            # What went wrong is read from SAIC's return code and message
            # (errors.py), not by searching the error text.
            if is_session_expired(e):
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
            elif is_vehicle_not_locked(e):
                # Same return code (8) as the real command limit below, but a
                # different server message — distinguish on the message text,
                # not the code, so this is never misreported as the vehicle
                # needing a physical key start (#374).
                LOGGER.warning(
                    "Command rejected: vehicle is not locked (return code 8). "
                    "Lock the vehicle and try again."
                )
                raise VehicleNotLockedException(str(e)) from e
            elif is_request_rejected(e):
                # Log and keep SAIC's actual words: code 8 covers several
                # rejections, and until 2026-09-24 every one was reported as
                # "start the car with the key" whether SAIC said so or not.
                self.last_rejection_message = saic_message_of(e)
                LOGGER.warning(
                    "SAIC rejected the command (return code 8): %s",
                    self.last_rejection_message or str(e),
                )
                raise CommandsLimitReachedException(str(e)) from e
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
        except Exception:
            # Do NOT discard self.saic_api here (issue #233): the object owns
            # an httpx.AsyncClient, and clearing the reference would leave the
            # caller's close() with nothing to close, leaking the transport.
            # The reference is retained so async_setup_entry's cleanup path can
            # close it; a fresh SaicApi is constructed on the next login
            # attempt regardless. The raw error is intentionally not logged
            # here (issue #234) — it is chained by the caller.
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
            # Return code 4 = "can't reach the car right now". Propagate it so the
            # coordinator can flag the Vehicle Reachability sensor as
            # 'unreachable'; previously this was swallowed into a None return,
            # which the coordinator reported only as a generic "is None" error
            # and never recognised as an unreachable condition (#238).
            if is_vehicle_unreachable(e):
                raise
            return None

    async def get_vehicle_info(self):
        """Retrieve vehicle information."""
        try:
            vehicle_list_resp = await self._make_api_call(self.saic_api.vehicle_list)
            return vehicle_list_resp.vinList
        except Exception as e:
            LOGGER.error("Error retrieving vehicle info: %s", e)
            return None

    async def get_cached_vehicle_status(self, vin: str | None = None):
        """Read SAIC's stored copy of the car's last status (cached_status.py).

        A plain request to SAIC's server, the one the iSmart app makes when
        it opens -- not the "ask the car" request every poll uses. Diagnostic
        only: nothing in the integration acts on the reply.
        """
        from saic_ismart_client_ng.crypto_utils import sha256_hex_digest

        target_vin = vin or self.vin

        async def _read(vin_to_read):
            return await self.saic_api.execute_api_call(
                "GET",
                CACHED_STATUS_PATH,
                params={
                    "vin": sha256_hex_digest(vin_to_read),
                    "vehStatusReqType": CACHED_STATUS_REQ_TYPE,
                },
                out_type=CachedVehicleStatus,
            )

        return await self._make_api_call(_read, target_vin)

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
            # Return code 4 = "can't reach the car right now". Propagate it so the
            # coordinator can flag the Vehicle Reachability sensor as
            # 'unreachable'; previously this was swallowed into a None return,
            # which the coordinator reported only as a generic "is None" error
            # and never recognised as an unreachable condition (#238).
            if is_vehicle_unreachable(e):
                raise
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
        Returns a MessageResp object with a .messages list of MessageEntity,
        or None when the queue is EMPTY (SAIC answers code 0 with no data).

        Errors are raised, not turned into None: the poller has to tell an
        empty queue (safe -- no backlog) from a failed read (queue unseen).
        Swallowing them made the two identical, so an empty first poll never
        counted and the next genuine start was discarded as backlog -- and
        the poller's own 401 re-login path could never run.
        """
        return await self._make_api_call(
            self.saic_api.get_alarm_list,
            page_num=page_num,
            page_size=page_size,
        )

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

    async def delete_all_alarms(self) -> bool:
        """Delete all alarm messages for this account from the SAIC queue.

        One request for the whole queue. Used by the message poller to clear
        stale backlog once a genuine vehicle start has been processed.
        Returns True on success, False (after logging) on failure, so the
        caller can fall back to per-message deletion.
        """
        try:
            await self._make_api_call(self.saic_api.delete_all_alarms)
            LOGGER.info("Deleted all alarm messages for account")
            return True
        except Exception as e:
            LOGGER.warning("Could not delete all alarm messages: %s", e)
            return False

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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error controlling heated seats for VIN %s: %s", vin, e)
            raise

    async def _send_raw_rvc_command(self, vin, req_type_value, param_pairs):
        """Send a raw SAIC vehicle control command.

        req_type_value is the wire value (str) of the rvcReqType, e.g. "5" for
        HEATED_SEATS. Now only used for AC Airflow, which mg-saic-client
        doesn't have.
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

        Sent by mg-saic-client's control_heated_seat (0.9.5+), which
        reproduces the iSmart app's per-seat command from decrypted MGS6 EV
        traffic (request type 5, one parameter per seat: front left 17, front
        right 18, rear left 25, rear right 26). Unlike the library's older
        control_heated_seats() it doesn't bundle both front seats together.

        seat: "front_left" | "front_right" | "rear_left" | "rear_right"
        level: front seats 0=off, 1=low, 2=medium, 3=high. Rear seats are
        on/off only; the caller sends REAR_SEAT_ON_LEVEL (3, the app's "on").
        """
        from saic_ismart_client_ng.api.vehicle.climate import HeatedSeat

        try:
            library_seat = HeatedSeat[str(seat).upper()]
        except KeyError:
            raise ValueError(f"Unknown seat: {seat}") from None

        try:
            LOGGER.debug(
                "Heated seat control - VIN: %s, seat: %s, level: %s",
                vin,
                seat,
                level,
            )
            await self._make_api_call(
                self.saic_api.control_heated_seat,
                vin,
                seat=library_seat,
                level=int(level),
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
        """Turn the heated steering wheel on or off.

        Sent by mg-saic-client's control_heated_steering_wheel (0.9.5+), which
        reproduces the iSmart app's command from decrypted MGS6 EV traffic:
        request type 8, parameter 24 = 1 (on) / 0 (off).
        """
        try:
            LOGGER.debug(
                "Steering wheel heat control - VIN: %s, enable: %s", vin, enable
            )
            await self._make_api_call(
                self.saic_api.control_heated_steering_wheel,
                vin,
                enable=bool(enable),
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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
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

    async def control_ac_airflow(self, vin: str):
        """Turn on the cabin AC-Airflow (ventilation) mode.

        A separate fresh-air blower mode with no cooling, distinct from the
        normal AC. Decoded from iSmart traffic (#262, MG HS PHEV / AS33P):
        rvcReqType=6 with paramId 19=1 (fan low), 20=0 (no temperature),
        22=1 (airflow), 255=0. The app requires AC Auto to be off first; from
        the API the command is sent directly.
        """
        try:
            await self._send_raw_rvc_command(
                vin, "6", [(19, 1), (20, 0), (22, 1), (255, 0)]
            )
            LOGGER.info("AC Airflow (ventilation) mode enabled for VIN: %s", vin)
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error enabling AC Airflow for VIN %s: %s", vin, e)
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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error starting AC with settings for VIN %s: %s", vin, e)
            raise

    async def start_front_defrost(self, vin):
        """Start the front defrost."""
        try:
            await self._make_api_call(self.saic_api.start_front_defrost, vin)
            LOGGER.info("Front defrost started successfully.")
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error starting front defrost: %s", e)
            raise

    async def stop_ac(self, vin):
        """Stop the vehicle AC.

        On a genuine failure this raises, same as every other command. But
        SAIC's server can report an error for a stop_ac call that actually
        reached the vehicle and took effect -- confirmed directly from a
        user's log (#262, Harry): the identical error on every attempt, yet
        remoteClimateStatus reliably transitioned to 0 (off) a short while
        later regardless. Rather than surface an error for a command that
        genuinely worked, wait briefly and check the car's own status before
        deciding. Scoped to stop_ac specifically -- the only command this
        has been observed on so far, not a general retry mechanism.
        """
        try:
            await self._make_api_call(self.saic_api.stop_ac, vin)
            LOGGER.info("AC stopped successfully.")
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.warning(
                "stop_ac reported an error (%s) -- checking whether it "
                "took effect anyway before treating it as a failure.",
                e,
            )
            await asyncio.sleep(STOP_AC_VERIFY_DELAY_SECONDS)
            remote_climate_status = None
            try:
                status = await self.get_vehicle_status(vin)
                basic_status = getattr(status, "basicVehicleStatus", None)
                remote_climate_status = getattr(
                    basic_status, "remoteClimateStatus", None
                )
            except Exception as verify_error:
                # Verification itself failing tells us nothing either way --
                # fall through to raising the original error, same as if we
                # hadn't attempted this at all.
                LOGGER.warning(
                    "Could not verify stop_ac's actual effect: %s", verify_error
                )

            if remote_climate_status == 0:
                LOGGER.info(
                    "AC stopped successfully (confirmed via a status check "
                    "after the server reported an error)."
                )
                return

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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error controlling charging port lock for VIN %s: %s", vin, e)
            raise

    async def lock_vehicle(self, vin):
        """Lock the vehicle."""
        try:
            await self._make_api_call(self.saic_api.lock_vehicle, vin)
            LOGGER.info("Vehicle locked successfully.")
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error locking vehicle: %s", e)
            raise

    async def open_tailgate(self, vin):
        """Open the vehicle tailgate."""
        try:
            await self._make_api_call(self.saic_api.open_tailgate, vin)
            LOGGER.info("Tailgate opened successfully.")
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error opening tailgate: %s", e)
            raise

    async def unlock_vehicle(self, vin):
        """Unlock the vehicle."""
        try:
            await self._make_api_call(self.saic_api.unlock_vehicle, vin)
            LOGGER.info("Vehicle unlocked successfully.")
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
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
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error controlling sunroof for VIN %s: %s", vin, e)
            raise

    async def control_windows(self, vin, action):
        """Close, ventilate or fully open the four door windows (together).

        Sent by mg-saic-client's control_door_windows (0.9.5+), which
        reproduces the iSmart app's command from decrypted MGS6 EV traffic
        (sunroof left alone, all four door windows, 0 close / 1 ventilate /
        2 fully open). The library's older control_windows helper uses a
        different open value that the MGS6 doesn't use.

        The car does not accept single-window control via this API, and its
        status field cannot distinguish "ventilated" from "fully open".

        action: "ventilate" | "open" | "close"
        """
        from saic_ismart_client_ng.api.vehicle.windows import DoorWindowsAction

        action_map = {
            "ventilate": DoorWindowsAction.VENTILATE,  # a few cm (app "Ventilation")
            "open": DoorWindowsAction.OPEN,            # fully open
            "close": DoorWindowsAction.CLOSE,
        }
        action_key = str(action).lower()
        if action_key not in action_map:
            raise ValueError(f"Unknown window action: {action}")

        try:
            LOGGER.debug("Windows control - VIN: %s, action: %s", vin, action_key)
            await self._make_api_call(
                self.saic_api.control_door_windows,
                vin,
                action=action_map[action_key],
            )
            LOGGER.info(
                "Windows %s command sent successfully for VIN: %s", action_key, vin
            )
        except CommandsLimitReachedException:
            raise
        except VehicleNotLockedException:
            raise
        except Exception as e:
            LOGGER.error("Error controlling windows for VIN %s: %s", vin, e)
            raise

    # SESSION MANAGEMENT
    async def close(self):
        """Close the client session and release the underlying transport.

        mg-saic-client (>=0.9.4) exposes a public close() on SaicApi that shuts
        down the internal httpx.AsyncClient; without it the transport leaks on
        teardown (issue #233). Failures are logged rather than raised so
        teardown never fails on cleanup.
        """
        if self.saic_api is None:
            return

        try:
            await self.saic_api.close()
            LOGGER.info("Closed MG SAIC API session.")
        except Exception as e:
            LOGGER.warning("Error closing MG SAIC API session: %s", e)
        finally:
            self.saic_api = None
