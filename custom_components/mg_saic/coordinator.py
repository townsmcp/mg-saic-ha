# File: coordinator.py

from datetime import datetime, timedelta, timezone
import asyncio
from contextlib import suppress
from homeassistant.config_entries import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import utcnow
from .api import SAICMGAPIClient, CommandsLimitReachedException
from .backends import Feature
from .backends import backend_supports as _backend_supports
from .logic import select_update_interval
from .trip_stats import TripStatsManager, TripSnapshot

# After the car turns off, fire extra refreshes at these intervals (seconds)
# to catch plug-in as quickly as possible.  The coordinator is still on its
# powered-on poll cycle when shutdown occurs; without this we could wait the
# full powered interval before detecting plug-in.
# The sequence exits early as soon as is_charging is detected.
# Sequence: 1 min, 3 min, 7 min, 15 min, 25 min → catches plug-in within ~1-25 min.
POST_SHUTDOWN_REFRESH_SEQUENCE = [60, 120, 240, 480, 600]

from .const import (
    DATA_DECIMAL_CORRECTION,
    DATA_DECIMAL_CORRECTION_SOC,
    MILEAGE_UINT16_SATURATION,
    AFTER_ACTION_UPDATE_INTERVAL_DELAY,
    CHARGING_STATUS_CODES,
    CONF_ABRP_API_KEY,
    CONF_ABRP_USER_TOKEN,
    DEFAULT_AC_LONG_INTERVAL,
    CONF_HOLIDAY_UPDATE_INTERVAL,
    CONF_STALE_DATA_THRESHOLD,
    DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS,
    DEFAULT_STALE_DATA_THRESHOLD_HOURS,
    REMOTE_CLIMATE_STATUS_DEFROST,
    REMOTE_CLIMATE_STATUS_OFF,
    SAIC_RETURN_CODE_UNREACHABLE,
    DATA_FRESHNESS_LIVE,
    DATA_FRESHNESS_CACHED,
    DATA_FRESHNESS_FAILED,
    VEHICLE_REACHABILITY_AWAKE,
    VEHICLE_REACHABILITY_LIKELY_ASLEEP,
    VEHICLE_REACHABILITY_UNREACHABLE,
    DEFAULT_ALARM_LONG_INTERVAL,
    DEFAULT_BATTERY_HEATING_LONG_INTERVAL,
    DEFAULT_CHARGING_CURRENT_LONG_INTERVAL,
    DEFAULT_CHARGING_LONG_INTERVAL,
    DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL,
    DEFAULT_FRONT_DEFROST_LONG_INTERVAL,
    DEFAULT_HEATED_SEATS_LONG_INTERVAL,
    DEFAULT_LOCK_UNLOCK_LONG_INTERVAL,
    DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL,
    DEFAULT_SUNROOF_LONG_INTERVAL,
    DEFAULT_TAILGATE_LONG_INTERVAL,
    DEFAULT_TARGET_SOC_LONG_INTERVAL,
    DEFAULT_VEHICLE_PROFILE,
    DOMAIN,
    GENERIC_RESPONSE_SOC_THRESHOLD,
    GENERIC_RESPONSE_STATUS_THRESHOLD,
    GENERIC_RESPONSE_TEMPERATURE,
    LOGGER,
    RETRY_BACKOFF_FACTOR,
    RETRY_LIMIT,
    STARTUP_API_TIMEOUT,
    STARTUP_CHARGING_TIMEOUT,
    RUNTIME_CHARGING_TIMEOUT,
    STATUS_TIMESTAMP_FUTURE_TOLERANCE,
    STATUS_TIMESTAMP_MAX_AGE,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_AFTER_SHUTDOWN,
    UPDATE_INTERVAL_AFTER_FAILURE,
    MAX_FAST_RETRIES_AFTER_FAILURE,
    UPDATE_INTERVAL_CHARGING,
    UPDATE_INTERVAL_DC_CHARGING,
    UPDATE_INTERVAL_GRACE_PERIOD,
    UPDATE_INTERVAL_POWERED,
    VEHICLE_PROFILES,
)


# Number of consecutive polls that must fail to bring fresh data (with a return
# code 4) before the Vehicle Reachability sensor is flagged 'unreachable'. A
# single transient "remote control instruction failed, please try again later"
# during a drive should not flip the sensor; a car that's genuinely out of
# contact will fail repeatedly and cross this threshold. See #238.
UNREACHABLE_CONSECUTIVE_POLL_THRESHOLD = 2


class SAICMGDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the MG SAIC API."""

    def __init__(self, hass, client: SAICMGAPIClient, config_entry):
        """Initialize."""
        super().__init__(
            hass,
            LOGGER,
            name="MG SAIC data update coordinator",
            update_interval=UPDATE_INTERVAL,
        )

        self.client = client
        self.config_entry = config_entry
        self.vin = config_entry.data.get("vin")

        # State Variables
        self.is_charging = False
        self.is_dc_charging = False
        # Locally pending battery heating schedule start time, used by the
        # time entity while the schedule switch is off.
        self.battery_heating_pending_time = None
        # Locally pending scheduled charging window, used by the time entities.
        # Values are only sent to the vehicle when the Scheduled Charging Mode
        # select is changed (mirrors the heated-seat level pattern to avoid
        # spending a remote command on every time adjustment).
        self.scheduled_charging_pending_start = None
        self.scheduled_charging_pending_end = None
        # Optimistic ventilation tracking. The vehicle has no reliable
        # "is ventilating" status field (remoteClimateStatus=2 reports A/C, not
        # ventilation, and is 0 when ventilating from cold), and the window
        # status can't distinguish ventilated from fully open. So the
        # Ventilation binary sensor reflects the last ventilate command sent
        # FROM HOME ASSISTANT: set True on a ventilate press, cleared when an
        # open/close command is sent, or when the windows report closed after
        # having been seen open (i.e. ventilation ended). Ventilation triggered
        # from the iSmart app is not reflected — a known, documented gap.
        self.ventilation_active = False
        self._ventilation_windows_seen_open = False
        self.is_powered_on = False
        self.is_initial_setup = False
        self.after_shutdown_active = False

        # Activity Tracking
        self.last_powered_on_time = None
        self.last_powered_off_time = None
        self.last_vehicle_activity = None

        # Next Update Time
        self.next_update_time = None
        # Holiday mode (runtime poll-rate override), persisted flag from options.
        self.holiday_mode = config_entry.options.get("holiday_mode", False)
        self.holiday_update_interval = timedelta(
            hours=config_entry.options.get(
                CONF_HOLIDAY_UPDATE_INTERVAL,
                DEFAULT_HOLIDAY_UPDATE_INTERVAL_HOURS,
            )
        )
        self.stale_data_threshold = timedelta(
            hours=config_entry.options.get(
                CONF_STALE_DATA_THRESHOLD,
                DEFAULT_STALE_DATA_THRESHOLD_HOURS,
            )
        )
        self._last_command_unreachable = False
        self._last_command_unreachable_time = None
        # Debounce for the code-4 'unreachable' signal: only flag after this many
        # consecutive polls fail to bring fresh data, so a single transient "try
        # again later" during a drive doesn't flip the sensor (#238). Reset by
        # any positive proof of reachability (fresh status, detected activity, a
        # successful command).
        self._consecutive_unreachable_polls = 0
        self._code4_this_cycle = False
        # Highest vehicle-reported statusTime we've seen. A response whose
        # statusTime advances beyond this is positive proof the telematics just
        # reported fresh data (not a cached response served while asleep), and
        # is used to clear the 'unreachable' flag when the car wakes. See #238.
        self._last_status_time = None
        # Bounded fast-retry after a failed update cycle (#238). A failed poll
        # skips interval selection, so without this the coordinator would keep
        # the last successful cycle's interval — potentially a multi-hour idle
        # value — and miss an active charge. Reset on any successful cycle.
        self._consecutive_update_failures = 0
        self.failure_retry_interval = UPDATE_INTERVAL_AFTER_FAILURE
        # Data Freshness (#238): result of the most recent poll — live (fresh
        # data), cached (poll succeeded but data unchanged), or failed (poll
        # errored). None until the first cycle completes.
        self._last_poll_result = None
        self._action_refresh_task = None
        self._action_refresh_generation = 0

        # Account-level API lock — shared with all coordinators on the same
        # account and the SAICMGAccountPoller.  Serialises concurrent API calls
        # so that a message-poll and a data refresh on the same account never
        # race each other and invalidate the session token.
        # Injected by __init__.async_setup_entry after construction.
        self._api_lock: asyncio.Lock | None = None

        # Post-shutdown rapid refresh state
        self._shutdown_refresh_task: asyncio.Task | None = None

        # Track previous powered-on state so we detect the transition even
        # when status_data is None (generic response during power-down)
        self._prev_is_powered_on: bool = False
        self._prev_is_charging: bool = False

        # Initialize with default values
        self.vehicle_series = None
        self.min_temp = 16  # Default fallback
        self.max_temp = 28  # Default fallback
        self.temp_offset = 2  # Default fallback
        self.known_battery_capacity_kwh = None  # Set once series is detected
        self.known_fuel_tank_litres = None  # Per-model tank size, for fuel stats (#301)
        # Trip/efficiency stats manager (#301). Created and loaded in async_setup.
        self.trip_stats = None
        # Climate control profile — set from VEHICLE_PROFILES on first data fetch.
        # Defaults match original integration behaviour so unrecognised models
        # continue to work as before.
        self.climate_status_cool: set = {3}
        self.climate_status_fan_only: set = {2}
        self.fan_speed_low: int = 1
        self.fan_speed_medium: int = 3
        self.fan_speed_high: int = 5
        self.temp_idx_inverted: bool = False
        # Optional per-model {temperature: index} lookup (from VEHICLE_PROFILES).
        # When present, takes priority over the linear formula in
        # get_ac_temperature_idx. None means "use the formula".
        self.temp_index_map: dict | None = None
        # Climate control scheme (see VEHICLE_PROFILES in const.py):
        #   "fan_speed"   — the classic model: HA exposes a Low/Med/High fan
        #                   slider, and the selected fan value is sent with each
        #                   AC command. This is the default for all models.
        #   "mode_select" — for cars (e.g. IS31P / MG S9 PHEV) where the API's
        #                   "fan_speed" byte is really a climate MODE selector,
        #                   not a linear fan speed. HA instead exposes hvac_mode
        #                   (off/fan_only/cool/heat) + preset_mode (Max Cool /
        #                   Defrost), each mapping to a fixed mode value. No fan
        #                   slider is shown for these models.
        self.climate_control_scheme: str = "fan_speed"
        # Shared climate control state, so the climate entity and the separate
        # A/C switch / temperature number / mode select all reflect one source
        # of truth (a change in any of them updates the others). requested_target
        # _temp is display-only and never sends a command on its own — it rides
        # along with the next actual A/C command, preserving the 3-command limit.
        # climate_entity is a back-reference the switch/select delegate to so the
        # command dispatch lives in exactly one place. Both are set up before any
        # command can be issued by a user.
        self.requested_target_temp: float = 22.0
        self.climate_entity = None
        # mode_select value map (only used when scheme == "mode_select").
        # Maps each logical climate action to the integer sent via the API's
        # fan_speed parameter. Defaults are the IS31P-confirmed values but are
        # always overridden per-profile when the scheme is mode_select.
        self.climate_mode_fan_only: int = 1   # HVACMode.FAN_ONLY
        self.climate_mode_cool: int = 2       # HVACMode.COOL (auto fan, follows temp)
        self.climate_mode_heat: int = 4       # HVACMode.HEAT
        self.climate_mode_max_cool: int = 3   # preset "Max Cool" (fixed strong fan)
        # When True, the Max Cool preset also pins the target temperature to the
        # profile minimum (mirrors the iSmart app's one-tap LOW-cool button).
        # Used by cars whose plain Cool mode is already the strongest cool, so
        # the only extra thing Max Cool adds is the coldest setpoint (e.g. the
        # MG4 EV URBAN, AH4EM — see #243).
        self.max_cool_forces_min_temp: bool = False
        self.climate_mode_defrost: int = 5    # preset "Defrost"
        # Reverse map: remoteClimateStatus values that mean "heating".
        # (cool/fan_only reverse maps already exist as climate_status_cool /
        # climate_status_fan_only above.)
        self.climate_status_heat: set = set()
        self.climate_status_defrost: set = set()
        # Fan value that engages PTC resistive heating on fan-speed cars that
        # have a heater (compressor off + this AUTO value). MG4-confirmed as 2
        # (#173). Only used when the profile defines a heat status.
        self.heat_fan_speed: int = 2
        # Fixed AUTO fan value for cars with no remote fan control (the fan
        # slider is hidden and this value is always sent). None = classic
        # Low/Med/High slider. climate_fan_only_airflow makes Fan Only send the
        # separate AC-Airflow ventilation command. Both set from the profile
        # (AS33P / MG HS PHEV — see const.py, #262).
        self.climate_fan_auto: int | None = None
        self.climate_fan_only_airflow: bool = False
        # Per-model feature flags — set from VEHICLE_PROFILES on first data fetch.
        self.supports_target_soc: bool = True
        self.reliable_fuel_range_elec: bool = True
        self.charging_capacity_correction: float | None = None
        self.supports_charging_current_limit: bool = True
        self.model_year_override: str | None = None

        # Reference to the command-error Event entity (event.py), set once it
        # registers itself via register_command_error_event_entity. May be
        # None if the entity hasn't loaded yet or was removed — callers must
        # tolerate that, since the event entity is purely supplementary to
        # the persistent notification path and must never block commands.
        self._command_error_event_entity = None

        # Initialize update intervals from config_entry options, falling back to defaults if not set
        options = config_entry.options

        # Helper function to get interval from options or fallback to const.py
        def get_interval(option_key, default_interval):
            return timedelta(
                minutes=options.get(
                    option_key, int(default_interval.total_seconds() / 60)
                )
            )

        def get_delay(option_key, default_interval):
            return timedelta(
                seconds=options.get(option_key, int(default_interval.total_seconds()))
            )

        # Base update intervals
        self.default_update_interval = get_interval("update_interval", UPDATE_INTERVAL)
        self.update_interval = self.default_update_interval
        self.charging_update_interval = get_interval(
            "charging_update_interval", UPDATE_INTERVAL_CHARGING
        )
        self.dc_charging_update_interval = get_interval(
            "dc_charging_update_interval", UPDATE_INTERVAL_DC_CHARGING
        )
        self.powered_update_interval = get_interval(
            "powered_update_interval", UPDATE_INTERVAL_POWERED
        )

        # Additional update intervals
        self.after_shutdown_update_interval = get_interval(
            "after_shutdown_update_interval", UPDATE_INTERVAL_AFTER_SHUTDOWN
        )
        self.grace_period_update_interval = get_interval(
            "grace_period_update_interval", UPDATE_INTERVAL_GRACE_PERIOD
        )

        # After action immediate and refresh intervals
        self.after_action_delay = get_delay(
            "after_action_delay", AFTER_ACTION_UPDATE_INTERVAL_DELAY
        )

        # Long-interval updates after actions
        self.alarm_long_interval = get_interval(
            "alarm_long_interval", DEFAULT_ALARM_LONG_INTERVAL
        )
        self.ac_long_interval = get_interval(
            "ac_long_interval", DEFAULT_AC_LONG_INTERVAL
        )
        self.front_defrost_long_interval = get_interval(
            "front_defrost_long_interval", DEFAULT_FRONT_DEFROST_LONG_INTERVAL
        )
        self.rear_window_heat_long_interval = get_interval(
            "rear_window_heat_long_interval", DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL
        )
        self.lock_unlock_long_interval = get_interval(
            "lock_unlock_long_interval", DEFAULT_LOCK_UNLOCK_LONG_INTERVAL
        )
        self.charging_port_lock_long_interval = get_interval(
            "charging_port_lock_long_interval", DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL
        )
        self.heated_seats_long_interval = get_interval(
            "heated_seats_long_interval", DEFAULT_HEATED_SEATS_LONG_INTERVAL
        )
        self.battery_heating_long_interval = get_interval(
            "battery_heating_long_interval", DEFAULT_BATTERY_HEATING_LONG_INTERVAL
        )
        self.charging_long_interval = get_interval(
            "charging_long_interval", DEFAULT_CHARGING_LONG_INTERVAL
        )
        self.sunroof_long_interval = get_interval(
            "sunroof_long_interval", DEFAULT_SUNROOF_LONG_INTERVAL
        )
        self.tailgate_long_interval = get_interval(
            "tailgate_long_interval", DEFAULT_TAILGATE_LONG_INTERVAL
        )
        self.target_soc_long_interval = get_interval(
            "target_soc_long_interval", DEFAULT_TARGET_SOC_LONG_INTERVAL
        )
        self.charging_current_long_interval = get_interval(
            "charging_current_long_interval", DEFAULT_CHARGING_CURRENT_LONG_INTERVAL
        )

        LOGGER.debug(
            f"Update intervals initialized: "
            f"Default: {self.default_update_interval}, "
            f"Charging: {self.charging_update_interval}, "
            f"Powered: {self.powered_update_interval}, "
            f"After Shutdown: {self.after_shutdown_update_interval}, "
            f"Grace Period: {self.grace_period_update_interval}, "
            f"After Action Delay: {self.after_action_delay}"
        )

        # Use the vehicle type from the config entry
        self.vehicle_type = self.config_entry.data.get("vehicle_type")

        # Vehicle capabilities
        self.has_sunroof = config_entry.options.get(
            "has_sunroof", config_entry.data.get("has_sunroof", False)
        )
        self.has_heated_seats = config_entry.options.get(
            "has_heated_seats", config_entry.data.get("has_heated_seats", False)
        )
        self.has_rear_heated_seats = config_entry.options.get(
            "has_rear_heated_seats",
            config_entry.data.get("has_rear_heated_seats", False),
        )
        self.has_battery_heating = config_entry.options.get(
            "has_battery_heating", config_entry.data.get("has_battery_heating", False)
        )
        self.has_steering_wheel_heat = config_entry.options.get(
            "has_steering_wheel_heat",
            config_entry.data.get("has_steering_wheel_heat", False),
        )
        self.has_window_control = config_entry.options.get(
            "has_window_control",
            config_entry.data.get("has_window_control", False),
        )
        # Locally-selected front heated-seat levels, pending until the matching
        # seat switch is turned on (see select.py / switch.py). Keyed by seat
        # ("front_left"/"front_right") -> level int (0-3).
        self.pending_seat_levels: dict = {}

        # Whether the car has rear doors/windows — driven by the per-model
        # VEHICLE_PROFILES entry (see const.py), not the SAIC API's own
        # DOOR/WINDOW vehicleModelConfiguration bitmask. That API data proved
        # unreliable: it reported WINDOW='0000' for 4-door/4-window cars
        # (MG4, MGS5) exactly the same way it does for the 2-door Cyberster,
        # which suppressed valid rear window entities for those cars.
        # See issue #203. Default True until the profile is loaded on first
        # data fetch, so pre-existing installations don't lose entities
        # before _update_state runs.
        self.has_rear_doors = True
        self.has_rear_windows = True
        # Front passenger window / front defrost are present on all normally
        # profiled cars; a per-model profile can turn them off (e.g. the MG3
        # Hybrid tracks only the driver window and ignores the defrost command
        # — see #258).
        self.has_front_passenger_window = True
        self.has_front_defrost = True
        # When True, the fan-speed "Cool" mode is sent via the simple start_ac
        # command instead of the full control_climate command (for cars that
        # only honour start_ac — e.g. the MG3 Hybrid, #258).
        self.cool_uses_start_ac = False

        # Post-shutdown refresh sequence — enabled by default, opt-out via options.
        # When enabled, the coordinator fires a rapid series of refreshes after
        # detecting engine-off or door-lock, to catch plug-in within 1-3 minutes
        # without relying on SAIC's slow/unreliable poweroff notifications.
        self.enable_shutdown_refresh_sequence = config_entry.options.get(
            "enable_shutdown_refresh_sequence", True
        )

    # ── Account-level lock injection ─────────────────────────────────────────

    def set_api_lock(self, lock: asyncio.Lock) -> None:
        """Inject the shared account-level API lock.

        Called by __init__.async_setup_entry immediately after the coordinator
        is constructed, before async_setup() is awaited.  The lock is shared
        with all other coordinators on the same (username, region) account and
        with the SAICMGAccountPoller, ensuring that message-queue polls and
        vehicle-data fetches never race each other.
        """
        self._api_lock = lock

    def backend_supports(self, feature: Feature) -> bool:
        """Return True if this vehicle's backend supports *feature*.

        Backends (see backends/__init__.py) declare which command/data
        families they implement AND have confirmed on a real car.  Entity
        platforms combine this with the per-vehicle "if equipped" flags:
        an entity exists only when the backend supports the feature and the
        vehicle has it.  Clients that predate the backend split declare no
        feature set and are treated as fully featured (global behaviour).
        """
        return _backend_supports(self.client, feature)

    # ── Event-driven refresh (called by SAICMGAccountPoller) ─────────────────

    async def async_trigger_refresh(self, reason: str = "message event") -> None:
        """Immediately request a data refresh, triggered by an alarm message.

        Called by SAICMGAccountPoller when it detects a significant event
        (engine start, shutdown, charging) for this coordinator's VIN.
        Uses async_request_refresh so HA's built-in deduplication prevents
        a pile-up if multiple messages arrive in the same poll cycle.

        Args:
            reason: short human-readable description for log output.
        """
        LOGGER.info(
            "Coordinator VIN %s: event-driven refresh requested — %s",
            self.vin,
            reason,
        )
        await self.async_request_refresh()

    def hint_vehicle_started(self, started_at: datetime) -> None:
        """Pre-apply powered-on state from a vehicle-start alarm message timestamp.

        Called by SAICMGAccountPoller when it receives a type-323 (vehicle
        start) message, *before* the confirming vehicle-status poll arrives.
        This pre-sets:

        - ``is_powered_on = True``
        - ``last_powered_on_time = started_at``   (message timestamp, not poll time)
        - Immediately switches ``update_interval`` to ``powered_update_interval``

        so that the coordinator begins rapid polling right away rather than
        waiting up to one full default interval (which could be hours for
        users with long idle intervals).

        The confirming poll in ``_update_state`` will still run normally —
        if it sees ``powerMode=2`` it keeps the hint state; if it sees
        ``powerMode`` as something else (e.g. the message was spurious), it
        corrects the state as usual.

        Guards:
        - If ``is_powered_on`` is already ``True`` and ``last_powered_on_time``
          is *newer* than ``started_at``, the hint is a no-op (a confirmed poll
          already has more accurate data).
        - If an action-interval sequence is active, ``_adjust_update_interval``
          will skip the reschedule as usual.

        Args:
            started_at: timezone-aware datetime derived from the vehicle-start
                        message.  Callers must ensure UTC-aware before passing.
        """
        # A power-on time can never be in the future. Clamp defensively so a
        # bad message timestamp can't poison the power-on time or the duration
        # maths downstream, regardless of the caller.
        now_utc = datetime.now(timezone.utc)
        if started_at > now_utc:
            LOGGER.debug(
                "hint_vehicle_started: VIN %s clamping future start time "
                "%s to now (%s)",
                self.vin,
                started_at,
                now_utc,
            )
            started_at = now_utc

        # Guard: don't regress a more-recent confirmed power-on timestamp
        if (
            self.is_powered_on
            and self.last_powered_on_time is not None
            and self.last_powered_on_time >= started_at
        ):
            LOGGER.debug(
                "hint_vehicle_started: VIN %s already powered on with newer "
                "timestamp (%s >= %s) — no-op",
                self.vin,
                self.last_powered_on_time,
                started_at,
            )
            return

        LOGGER.info(
            "hint_vehicle_started: VIN %s — pre-setting powered-on from "
            "message timestamp %s (was: is_powered_on=%s, last_powered_on=%s)",
            self.vin,
            started_at,
            self.is_powered_on,
            self.last_powered_on_time,
        )

        self.is_powered_on = True
        self.last_powered_on_time = started_at

        # Immediately switch to the powered interval so the next scheduled
        # poll fires at the rapid powered-on cadence, not the slow idle cadence.
        # _adjust_update_interval is the single source of truth for interval
        # selection and scheduling — call it rather than setting update_interval
        # directly, so all action-interval / grace-period guards apply correctly.
        self._adjust_update_interval()

        # Notify listeners so the last_powered_on sensor updates immediately
        # (before the poll confirms it), giving users an accurate start time.
        self.async_update_listeners()

    # Update Options
    async def async_update_options(self, options):
        """Update options and reschedule refresh."""

        # Helper functions to get intervals
        def get_interval(option_key, default_interval):
            """Retrieve interval in minutes from options or fallback to default."""
            return timedelta(
                minutes=options.get(
                    option_key, int(default_interval.total_seconds() / 60)
                )
            )

        def get_interval_hours(option_key, default_interval):
            """Retrieve interval in hours from options or fallback to default."""
            return timedelta(
                hours=options.get(
                    option_key, int(default_interval.total_seconds() / 3600)
                )
            )

        def get_delay(option_key, default_interval):
            """Retrieve delay in seconds from options or fallback to default."""
            return timedelta(
                seconds=options.get(option_key, int(default_interval.total_seconds()))
            )

        # Update all update intervals
        self.default_update_interval = get_interval("update_interval", UPDATE_INTERVAL)
        self.update_interval = self.default_update_interval
        self.charging_update_interval = get_interval(
            "charging_update_interval", UPDATE_INTERVAL_CHARGING
        )
        self.dc_charging_update_interval = get_interval(
            "dc_charging_update_interval", UPDATE_INTERVAL_DC_CHARGING
        )
        self.powered_update_interval = get_interval(
            "powered_update_interval", UPDATE_INTERVAL_POWERED
        )

        self.after_shutdown_update_interval = get_interval(
            "after_shutdown_update_interval", UPDATE_INTERVAL_AFTER_SHUTDOWN
        )
        self.grace_period_update_interval = get_interval(
            "grace_period_update_interval", UPDATE_INTERVAL_GRACE_PERIOD
        )

        self.after_action_delay = get_delay(
            "after_action_delay", AFTER_ACTION_UPDATE_INTERVAL_DELAY
        )

        # Long-interval updates after actions
        self.alarm_long_interval = get_interval(
            "alarm_long_interval", DEFAULT_ALARM_LONG_INTERVAL
        )
        self.ac_long_interval = get_interval(
            "ac_long_interval", DEFAULT_AC_LONG_INTERVAL
        )
        self.front_defrost_long_interval = get_interval(
            "front_defrost_long_interval", DEFAULT_FRONT_DEFROST_LONG_INTERVAL
        )
        self.rear_window_heat_long_interval = get_interval(
            "rear_window_heat_long_interval", DEFAULT_REAR_WINDOW_HEAT_LONG_INTERVAL
        )
        self.lock_unlock_long_interval = get_interval(
            "lock_unlock_long_interval", DEFAULT_LOCK_UNLOCK_LONG_INTERVAL
        )
        self.charging_port_lock_long_interval = get_interval(
            "charging_port_lock_long_interval", DEFAULT_CHARGING_PORT_LOCK_LONG_INTERVAL
        )
        self.heated_seats_long_interval = get_interval(
            "heated_seats_long_interval", DEFAULT_HEATED_SEATS_LONG_INTERVAL
        )
        self.battery_heating_long_interval = get_interval(
            "battery_heating_long_interval", DEFAULT_BATTERY_HEATING_LONG_INTERVAL
        )
        self.charging_long_interval = get_interval(
            "charging_long_interval", DEFAULT_CHARGING_LONG_INTERVAL
        )
        self.sunroof_long_interval = get_interval(
            "sunroof_long_interval", DEFAULT_SUNROOF_LONG_INTERVAL
        )
        self.tailgate_long_interval = get_interval(
            "tailgate_long_interval", DEFAULT_TAILGATE_LONG_INTERVAL
        )
        self.target_soc_long_interval = get_interval(
            "target_soc_long_interval", DEFAULT_TARGET_SOC_LONG_INTERVAL
        )
        self.charging_current_long_interval = get_interval(
            "charging_current_long_interval", DEFAULT_CHARGING_CURRENT_LONG_INTERVAL
        )

        # Update capabilities from options
        self.has_sunroof = options.get("has_sunroof", self.has_sunroof)
        self.has_heated_seats = options.get("has_heated_seats", self.has_heated_seats)
        self.has_rear_heated_seats = options.get(
            "has_rear_heated_seats", self.has_rear_heated_seats
        )
        self.has_battery_heating = options.get(
            "has_battery_heating", self.has_battery_heating
        )
        self.has_steering_wheel_heat = options.get(
            "has_steering_wheel_heat", self.has_steering_wheel_heat
        )
        self.has_window_control = options.get(
            "has_window_control", self.has_window_control
        )
        self.enable_shutdown_refresh_sequence = options.get(
            "enable_shutdown_refresh_sequence", self.enable_shutdown_refresh_sequence
        )
        self.holiday_mode = options.get("holiday_mode", self.holiday_mode)
        self.holiday_update_interval = get_interval_hours(
            CONF_HOLIDAY_UPDATE_INTERVAL, self.holiday_update_interval
        )
        self.stale_data_threshold = timedelta(
            hours=options.get(
                CONF_STALE_DATA_THRESHOLD,
                int(self.stale_data_threshold.total_seconds() / 3600),
            )
        )

        LOGGER.debug(
            f"Update intervals updated via options: "
            f"Default: {self.default_update_interval}, "
            f"Charging: {self.charging_update_interval}, "
            f"Powered: {self.powered_update_interval}, "
            f"After Shutdown: {self.after_shutdown_update_interval}, "
            f"Grace Period: {self.grace_period_update_interval}, "
            f"After Action Delay: {self.after_action_delay}, "
            f"Alarm: {self.alarm_long_interval}, "
            f"AC: {self.ac_long_interval}, "
            f"Front Defrost: {self.front_defrost_long_interval}, "
            f"Rear Window Heat: {self.rear_window_heat_long_interval}, "
            f"Lock/Unlock: {self.lock_unlock_long_interval}, "
            f"Charging Port Lock: {self.charging_port_lock_long_interval}, "
            f"Heated Seats: {self.heated_seats_long_interval}, "
            f"Battery Heating: {self.battery_heating_long_interval}, "
            f"Charging: {self.charging_long_interval}, "
            f"Sunroof: {self.sunroof_long_interval}, "
            f"Tailgate: {self.tailgate_long_interval}, "
            f"Target SOC: {self.target_soc_long_interval}, "
            f"Charging Current: {self.charging_current_long_interval}"
        )

        if not getattr(self, "_action_interval_active", False):
            self._adjust_update_interval()
            # Notify entities so the Next/Last Update Time sensors reflect the
            # new interval immediately (e.g. when holiday mode is toggled, which
            # reschedules without a data fetch). Without this the time sensors
            # would keep showing the previous schedule until the next poll.
            self.async_update_listeners()
        else:
            self.next_update_time = utcnow() + self.update_interval
            self.async_update_listeners()

    async def async_setup(self):
        """Set up the coordinator."""
        self.is_initial_setup = True
        vin = self.vin

        # Trip/efficiency statistics (#301). Load the persisted open/last trip
        # so a drive in progress survives a restart and the sensors repopulate.
        try:
            self.trip_stats = TripStatsManager(
                self.hass, self.config_entry.entry_id, self.vin
            )
            await self.trip_stats.async_load()
        except Exception as e:  # noqa: BLE001 - stats must never block setup
            LOGGER.warning("Trip stats unavailable for VIN %s: %s", self.vin, e)
            self.trip_stats = None

        # Restore last known values for activity and power-off times
        entity_id_last_activity = f"sensor.{DOMAIN}_{self.vin}_last_vehicle_activity"
        entity_id_last_power_off = f"sensor.{DOMAIN}_{self.vin}_last_powered_off"
        entity_id_last_power_on = f"sensor.{DOMAIN}_{self.vin}_last_powered_on"

        last_activity_state = self.hass.states.get(entity_id_last_activity)
        last_power_off_state = self.hass.states.get(entity_id_last_power_off)
        last_power_on_state = self.hass.states.get(entity_id_last_power_on)

        if last_activity_state and last_activity_state.state != "unavailable":
            try:
                self.last_vehicle_activity = datetime.fromisoformat(
                    last_activity_state.state
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                self.last_vehicle_activity = datetime.now(timezone.utc) - timedelta(
                    hours=24
                )
                LOGGER.warning(
                    f"Invalid last_vehicle_activity format: {last_activity_state.state}. Falling back to default."
                )
        else:
            self.last_vehicle_activity = datetime.now(timezone.utc) - timedelta(
                hours=24
            )

        if last_power_off_state and last_power_off_state.state != "unavailable":
            try:
                self.last_powered_off_time = datetime.fromisoformat(
                    last_power_off_state.state
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                self.last_powered_off_time = datetime.now(timezone.utc) - timedelta(
                    hours=24
                )
                LOGGER.warning(
                    f"Invalid last_powered_off format: {last_power_off_state.state}. Falling back to default."
                )
        else:
            self.last_powered_off_time = datetime.now(timezone.utc) - timedelta(
                hours=24
            )

        if last_power_on_state and last_power_on_state.state != "unavailable":
            try:
                self.last_powered_on_time = datetime.fromisoformat(
                    last_power_on_state.state
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                self.last_powered_on_time = datetime.now(timezone.utc) - timedelta(
                    hours=24
                )
                LOGGER.warning(
                    f"Invalid last_powered_on format: {last_power_on_state.state}. Falling back to default."
                )
        else:
            self.last_powered_on_time = datetime.now(timezone.utc) - timedelta(hours=24)

        try:
            await asyncio.wait_for(
                self.async_config_entry_first_refresh(),
                timeout=STARTUP_API_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise ConfigEntryNotReady(
                f"MG SAIC API did not respond within {STARTUP_API_TIMEOUT}s at "
                f"startup for VIN {vin} — HA will retry automatically in the background."
            )
        except Exception as e:
            raise ConfigEntryNotReady(
                f"MG SAIC API unavailable at startup for VIN {vin}: {e} "
                f"— HA will retry automatically in the background."
            )

        if "info" in self.data and self.data["info"]:
            # Find the vehicle info matching the current VIN
            vin_info = next(
                (v for v in self.data.get("info", []) if v.vin == vin), None
            )

            if not vin_info:
                LOGGER.error(f"No vehicle data found for VIN: {vin}")
                raise UpdateFailed("No matching vehicle data found.")

            # Store the matching vehicle info
            self.vin_info = vin_info

            # Get vehicle series from API response
            self.vehicle_series = getattr(vin_info, "series", "").upper()

            # Look up the per-model profile (temperature range/offset and
            # known battery capacity) by matching the series against
            # VEHICLE_PROFILES. Falls back to DEFAULT_VEHICLE_PROFILE for
            # any series not yet profiled (e.g. MG5, ZS EV).
            profile = DEFAULT_VEHICLE_PROFILE
            matched_series_key = None
            for series_key, series_profile in VEHICLE_PROFILES.items():
                if series_key in self.vehicle_series:
                    profile = series_profile
                    matched_series_key = series_key
                    break

            self.min_temp = profile["min_temp"]
            self.max_temp = profile["max_temp"]
            self.temp_offset = profile["temp_offset"]
            self.known_battery_capacity_kwh = profile["battery_capacity_kwh"]
            self.known_fuel_tank_litres = profile.get("fuel_tank_litres")
            self.climate_status_cool = profile.get("climate_status_cool", {3})
            self.climate_status_fan_only = profile.get("climate_status_fan_only", {2})
            self.fan_speed_low = profile.get("fan_speed_low", 1)
            self.fan_speed_medium = profile.get("fan_speed_medium", 3)
            self.fan_speed_high = profile.get("fan_speed_high", 5)
            self.temp_idx_inverted = profile.get("temp_idx_inverted", False)
            self.temp_index_map = profile.get("temp_index_map", None)
            # Climate control scheme + mode_select value map (see const.py).
            self.climate_control_scheme = profile.get("climate_control_scheme", "fan_speed")
            self.climate_mode_fan_only = profile.get("climate_mode_fan_only", 1)
            self.climate_mode_cool = profile.get("climate_mode_cool", 2)
            self.climate_mode_heat = profile.get("climate_mode_heat", 4)
            self.climate_mode_max_cool = profile.get("climate_mode_max_cool", 3)
            self.max_cool_forces_min_temp = profile.get(
                "max_cool_forces_min_temp", False
            )
            self.climate_mode_defrost = profile.get("climate_mode_defrost", 5)
            self.climate_status_heat = profile.get("climate_status_heat", set())
            self.climate_status_defrost = profile.get("climate_status_defrost", set())
            self.heat_fan_speed = profile.get("heat_fan_speed", 2)
            self.climate_fan_auto = profile.get("climate_fan_auto", None)
            self.climate_fan_only_airflow = profile.get(
                "climate_fan_only_airflow", False
            )
            self.supports_target_soc = profile.get("supports_target_soc", True)
            self.reliable_fuel_range_elec = profile.get("reliable_fuel_range_elec", True)
            self.charging_capacity_correction = profile.get("charging_capacity_correction", None)
            self.supports_charging_current_limit = profile.get("supports_charging_current_limit", True)
            self.model_year_override = profile.get("model_year_override", None)
            # Rear door/window presence — from the vehicle profile, not the
            # API's DOOR/WINDOW bitmask (see issue #203; that bitmask data is
            # unreliable for WINDOW across models). Defaults to True (has
            # rear doors/windows) for any unprofiled or 4-door/4-window car.
            self.has_rear_doors = profile.get("has_rear_doors", True)
            self.has_rear_windows = profile.get("has_rear_windows", True)
            self.has_front_passenger_window = profile.get(
                "has_front_passenger_window", True
            )
            self.has_front_defrost = profile.get("has_front_defrost", True)
            self.cool_uses_start_ac = profile.get("cool_uses_start_ac", False)

            LOGGER.debug(
                "Vehicle series detected: %s (profile: %s). "
                "Temperature range: %d-%d°C, Offset: %d, "
                "Temp index inverted: %s, "
                "Fan speeds: low=%d mid=%d high=%d, "
                "Cool status codes: %s, "
                "Climate scheme: %s, "
                "Rear doors: %s, Rear windows: %s",
                self.vehicle_series,
                matched_series_key or "default/unprofiled",
                self.min_temp,
                self.max_temp,
                self.temp_offset,
                self.temp_idx_inverted,
                self.fan_speed_low,
                self.fan_speed_medium,
                self.fan_speed_high,
                self.climate_status_cool,
                self.climate_control_scheme,
                self.has_rear_doors,
                self.has_rear_windows,
            )
            if self.known_battery_capacity_kwh is not None:
                LOGGER.debug(
                    "Known battery capacity override for series %s: %.1f kWh "
                    "(overrides unreliable API-reported value)",
                    self.vehicle_series,
                    self.known_battery_capacity_kwh,
                )

            # NOTE: rear door/window presence is no longer derived from the
            # API's DOOR/WINDOW vehicleModelConfiguration bitmask. That data
            # proved unreliable — MG4 and MGS5 (both 4-door, 4-window cars)
            # report WINDOW='0000' identically to the 2-door Cyberster, which
            # incorrectly suppressed valid rear window entities for them
            # (issue #203). has_rear_doors/has_rear_windows are now set from
            # the VEHICLE_PROFILES entry above instead (see EC32 for the
            # Cyberster override).

        else:
            LOGGER.error(f"No 'info' data found for VIN: {vin}")
            raise ConfigEntryNotReady(
                f"No vehicle info returned by SAIC API for VIN {vin}. "
                f"HA will retry automatically."
            )

        # Update capabilities from options
        self.has_sunroof = self.config_entry.options.get(
            "has_sunroof", self.has_sunroof
        )
        self.has_heated_seats = self.config_entry.options.get(
            "has_heated_seats", self.has_heated_seats
        )
        self.has_rear_heated_seats = self.config_entry.options.get(
            "has_rear_heated_seats", self.has_rear_heated_seats
        )
        self.has_battery_heating = self.config_entry.options.get(
            "has_battery_heating", self.has_battery_heating
        )
        self.has_steering_wheel_heat = self.config_entry.options.get(
            "has_steering_wheel_heat", self.has_steering_wheel_heat
        )
        self.has_window_control = self.config_entry.options.get(
            "has_window_control", self.has_window_control
        )

        self.is_initial_setup = False

        # NOTE: set_alarm_switches and message-queue polling are no longer
        # managed here.  Both are handled by __init__.async_setup_entry under
        # the shared api_lock, and the SAICMGAccountPoller owns the poll loop
        # for the whole account.  See __init__.py and message_poller.py.

        return True

    async def _async_update_data(self):
        """Coordinator entry point.

        Wraps the real update cycle so that a failed cycle doesn't leave the
        coordinator parked on a long idle interval. When the cycle raises before
        interval selection runs (e.g. a transient "return code 4" that exhausts
        its retries), the last successful cycle's interval would otherwise stand
        — which can be several hours — so an active charge that began just before
        the failed poll would go unpolled until the next idle wake-up (#238).

        On failure we shorten the next retry (capped so it never *lengthens* an
        already-short interval), but only for a bounded number of consecutive
        failures, so a car that is genuinely away or asleep isn't polled every
        few minutes forever. Any successful cycle resets the counter.
        """
        try:
            data = await self._run_update_cycle()
        except Exception:
            self._consecutive_update_failures += 1
            self._last_poll_result = DATA_FRESHNESS_FAILED
            if self._consecutive_update_failures <= MAX_FAST_RETRIES_AFTER_FAILURE:
                # Possibly a transient failure at the start of a charge — retry
                # soon, capped so we never lengthen an already-short interval.
                self.update_interval = min(
                    self.update_interval, self.failure_retry_interval
                )
                LOGGER.debug(
                    "Update cycle failed for VIN %s (failure %d/%d); retrying in "
                    "%s instead of the idle interval.",
                    self.vin,
                    self._consecutive_update_failures,
                    MAX_FAST_RETRIES_AFTER_FAILURE,
                    self.update_interval,
                )
            else:
                # Sustained failure: the car is probably genuinely away or
                # asleep. Fall back to the normal idle cadence so we don't keep
                # polling (and risk waking) it every few minutes indefinitely.
                self.update_interval = self.default_update_interval
                LOGGER.debug(
                    "Update cycle still failing for VIN %s after %d fast retries; "
                    "backing off to the idle interval (%s).",
                    self.vin,
                    MAX_FAST_RETRIES_AFTER_FAILURE,
                    self.update_interval,
                )
            raise
        else:
            self._consecutive_update_failures = 0
            return data

    async def _run_update_cycle(self):
        """Fetch data from the API.

        All network calls are made while holding the account-level _api_lock.
        This serialises concurrent fetches across coordinators sharing the same
        SAIC account, preventing session token invalidation when two VINs try
        to refresh simultaneously (the #147 startup race).

        The lock is acquired once for the entire update cycle (info + status +
        charging) rather than per-call, so the three sequential fetches for one
        VIN are never interleaved with fetches for another VIN on the same
        account.
        """
        data = {}
        # Reset the per-cycle marker; note_command_unreachable() sets it if a
        # code 4 is seen during this cycle's fetches (#238 debounce).
        self._code4_this_cycle = False

        # _api_lock is injected by __init__ before async_setup is called.
        # Fall back to a no-op context if somehow not set (single-entry case
        # where __init__ predates this change — belt-and-braces only).
        lock = self._api_lock or asyncio.Lock()

        async with lock:
            # Fetch vehicle info with retries
            data["info"] = (
                await self._fetch_with_retries(
                    self.client.get_vehicle_info,
                    self._is_generic_response_vehicle_info,
                    "vehicle info",
                )
                or []
            )

            if not data["info"]:
                raise UpdateFailed("Cannot proceed without vehicle info.")

            vin = self.config_entry.data.get("vin")
            filtered_info = [v for v in data["info"] if v.vin == vin]
            if not filtered_info:
                raise UpdateFailed(f"No data found for VIN: {vin}")

            # Overwrite info with the filtered result and store it in an attribute
            data["info"] = filtered_info
            self.vin_info = filtered_info[0]

            # Fetch vehicle status with retries.
            # Pass self.vin explicitly — the client is shared across all VINs
            # on the same account, so without an explicit vin it would always
            # fetch status for whichever VIN the client was first constructed
            # with, causing all cars on the account to show the same data.
            vin = self.vin
            try:
                data["status"] = await self._fetch_with_retries(
                    lambda: self.client.get_vehicle_status(vin),
                    self._is_generic_response_vehicle_status,
                    "vehicle status",
                )
                if data["status"] is not None and not self._is_status_timestamp_valid(
                    data["status"]
                ):
                    # Timestamp failed the sanity check — discard the response.
                    # Downstream sensors already retain their last known valid
                    # values, so this degrades gracefully rather than showing
                    # stale/wrong data as if it were current.
                    data["status"] = None
            except Exception as e:
                # During first setup, a vehicle status failure must not prevent
                # the integration from loading.
                if self.is_initial_setup:
                    LOGGER.warning(
                        "Vehicle status unavailable during setup for VIN %s: %s — "
                        "will retry on next scheduled update",
                        self.vin,
                        e,
                    )
                    data["status"] = None
                else:
                    raise

            # Fetch charging info with retries.
            # Same explicit-vin pattern as above.
            # Backend-gated: only fetched where the backend supports charging
            # data at all (e.g. MG India's platform has none — issue #169).
            if self.vehicle_type in ["BEV", "PHEV"] and self.backend_supports(
                Feature.CHARGING_DATA
            ):
                # Charging data is non-essential (status is the core payload) and
                # its endpoint can be slow or fail for long stretches (SAIC-side,
                # return code 4) independently of everything else. Cap the fetch
                # and never let it block or abort the rest of the cycle. On
                # failure proceed with charging=None: the charging sensors retain
                # their last displayed value via their own retention logic, and
                # the coordinator treats the resulting is_charging change as
                # activity so a grace-period poll re-checks soon rather than
                # jumping to the slow idle interval (reported by @HarryFlatter,
                # #262 — a failing charging fetch used to hold up / abort the
                # whole refresh, including manual refreshes).
                charging_timeout = (
                    STARTUP_CHARGING_TIMEOUT
                    if self.is_initial_setup
                    else RUNTIME_CHARGING_TIMEOUT
                )
                try:
                    data["charging"] = await asyncio.wait_for(
                        self._fetch_with_retries(
                            lambda: self.client.get_charging_info(vin),
                            self._is_generic_response_charging,
                            "charging info",
                        ),
                        timeout=charging_timeout,
                    )
                except asyncio.TimeoutError:
                    LOGGER.warning(
                        "Charging info did not return within %ss for VIN %s — "
                        "proceeding without it; will retry on the next update",
                        charging_timeout,
                        self.vin,
                    )
                    data["charging"] = None
                except Exception as e:
                    LOGGER.warning(
                        "Charging info unavailable for VIN %s: %s — proceeding "
                        "without it; will retry on the next update",
                        self.vin,
                        e,
                    )
                    data["charging"] = None

            # Fetch the scheduled battery heating configuration (cheap GET).
            # Non-fatal: on failure, retain the last known value so the
            # schedule entities do not flap during SAIC API outages.
            # Backend-gated alongside the vehicle-level flag.
            if self.has_battery_heating and self.backend_supports(
                Feature.BATTERY_HEATING
            ):
                try:
                    data["battery_heating_schedule"] = await self.client.get_battery_heating_schedule(vin)
                except Exception as e:
                    previous = (self.data or {}).get("battery_heating_schedule")
                    data["battery_heating_schedule"] = previous
                    LOGGER.debug(
                        "Battery heating schedule unavailable for VIN %s: %s — "
                        "retaining previous value",
                        self.vin,
                        e,
                    )

        # Determine charging status
        self.is_charging = False
        self.is_dc_charging = False
        if data.get("charging") is not None:
            chrg_data = getattr(data["charging"], "chrgMgmtData", None)
            if chrg_data is not None:
                bms_chrg_sts = getattr(chrg_data, "bmsChrgSts", None)
                self.is_charging = bms_chrg_sts in CHARGING_STATUS_CODES
                # bmsChrgSts 10 = DC charging, 11 = super offboard DC charging
                self.is_dc_charging = bms_chrg_sts in {10, 11}
        else:
            LOGGER.debug("Charging data not available.")

        # Update internal state variables
        self._update_state(data)

        # Adjust update intervals dynamically
        self._adjust_update_interval()

        # Log data
        LOGGER.debug("Vehicle Type: %s", self.vehicle_type)
        LOGGER.debug("Vehicle Info: %s", data.get("info"))
        LOGGER.debug("Vehicle Status: %s", data.get("status"))
        LOGGER.debug("Vehicle Charging Data: %s", data.get("charging"))
        LOGGER.debug(
            f"State updated: Is Powered On: {self.is_powered_on}, Is Charging: {self.is_charging}, "
            f"Last Powered On Time: {self.last_powered_on_time}, "
            f"Last Powered Off Time: {self.last_powered_off_time}, "
            f"Last Vehicle Activity: {self.last_vehicle_activity}, "
            f"Update Interval: {self.update_interval}"
        )

        # Set the last update time
        self.last_update_time = datetime.now(timezone.utc)
        # Clear the "unreachable" (code 4) flag when we have positive proof the
        # car is reachable again. Two things qualify:
        #   1. it reports powered-on, OR
        #   2. it returned a status response whose statusTime ADVANCED beyond
        #      the last one we saw — i.e. the telematics actually reported fresh
        #      data this cycle.
        # A *cached* status poll is still NOT sufficient: the SAIC backend
        # serves cached status (same, unchanged statusTime) even while the car
        # is asleep and rejecting live commands, so clearing on any status
        # success would wrongly show "awake" while commands keep failing. Using
        # statusTime advancement rather than mere success preserves that
        # guarantee while also releasing the flag once the car genuinely wakes
        # and answers a poll (previously it stayed stuck on 'unreachable' until
        # the car was driven — the flip-side of now SETTING the flag on
        # status-poll code 4). A successful command also clears it (see
        # record_command_ok). See #238.
        fresh_status = False
        new_status = data.get("status")
        if new_status is not None:
            new_status_time = getattr(new_status, "statusTime", None)
            if new_status_time is not None and (
                self._last_status_time is None
                or new_status_time > self._last_status_time
            ):
                fresh_status = True
                self._last_status_time = new_status_time
        self._update_reachability_after_poll(fresh_status)
        # Record how current this cycle's data was, for the Data Freshness
        # sensor. A poll that returns unchanged/cached status is "cached", not
        # "live" — the same distinction the reachability debounce relies on.
        self._last_poll_result = (
            DATA_FRESHNESS_LIVE if fresh_status else DATA_FRESHNESS_CACHED
        )

        # Include capabilities in the returned data
        data["capabilities"] = {
            "has_sunroof": self.has_sunroof,
            "has_heated_seats": self.has_heated_seats,
            "has_rear_heated_seats": self.has_rear_heated_seats,
            "has_battery_heating": self.has_battery_heating,
            "has_steering_wheel_heat": self.has_steering_wheel_heat,
            "has_window_control": self.has_window_control,
        }

        # Push telemetry to ABRP, but only on a genuinely fresh status response
        # (advanced statusTime) so we never feed ABRP a cached/stale SoC. Reuses
        # the freshness signal computed above for the reachability flag.
        await self._maybe_send_abrp(data, fresh_status)

        return data

    async def _maybe_send_abrp(self, data, fresh_status):
        """Send this cycle's telemetry to ABRP if configured and fresh.

        No-ops silently unless a user token is set for this vehicle. Never
        raises — an ABRP failure must not affect the coordinator update.
        """
        if not fresh_status:
            return

        options = self.config_entry.options
        user_token = (options.get(CONF_ABRP_USER_TOKEN) or "").strip()
        if not user_token:
            return  # ABRP not enabled for this vehicle

        api_key = (options.get(CONF_ABRP_API_KEY) or "").strip()
        if not api_key:
            LOGGER.debug(
                "ABRP: user token set for VIN %s but no API key — "
                "both credentials are required; skipping",
                self.vin,
            )
            return

        status = data.get("status")
        if status is None:
            return

        try:
            from .abrp import AbrpApi

            session = async_get_clientsession(self.hass)
            sent, message = await AbrpApi(session, api_key, user_token).async_send(
                status, data.get("charging")
            )
            if sent:
                LOGGER.debug("ABRP telemetry sent for VIN %s", self.vin)
            else:
                LOGGER.debug(
                    "ABRP telemetry not sent for VIN %s: %s", self.vin, message
                )
        except Exception as err:  # noqa: BLE001 - never break the update loop
            LOGGER.warning("ABRP telemetry failed for VIN %s: %s", self.vin, err)

    # Update Vehicle State
    # ── Trip statistics (#301) ───────────────────────────────────────────────

    def _trip_snapshot(self, basic_status, charging_data):
        """Build a TripSnapshot from the current poll's data.

        Mirrors the odometer/SOC extraction used by the mileage and SOC
        sensors (decimal correction, uint16 saturation, -128 sentinels), so a
        trip boundary reads the same numbers the user sees. Returns None if we
        can't establish a valid odometer reading.
        """
        odometer = self._extract_odometer_km(basic_status, charging_data)
        if odometer is None:
            return None
        return TripSnapshot(
            ts=datetime.now(timezone.utc).isoformat(),
            odometer_km=odometer,
            soc_pct=self._extract_soc_pct(basic_status, charging_data),
            fuel_pct=self._extract_fuel_pct(basic_status),
        )

    @staticmethod
    def _extract_odometer_km(basic_status, charging_data):
        """Odometer in km, or None. Rejects 0/-128 and the uint16 saturation."""
        for source, factor in ((basic_status, DATA_DECIMAL_CORRECTION),):
            raw = getattr(source, "mileage", None) if source is not None else None
            if raw is not None and raw > 0 and raw != MILEAGE_UINT16_SATURATION:
                return raw * factor
        # Fall back to the wider odometer field in charging data.
        chrg = getattr(charging_data, "chrgMgmtData", None) if charging_data else None
        raw = getattr(chrg, "mileage", None) if chrg is not None else None
        if raw is not None and raw > 0:
            return raw * DATA_DECIMAL_CORRECTION
        return None

    @staticmethod
    def _extract_soc_pct(basic_status, charging_data):
        """SOC % — charging bmsPackSOCDsp (×0.1), else basic extendedData1 (int %).

        Mirrors the SOC sensor's own fallback so a momentary charging-endpoint
        dropout doesn't leave a trip snapshot with no SOC — which would drop the
        whole trip's efficiency (energy needs a start AND end SOC).
        """
        chrg = getattr(charging_data, "chrgMgmtData", None) if charging_data else None
        raw = getattr(chrg, "bmsPackSOCDsp", None) if chrg is not None else None
        if raw is not None and raw != -128:
            return raw * DATA_DECIMAL_CORRECTION_SOC
        raw = getattr(basic_status, "extendedData1", None) if basic_status else None
        if raw is not None and raw not in (-128, -1):
            return float(raw)
        return None

    @staticmethod
    def _extract_fuel_pct(basic_status):
        """Fuel level % from basicVehicleStatus (0 is a valid reading), or None."""
        raw = getattr(basic_status, "fuelLevelPrc", None) if basic_status else None
        if raw is not None and 0 <= raw <= 100:
            return float(raw)
        return None

    def _update_trip_state(self, power_mode, basic_status, charging_data):
        """Open/close a trip based on the current power mode (#301).

        Keyed off power_mode + whether a trip is already open — NOT the
        is_powered_on transition, which the 323 start-message hint pre-sets
        before the poll runs (so a transition-based hook would never fire an
        open). This is also robust to HA restarts mid-drive and installing the
        feature mid-drive. State is mutated synchronously; only the persist is
        scheduled.
        """
        if self.trip_stats is None:
            return
        driving = power_mode in (2, 3)
        open_snap = self.trip_stats.open_snapshot
        if driving and open_snap is None:
            snap = self._trip_snapshot(basic_status, charging_data)
            if snap is not None and self.trip_stats.open(snap):
                LOGGER.debug("Trip opened for VIN %s at %s km", self.vin, snap.odometer_km)
                self._schedule_trip_save()
        elif not driving and open_snap is not None:
            snap = self._trip_snapshot(basic_status, charging_data)
            if snap is not None:
                trip = self.trip_stats.close(
                    snap,
                    capacity_kwh=self.known_battery_capacity_kwh,
                    tank_litres=self.known_fuel_tank_litres,
                    is_electric=self.vehicle_type in ("BEV", "PHEV"),
                    is_combustion=self.vehicle_type in ("ICE", "HEV", "PHEV"),
                )
                LOGGER.debug("Trip closed for VIN %s: %s", self.vin, trip)
                self._schedule_trip_save()

    def _schedule_trip_save(self):
        """Persist trip state in the background (best-effort)."""
        try:
            self.config_entry.async_create_background_task(
                self.hass,
                self.trip_stats.async_save(),
                f"mg_saic_trip_save_{self.vin}",
            )
        except Exception as e:  # noqa: BLE001 - persistence must never break a poll
            LOGGER.debug("Trip save scheduling failed for VIN %s: %s", self.vin, e)

    def _update_state(self, data):
        """Update state variables based on fetched data."""
        status_data = data.get("status")
        charging_data = data.get("charging")
        recent_activity = False

        # Vehicle status
        if status_data:
            basic_status = getattr(status_data, "basicVehicleStatus", None)
            if basic_status is None:
                LOGGER.warning("basicVehicleStatus is not available in Status Data.")
                return

            # Clear the optimistic ventilation flag if the windows have closed.
            self._update_ventilation_from_status(basic_status)

            power_mode = getattr(basic_status, "powerMode", None)

            # Trip open/close (#301) — evaluated every poll from power_mode, so
            # it's independent of the is_powered_on transition (which the 323
            # start-message hint pre-sets before this poll runs).
            self._update_trip_state(power_mode, basic_status, charging_data)

            # Detect Power State
            # Track previous state so we catch the transition even if a prior
            # poll returned None (generic response during power-down window)
            self._prev_is_powered_on = self.is_powered_on
            if power_mode in [2, 3]:
                if not self.is_powered_on:
                    self.last_powered_on_time = datetime.now(timezone.utc)
                self.is_powered_on = True
            else:
                if self.is_powered_on:
                    self.last_powered_off_time = datetime.now(timezone.utc)
                    LOGGER.info(
                        "Vehicle powered off detected for VIN %s — "
                        "%s post-shutdown refresh sequence",
                        self.vin,
                        "starting" if self.enable_shutdown_refresh_sequence else "skipping (disabled)",
                    )
                    if self.enable_shutdown_refresh_sequence:
                        self._start_shutdown_refresh_sequence()
                self.is_powered_on = False

            # Detect vehicle activity
            recent_activity = self._detect_activity(basic_status, charging_data)

        # Charging status
        self.is_charging = False
        if charging_data:
            chrg_mgmt_data = getattr(charging_data, "chrgMgmtData", None)
            if chrg_mgmt_data:
                self.is_charging = (
                    getattr(chrg_mgmt_data, "bmsChrgSts", None) in CHARGING_STATUS_CODES
                )

        # A charging -> not-charging transition (charge complete, or the
        # charging endpoint dropping out) is registered as activity so the
        # grace-period poll re-checks soon, instead of the interval jumping
        # straight to the slow idle poll and missing the "Charging Complete /
        # Connecting" transition (reported by @HarryFlatter, #262). Note that a
        # failed charging fetch drops charging_data to None, which flips
        # is_charging to False — so this also recovers quickly when the charging
        # endpoint blips. We only trigger on charge-stop; plug-in is already
        # caught by the lock/shutdown-sequence logic.
        if self._prev_is_charging and not self.is_charging:
            LOGGER.debug(
                "Charging stopped/dropped for VIN %s — flagging activity so a "
                "grace-period poll re-checks",
                self.vin,
            )
            recent_activity = True
        self._prev_is_charging = self.is_charging

        # Missed-transition guard: if vehicle status was unavailable (None) but
        # charging data confirms the car is now charging, we know the car must
        # have powered off. Fire the shutdown sequence if we haven't already.
        if (
            not status_data
            and self.is_charging
            and self._prev_is_powered_on
            and self.is_powered_on
        ):
            if self._shutdown_refresh_task is None or self._shutdown_refresh_task.done():
                LOGGER.info(
                    "Charging detected after status unavailable for VIN %s — "
                    "inferring shutdown, %s post-shutdown refresh sequence",
                    self.vin,
                    "starting" if self.enable_shutdown_refresh_sequence else "skipping (disabled)",
                )
                self.last_powered_off_time = datetime.now(timezone.utc)
                self.is_powered_on = False
                if self.enable_shutdown_refresh_sequence:
                    self._start_shutdown_refresh_sequence()

        # Update activity timestamp
        if recent_activity:
            new_activity_time = datetime.now(timezone.utc)
            if self.last_vehicle_activity != new_activity_time:
                self.last_vehicle_activity = new_activity_time
                LOGGER.debug(
                    "Updated Last Vehicle Activity: %s", self.last_vehicle_activity
                )
            # Genuine detected activity (doors, lock, engine, journey change) is
            # positive proof the car is awake — clear any stale unreachable flag.
            # This is distinct from a plain cached-status poll, which is not.
            self._mark_reachable()

        # Notify listeners of data changes
        self.async_update_listeners()

    # Chech Vehicle Activity
    def _detect_activity(self, basic_status, charging_data=None):
        """Detect recent activity based on changes in vehicle status and charging.

        Lock-to-locked transition (0 → 1) is treated as a special trigger:
        when the car locks while not already charging, it almost certainly means
        the occupants have just arrived home and may be about to plug in.  We
        start the post-shutdown rapid refresh sequence immediately on lock so
        that plug-in is detected within 1-3 minutes without waiting for SAIC's
        slow poweroff message or the background poll timer.

        This is strictly better than waiting for poweroff detection because:
        - Lock is a real-time state change from the vehicle status API (no SAIC
          message queue delay)
        - Charging port must be opened before locking, so lock always follows
          plug-in at home
        - The sequence exits as soon as is_charging is True, so false triggers
          (locking at a shop) just run a few extra polls then stop harmlessly
        """
        activity_keys = [
            "lockStatus",
            "driverDoor",
            "passengerDoor",
            "rearLeftDoor",
            "rearRightDoor",
            "bootStatus",
            "bonnetStatus",
            "remoteClimateStatus",
            "rmtHtdRrWndSt",
            "engineStatus",
        ]
        detected_activity = False
        lock_just_engaged = False

        # Check for door, lock, and other physical activity
        for key in activity_keys:
            current_value = getattr(basic_status, key, None)
            last_value = getattr(self, f"_last_{key}", None)
            if current_value != last_value:
                LOGGER.debug(
                    "Detected activity for %s: previous=%s, current=%s",
                    key,
                    last_value,
                    current_value,
                )
                # Detect the specific locked transition (unlocked → locked)
                if key == "lockStatus" and last_value == 0 and current_value == 1:
                    lock_just_engaged = True
                setattr(self, f"_last_{key}", current_value)
                detected_activity = True

        # Check for power state changes
        power_mode = getattr(basic_status, "powerMode", None)
        if power_mode is not None and power_mode != getattr(
            self, "_last_power_mode", None
        ):
            LOGGER.debug(
                "Detected power mode change: previous=%s, current=%s",
                getattr(self, "_last_power_mode", None),
                power_mode,
            )
            self._last_power_mode = power_mode
            detected_activity = True

        # Check for charging status changes
        if charging_data:
            charging_status = getattr(charging_data, "bmsChrgSts", None)
            if charging_status != getattr(self, "_last_charging_status", None):
                LOGGER.debug(
                    "Detected charging status change: previous=%s, current=%s",
                    getattr(self, "_last_charging_status", None),
                    charging_status,
                )
                self._last_charging_status = charging_status
                detected_activity = True

        # Lock-engaged trigger: start the post-shutdown rapid refresh sequence
        # when the car locks while not already actively charging.
        # This catches the "just arrived home, about to plug in" scenario without
        # any dependency on the slow SAIC poweroff notification.
        if (
            lock_just_engaged
            and not self.is_charging
            and self.enable_shutdown_refresh_sequence
        ):
            if self._shutdown_refresh_task is None or self._shutdown_refresh_task.done():
                LOGGER.info(
                    "Lock engaged for VIN %s while not charging — "
                    "starting post-shutdown refresh sequence to catch plug-in",
                    self.vin,
                )
                self._start_shutdown_refresh_sequence()
            else:
                LOGGER.debug(
                    "Lock engaged for VIN %s but shutdown sequence already running — "
                    "not starting a second one",
                    self.vin,
                )

        # Log no activity detected
        if not detected_activity:
            LOGGER.debug("No changes detected in monitored keys or charging status.")

        return detected_activity

    # Adjust Update Intervals
    def _adjust_update_interval(self):
        """Adjust update interval dynamically based on state."""
        if getattr(self, "_action_interval_active", False):
            # If we're in an action interval sequence, do not override intervals.
            LOGGER.debug(
                "Action interval active, skipping dynamic interval adjustment."
            )
            return

        now = datetime.now(timezone.utc)

        # Use restored or initialized timestamps for calculations
        last_powered_off_time = self.last_powered_off_time or (
            now - timedelta(hours=24)
        )
        last_vehicle_activity = self.last_vehicle_activity or (
            now - timedelta(hours=24)
        )

        # Calculate durations since last activity or powered-off state
        idle_duration = now - last_powered_off_time
        activity_duration = now - last_vehicle_activity

        LOGGER.debug(
            "Evaluating interval adjustment: Powered On: %s, Charging: %s, "
            "Idle Duration: %s, Activity Duration: %s",
            self.is_powered_on,
            self.is_charging,
            idle_duration,
            activity_duration,
        )

        # Determine update interval based on state and recent activity
        self.update_interval = select_update_interval(
            is_powered_on=self.is_powered_on,
            is_charging=self.is_charging,
            is_dc_charging=self.is_dc_charging,
            idle_duration=idle_duration,
            activity_duration=activity_duration,
            default_update_interval=self.default_update_interval,
            powered_update_interval=self.powered_update_interval,
            charging_update_interval=self.charging_update_interval,
            dc_charging_update_interval=self.dc_charging_update_interval,
            grace_period_update_interval=self.grace_period_update_interval,
            after_shutdown_update_interval=self.after_shutdown_update_interval,
            holiday_mode=self.holiday_mode,
            holiday_update_interval=self.holiday_update_interval,
        )

        if self.is_powered_on:
            LOGGER.debug("Vehicle is powered on. Using powered update interval.")
        elif self.is_dc_charging:
            LOGGER.debug("Vehicle is DC charging. Using DC charging update interval.")
        elif self.is_charging:
            LOGGER.debug("Vehicle is AC charging. Using charging update interval.")
        elif self.update_interval == self.grace_period_update_interval:
            LOGGER.debug("Within grace period. Using grace period interval.")
        elif self.update_interval == self.after_shutdown_update_interval:
            LOGGER.debug("Within shutdown window. Using shutdown interval.")
        else:
            LOGGER.debug("No recent activity. Using default update interval.")

        # Log and schedule the next refresh
        LOGGER.debug(f"Adjusted update interval: {self.update_interval}.")
        self._schedule_refresh()

    # Additional Update Intervals for Actions and Confirmation
    async def schedule_action_refresh(self, vin, immediate_interval, long_interval):
        """Schedule non-blocking follow-up refreshes after an action."""
        # A command reached this point, which means it succeeded (failures raise
        # before here) — positive proof the car is reachable, so clear any
        # unreachable flag. Covers every command centrally.
        self.note_command_ok()
        self._action_refresh_generation += 1
        generation = self._action_refresh_generation

        if self._action_refresh_task and not self._action_refresh_task.done():
            self._action_refresh_task.cancel()

        self._action_refresh_task = self.hass.async_create_task(
            self._run_action_refresh_sequence(
                vin,
                immediate_interval,
                long_interval,
                generation,
            )
        )

    async def _run_action_refresh_sequence(
        self,
        vin,
        immediate_interval,
        long_interval,
        generation,
    ):
        """Run action follow-up refreshes in the background."""
        self._action_interval_active = True

        try:
            self.update_interval = immediate_interval
            self.next_update_time = utcnow() + self.update_interval
            self.async_update_listeners()
            LOGGER.debug(
                "Scheduling immediate refresh with interval %s for VIN: %s.",
                immediate_interval,
                vin,
            )
            await self.async_request_refresh()

            await asyncio.sleep(immediate_interval.total_seconds())

            self.update_interval = long_interval
            self.next_update_time = utcnow() + self.update_interval
            self.async_update_listeners()
            LOGGER.debug(
                "Switching to long interval %s for VIN: %s after immediate refresh.",
                long_interval,
                vin,
            )
            await self.async_request_refresh()

            await asyncio.sleep(long_interval.total_seconds())
        except asyncio.CancelledError:
            LOGGER.debug("Cancelled action refresh sequence for VIN: %s.", vin)
            raise
        finally:
            if generation == self._action_refresh_generation:
                self._action_interval_active = False
                self._action_refresh_task = None
                self._adjust_update_interval()


    # ── Post-shutdown rapid refresh sequence ─────────────────────────────────

    def _start_shutdown_refresh_sequence(self) -> None:
        """Kick off a background task that polls rapidly after engine-off.

        Because the SAIC REST API has no dedicated shutdown alarm type, the
        coordinator may not poll again for up to 15 minutes after the car
        turns off (it was on the powered-on interval). This sequence fires
        a series of extra refreshes at POST_SHUTDOWN_REFRESH_SEQUENCE intervals
        so that plug-in events are detected within ~1-25 minutes (refreshes at
        1, 3, 7, 15 and 25 minutes), exiting early as soon as charging is seen.
        """
        # Cancel any existing shutdown refresh from a previous cycle
        if self._shutdown_refresh_task and not self._shutdown_refresh_task.done():
            self._shutdown_refresh_task.cancel()

        self._shutdown_refresh_task = self.config_entry.async_create_background_task(
            self.hass,
            self._run_shutdown_refresh_sequence(),
            f"mg_saic_shutdown_refresh_{self.vin}",
        )

    async def _run_shutdown_refresh_sequence(self) -> None:
        """Run the post-shutdown refresh sequence."""
        try:
            for delay in POST_SHUTDOWN_REFRESH_SEQUENCE:
                await asyncio.sleep(delay)
                LOGGER.info(
                    "Post-shutdown refresh for VIN %s (delay was %ds)",
                    self.vin,
                    delay,
                )
                await self.async_request_refresh()
                # If the car is now charging, the coordinator interval will have
                # already switched to charging interval — we can stop early.
                if self.is_charging:
                    LOGGER.info(
                        "Charging detected for VIN %s — ending post-shutdown sequence",
                        self.vin,
                    )
                    break
        except asyncio.CancelledError:
            LOGGER.debug(
                "Post-shutdown refresh sequence cancelled for VIN %s", self.vin
            )
            raise
        finally:
            self._shutdown_refresh_task = None

    def register_command_error_event_entity(self, entity) -> None:
        """Register (or deregister, with entity=None) the command-error Event
        entity so the coordinator can fire events through it.

        Called by SAICMGCommandErrorEvent.async_added_to_hass /
        async_will_remove_from_hass — entities never need to call this
        directly.
        """
        self._command_error_event_entity = entity

    async def notify_command_limit_reached(
        self, vin: str, source: str | None = None
    ) -> None:
        """Fire a persistent notification when the remote command limit is reached.

        The SAIC API returns return code 8 when the vehicle has received too many
        remote commands without a physical key start to reset the counter. This
        notification surfaces that clearly in the HA UI rather than silently
        failing or only logging to the error log.

        Also fires a command_limit_reached event via the command-error Event
        entity (if registered), giving a queryable Logbook history of every
        time this has happened, alongside the actionable notification.

        Args:
            vin: the vehicle's VIN.
            source: optional short identifier of which command triggered the
                limit (e.g. "climate.set_hvac_mode"), included in the event
                data for diagnostics. Existing callers that don't pass this
                still work — it simply falls back to a generic label.
        """
        # Include the vehicle's brand/model name alongside the VIN so the
        # notification is identifiable at a glance in multi-vehicle setups,
        # not just by VIN. Falls back gracefully if vin_info isn't available
        # for any reason (e.g. very early in setup).
        vin_info = getattr(self, "vin_info", None)
        if vin_info is not None:
            vehicle_label = (
                f"{vin_info.brandName} {vin_info.modelName} (VIN: {vin})"
            )
        else:
            vehicle_label = f"VIN: {vin}"

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "MG SAIC: Remote Command Limit Reached",
                "message": (
                    f"The vehicle {vehicle_label} has reached the maximum number "
                    "of remote commands allowed without a physical key start.\n\n"
                    "**To reset:** Start the vehicle with the physical key, then "
                    "remote commands will work again."
                ),
                "notification_id": f"mg_saic_command_limit_{vin}",
            },
        )
        LOGGER.warning(
            "Persistent notification fired: remote command limit reached for %s",
            vehicle_label,
        )

        if self._command_error_event_entity is not None:
            self._command_error_event_entity.record_command_limit_reached(
                source or "unknown command"
            )

    def is_climate_blocking_defrost(self) -> bool:
        """True when a running climate mode would block front defrost.

        The iSmart app refuses to send front defrost while the AC is already
        running, telling the user to "turn off AC Auto mode" first. This is
        confirmed by a decrypted capture (2026-07-16): pressing front defrost
        with the AC on (remoteClimateStatus=2) sent NO command at all — the app
        blocked it client-side. Only after the AC was switched off
        (remoteClimateStatus=0) did the defrost command go through.

        We mirror that guard so the user gets a clear, actionable message
        rather than a command that the vehicle silently ignores — and which
        would still count against the vehicle's daily remote-command limit.

        Off (0) and defrost itself (5) do not block. Any other active climate
        mode does: cool / max-cool / heat / fan-only, or 6 (the climate running
        under local in-car control).

        NOTE: only the cool case (2) is confirmed to be blocked by the app; the
        other active modes are blocked here on the same principle but are
        unverified. Over-blocking fails safe — the user is told to turn the AC
        off, which they can do — whereas under-blocking silently wastes one of
        the vehicle's limited remote commands.
        """
        # Only meaningful on backends that actually offer front defrost. The
        # India (TAP) backend does not expose front defrost and synthesises
        # remoteClimateStatus into 0/2 only, so guarding there would wrongly
        # block on a value that doesn't carry the same meaning. Gate on the
        # capability so this stays correct if profiles change in future.
        from .backends import Feature

        if not self.backend_supports(Feature.FRONT_DEFROST):
            return False

        status = self.data.get("status") if self.data else None
        basic_status = getattr(status, "basicVehicleStatus", None)
        if basic_status is None:
            return False
        remote_climate = getattr(basic_status, "remoteClimateStatus", None)
        if remote_climate is None:
            return False
        return remote_climate not in (
            REMOTE_CLIMATE_STATUS_OFF,
            REMOTE_CLIMATE_STATUS_DEFROST,
        )

    async def notify_front_defrost_blocked(
        self, vin: str, source: str | None = None
    ) -> None:
        """Fire a persistent notification when front defrost is blocked.

        Mirrors notify_command_limit_reached: raises a persistent notification
        in the HA UI so the user knows why nothing happened, and records a
        command_error event so there is a queryable Logbook history.

        Args:
            vin: the vehicle's VIN.
            source: short identifier of which control was used, e.g.
                "switch.front_defrost.turn_on" or "climate.set_preset_mode".
        """
        vin_info = getattr(self, "vin_info", None)
        if vin_info is not None:
            vehicle_label = f"{vin_info.brandName} {vin_info.modelName} (VIN: {vin})"
        else:
            vehicle_label = f"VIN: {vin}"

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "MG SAIC: Front Defrost Blocked",
                "message": (
                    f"Front defrost was not started on {vehicle_label} because "
                    "the air conditioning is already running.\n\n"
                    "**To fix:** turn the air conditioning off first, then start "
                    "front defrost.\n\n"
                    "This mirrors the iSmart app, which also requires AC Auto "
                    "mode to be turned off before front defrost can be used. The "
                    "command was not sent, so it has not used up one of the "
                    "vehicle's limited remote commands."
                ),
                "notification_id": f"mg_saic_front_defrost_blocked_{vin}",
            },
        )
        LOGGER.warning(
            "Front defrost blocked (AC already running) for %s", vehicle_label
        )
        self.record_command_error(
            source or "front_defrost",
            "Front defrost blocked: the air conditioning is already running",
        )

    def is_climate_blocking_airflow(self) -> bool:
        """True when the AC is running and would block AC Airflow.

        The MG HS PHEV (and its app) require the AC to be turned off before the
        separate AC Airflow ventilation mode can be enabled — the app shows
        "To turn on airflow, please turn off AC Auto mode" and blocks it
        client-side (confirmed by decrypted capture, #262). Sending it with the
        AC on would just be rejected by the car while still using up one of the
        3 limited remote commands, so we guard it the same way as front defrost
        and warn the user instead of auto-switching the AC off (which would also
        cost a command).

        Only meaningful on profiles that map Fan Only to AC Airflow. Off (0) and
        the car's fan-only/airflow status itself do not block; any other active
        climate status does.
        """
        if not self.climate_fan_only_airflow:
            return False
        status = self.data.get("status") if self.data else None
        basic_status = getattr(status, "basicVehicleStatus", None)
        if basic_status is None:
            return False
        remote_climate = getattr(basic_status, "remoteClimateStatus", None)
        if remote_climate is None:
            return False
        return remote_climate not in (
            REMOTE_CLIMATE_STATUS_OFF,
            *self.climate_status_fan_only,
        )

    async def notify_ac_airflow_blocked(
        self, vin: str, source: str | None = None
    ) -> None:
        """Fire a persistent notification when AC Airflow is blocked.

        Mirrors notify_front_defrost_blocked: the command is NOT sent (so it
        doesn't waste one of the vehicle's limited remote commands), and the
        user is told to turn the AC off first.
        """
        vin_info = getattr(self, "vin_info", None)
        if vin_info is not None:
            vehicle_label = f"{vin_info.brandName} {vin_info.modelName} (VIN: {vin})"
        else:
            vehicle_label = f"VIN: {vin}"

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "MG SAIC: AC Airflow Blocked",
                "message": (
                    f"AC Airflow was not started on {vehicle_label} because the "
                    "air conditioning is already running.\n\n"
                    "**To fix:** turn the air conditioning off first, then select "
                    "Fan Only to start AC Airflow.\n\n"
                    "This mirrors the iSmart app, which requires AC Auto mode to "
                    "be turned off before AC Airflow can be used. The command was "
                    "not sent, so it has not used up one of the vehicle's limited "
                    "remote commands."
                ),
                "notification_id": f"mg_saic_ac_airflow_blocked_{vin}",
            },
        )
        LOGGER.warning(
            "AC Airflow blocked (AC already running) for %s", vehicle_label
        )
        self.record_command_error(
            source or "ac_airflow",
            "AC Airflow blocked: the air conditioning is already running",
        )

    def set_ventilation_active(self, active: bool) -> None:
        """Set/clear the optimistic ventilation flag (called by window buttons)."""
        self.ventilation_active = active
        # Reset the "seen open" latch each time we (re)assert or clear state.
        self._ventilation_windows_seen_open = False
        LOGGER.debug(
            "Ventilation flag set to %s for VIN %s", active, getattr(self, "vin", "?")
        )

    def _update_ventilation_from_status(self, basic_status) -> None:
        """Clear the ventilation flag when the windows report closed.

        Only clears after the windows have been observed open at least once
        since the ventilate command, so the brief window between pressing
        ventilate and the car actioning it (windows still reading closed)
        does not immediately switch the sensor back off.
        """
        if not self.ventilation_active or basic_status is None:
            return
        from .const import WINDOW_STATUS_FIELDS

        any_open = any(
            getattr(basic_status, field, 0) == 1 for field in WINDOW_STATUS_FIELDS
        )
        all_closed = all(
            getattr(basic_status, field, 0) in (0, None)
            for field in WINDOW_STATUS_FIELDS
        )
        if any_open:
            self._ventilation_windows_seen_open = True
        elif all_closed and self._ventilation_windows_seen_open:
            LOGGER.debug(
                "Windows reported closed after ventilation for VIN %s; "
                "clearing ventilation flag.",
                getattr(self, "vin", "?"),
            )
            self.ventilation_active = False
            self._ventilation_windows_seen_open = False

    async def async_set_holiday_mode(self, enabled: bool) -> None:
        """Turn holiday mode on/off and persist it.

        Writes ONLY the holiday_mode flag into the config entry options (never
        the user's configured intervals), which triggers the options update
        listener -> async_update_options, re-reading the flag and rescheduling
        the next poll at the holiday (or normal) cadence. Persisting to options
        means the setting survives a Home Assistant restart — important, since
        the whole point is to leave the car alone while you're away.
        """
        self.holiday_mode = enabled
        new_options = {**self.config_entry.options, "holiday_mode": enabled}
        self.hass.config_entries.async_update_entry(
            self.config_entry, options=new_options
        )
        LOGGER.info(
            "Holiday mode %s for VIN %s",
            "enabled" if enabled else "disabled",
            getattr(self, "vin", "?"),
        )

    def note_command_ok(self) -> None:
        """Clear the unreachable flag after a command SUCCEEDS.

        A successful remote command is positive proof the car is reachable, so
        it clears the flag immediately (unlike a status poll, which can be
        served from cache while the car is asleep).
        """
        if self._last_command_unreachable:
            LOGGER.debug(
                "Command succeeded; clearing unreachable flag for VIN %s",
                getattr(self, "vin", "?"),
            )
        self._mark_reachable()

    def _mark_reachable(self) -> None:
        """Clear the unreachable flag and reset the debounce counter.

        Called on positive proof the car is reachable: a status response with a
        fresh (advanced) statusTime, genuine detected activity, or a successful
        live command.
        """
        self._last_command_unreachable = False
        self._last_command_unreachable_time = None
        self._consecutive_unreachable_polls = 0

    def _update_reachability_after_poll(self, fresh_status: bool) -> None:
        """Apply the code-4 debounce at the end of a poll cycle.

        - Fresh status this cycle → proof of reachability, reset everything.
        - Otherwise, if this cycle saw a code 4 with no fresh data, count it;
          flag 'unreachable' only once enough consecutive polls have failed, so
          a transient mid-drive hiccup doesn't flip the sensor (#238).
        """
        if fresh_status:
            self._mark_reachable()
        elif self._code4_this_cycle:
            self._consecutive_unreachable_polls += 1
            if (
                self._consecutive_unreachable_polls
                >= UNREACHABLE_CONSECUTIVE_POLL_THRESHOLD
                and not self._last_command_unreachable
            ):
                self._last_command_unreachable = True
                self._last_command_unreachable_time = datetime.now(timezone.utc)
                LOGGER.debug(
                    "Vehicle flagged unreachable after %s consecutive failed "
                    "polls for VIN %s",
                    self._consecutive_unreachable_polls,
                    getattr(self, "vin", "?"),
                )

    def note_command_unreachable(self) -> None:
        """Record that a live command/poll failed with the 'can't reach car'
        code (4) this cycle.

        This no longer flips the Vehicle Reachability sensor on its own. A single
        code 4 ("remote control instruction failed, please try again later") is
        often just a transient backend hiccup — flagging on the first one made
        the sensor flip-flop to 'unreachable' and back throughout a drive
        (reported by @SteveMSJ on #238). Instead we mark that this cycle saw a
        code 4; only after several consecutive polls fail to bring fresh data
        (see the debounce in _run_update_cycle) is the car flagged unreachable.
        This also fixes the related edge case Steve raised — a car that stops in
        a no-signal spot right after a drive: is_powered_on is still 'on' from
        the last good poll, but because fresh data stops arriving the consecutive
        failures still cross the threshold and it correctly reads 'unreachable'.
        """
        self._code4_this_cycle = True
        LOGGER.debug(
            "Return code %s seen this cycle for VIN %s (consecutive so far: %s)",
            SAIC_RETURN_CODE_UNREACHABLE,
            getattr(self, "vin", "?"),
            self._consecutive_unreachable_polls,
        )

    @property
    def current_remote_climate_status(self):
        """The car's raw remoteClimateStatus value, or None if unavailable."""
        status = self.data.get("status") if self.data else None
        basic = getattr(status, "basicVehicleStatus", None) if status else None
        return getattr(basic, "remoteClimateStatus", None) if basic else None

    def climate_mode_from_status(self):
        """Decode remoteClimateStatus into a mode string.

        Returns one of "off", "cool", "fan_only", "heat", "defrost",
        "on_local", "unknown", or None when no status is available. Uses the
        same per-model reverse maps the climate entity uses, so the A/C switch
        and the Climate Mode sensor agree with the climate entity's hvac_mode.
        """
        s = self.current_remote_climate_status
        if s is None:
            return None
        if s in self.climate_status_heat:
            return "heat"
        if s in self.climate_status_defrost:
            return "defrost"
        if s in self.climate_status_cool:
            return "cool"
        if s in self.climate_status_fan_only:
            return "fan_only"
        if s == 0:
            return "off"
        if s == 6:
            # 6 = the climate is running under LOCAL control — i.e. the driver
            # is operating it from the dashboard (typically while driving), not
            # a remote command. Confirmed SAIC-wide, not tied to a profile.
            return "on_local"
        return "unknown"

    @property
    def vehicle_reachability(self) -> str:
        """Inferred reachability state for the Vehicle Reachability sensor.

        - unreachable:   a live command recently failed with code 4
        - awake:         car powered on, or the car reported recently
        - likely_asleep: the car has not reported for longer than the
                         stale-data threshold

        The staleness basis is the vehicle's OWN status timestamp
        (`statusTime` — when the car last reported to SAIC), NOT our polling
        cadence, so slowing polling (holiday mode) cannot falsely flip it to
        asleep. Using statusTime rather than our activity detection also means
        a car that is awake and returning fresh data reads "awake" even if
        none of the monitored fields happened to change between polls — the
        previous behaviour left such a car stuck on "likely_asleep" (reported
        by @SteveMSJ on #238).
        """
        if self._last_command_unreachable:
            return VEHICLE_REACHABILITY_UNREACHABLE
        if self.is_powered_on:
            return VEHICLE_REACHABILITY_AWAKE

        # Prefer the car's own reported timestamp; fall back to detected
        # activity if the response didn't carry one.
        reference = None
        status = self.data.get("status") if self.data else None
        status_time = getattr(status, "statusTime", None)
        if status_time is not None:
            try:
                reference = datetime.fromtimestamp(status_time, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                reference = None
        if reference is None:
            reference = self.last_vehicle_activity

        if reference is not None:
            stale_for = datetime.now(timezone.utc) - reference
            if stale_for >= self.stale_data_threshold:
                return VEHICLE_REACHABILITY_LIKELY_ASLEEP
        return VEHICLE_REACHABILITY_AWAKE

    @property
    def data_freshness(self) -> str | None:
        """How current the data from the most recent poll was (#238).

        A separate axis from vehicle_reachability:
        - live:   the poll returned a status whose timestamp advanced (proof of
                  live contact with the car).
        - cached: the poll succeeded but SAIC served the same, unchanged status
                  (the car is asleep / not reporting fresh data).
        - failed: the poll errored (e.g. a transient "return code 4").

        None until the first poll cycle completes.
        """
        return self._last_poll_result

    def record_command_error(self, source: str, error: Exception | str) -> None:
        """Record a generic command failure via the command-error Event entity.

        This is a lightweight, fire-and-forget complement to the existing
        LOGGER.error calls already present in every entity's except block —
        it does not replace logging, it adds a queryable Logbook entry so
        users without debug logging enabled can see command failures too.

        Safe to call even if the event entity hasn't loaded yet (no-op in
        that case) — never raises, so it can't break the calling command's
        own error handling.

        Args:
            source: short identifier of what failed, e.g.
                "climate.set_hvac_mode" or "switch.sunroof.turn_on".
            error: the exception or error message that occurred.
        """
        # Detect the "can't reach the car" return code (4) from any command
        # failure and flag reachability. All command errors flow through here,
        # so this single hook covers every entity without per-handler changes.
        if f"return code: {SAIC_RETURN_CODE_UNREACHABLE}" in str(error):
            self.note_command_unreachable()

        if self._command_error_event_entity is None:
            return
        try:
            self._command_error_event_entity.record_command_error(
                source, str(error)
            )
        except Exception as e:
            # The event entity is supplementary — never let a failure here
            # mask the original error or break the calling command.
            LOGGER.debug("Could not record command error event: %s", e)

    async def async_shutdown(self):
        """Release coordinator resources when the entry is unloaded.

        Note: the SAICMGAccountPoller is NOT stopped here.  It is managed by
        __init__.async_unload_entry, which stops the poller only when the last
        coordinator for that account is unloaded.
        """
        if self._shutdown_refresh_task and not self._shutdown_refresh_task.done():
            self._shutdown_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._shutdown_refresh_task

        self._action_refresh_generation += 1

        if self._action_refresh_task and not self._action_refresh_task.done():
            self._action_refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._action_refresh_task
        self._action_refresh_task = None

        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

    def _schedule_refresh(self):
        """Schedule the next refresh and update listeners."""
        if self._unsub_refresh:
            self._unsub_refresh()
            self._unsub_refresh = None

        if self.update_interval and self.update_interval > timedelta(0):
            self.next_update_time = utcnow() + self.update_interval
            self._unsub_refresh = async_track_point_in_utc_time(
                self.hass, self._handle_refresh_interval, self.next_update_time
            )
            LOGGER.debug(
                "Next update scheduled in %s.",
                self.update_interval,
            )
        else:
            self.next_update_time = None
            LOGGER.debug("Update interval is None or zero; no refresh scheduled.")

    async def _handle_refresh_interval(self, now):
        """Handle a scheduled refresh."""
        self._unsub_refresh = None
        await self.async_refresh()

    async def _fetch_with_retries(self, fetch_func, is_generic_func, data_name):
        """Fetch data with retries and handle generic responses.

        On a 401 (token expired/invalidated), re-login immediately and retry
        without waiting for the full RETRY_BACKOFF_FACTOR delay.  This handles
        the race where the message poller re-auths and invalidates the
        coordinator's token a fraction of a second before an event-driven
        refresh fires — previously this caused a 15-second delay and noisy
        ERROR log entries on every engine-start event for two-car accounts.
        """
        retries = 0
        while retries < RETRY_LIMIT:
            try:
                data = await fetch_func()
                if data is None:
                    LOGGER.warning("%s returned None.", data_name.capitalize())
                    raise UpdateFailed(f"{data_name.capitalize()} is None.")
                if is_generic_func(data):
                    LOGGER.warning("Generic %s response received.", data_name)
                    raise GenericResponseException(f"Generic {data_name} response.")
                return data
            except (UpdateFailed, GenericResponseException, Exception) as e:
                retries += 1
                exc_str = str(e)

                # Return code 4 = "can't reach the car". Previously only failed
                # *commands* set the Reachability sensor to 'unreachable'; a
                # failed *status* poll (manual or scheduled refresh) left it
                # showing 'awake' for the whole retry window. Flag it as soon as
                # the car rejects the fetch so the sensor reflects reality
                # immediately (reported by @SteveMSJ, #238). Cleared again by a
                # fresh status response or a successful command.
                if f"return code: {SAIC_RETURN_CODE_UNREACHABLE}" in exc_str:
                    self.note_command_unreachable()

                # 401 means our token was invalidated — re-login immediately
                # rather than waiting RETRY_BACKOFF_FACTOR seconds.  This is
                # the common case when the poller re-auths concurrently.
                if "401" in exc_str:
                    LOGGER.debug(
                        "401 on %s fetch for VIN %s — re-logging in before retry "
                        "(attempt %d/%d)",
                        data_name,
                        self.vin,
                        retries,
                        RETRY_LIMIT,
                    )
                    try:
                        await self.client.login()
                    except Exception as login_exc:
                        LOGGER.warning(
                            "Re-login failed for VIN %s: %s", self.vin, login_exc
                        )
                    # No sleep — retry immediately after re-auth
                    continue

                delay = RETRY_BACKOFF_FACTOR
                LOGGER.warning(
                    "Error fetching %s: %s. Retrying in %s seconds... (Attempt %d/%d)",
                    data_name,
                    e,
                    delay,
                    retries,
                    RETRY_LIMIT,
                )
                await asyncio.sleep(delay)
        LOGGER.error("Failed to fetch %s after %d retries.", data_name, RETRY_LIMIT)
        return None

    def _is_generic_response_vehicle_info(self, vehicle_info):
        """Check if the vehicle info response is generic (placeholder if needed)."""
        return False  # Vehicle info doesn't have known generic responses

    def _is_generic_response_vehicle_status(self, status):
        """Check if the vehicle status response is generic.

        A response is considered generic (placeholder/incomplete) when:
        - All three of fuelRange, fuelRangeElec, and mileage are zero, OR
        - Temperature fields return the sentinel value (-40)

        Previously this method had an operator precedence bug where
        `or mileage <= 0` was evaluated independently of the `and` chain,
        causing any response with mileage=0 (legitimate during charging) to
        be incorrectly flagged as generic. Fixed by wrapping the all-zero
        condition in explicit parentheses.
        """
        try:
            if not hasattr(status, "basicVehicleStatus"):
                return False
            basic = status.basicVehicleStatus
            # All three placeholder fields are zero — classic deep-sleep response
            all_zero = (
                basic.fuelRange == GENERIC_RESPONSE_STATUS_THRESHOLD
                and basic.fuelRangeElec == GENERIC_RESPONSE_STATUS_THRESHOLD
                and basic.mileage == GENERIC_RESPONSE_STATUS_THRESHOLD
            )
            # Temperature sentinel values
            bad_temp = (
                basic.interiorTemperature == GENERIC_RESPONSE_TEMPERATURE
                or basic.exteriorTemperature == GENERIC_RESPONSE_TEMPERATURE
            )
            if all_zero or bad_temp:
                LOGGER.debug(
                    "Generic Vehicle Status Data: %s", basic
                )
                return True
            return False
        except Exception as e:
            LOGGER.error("Error. Generic Vehicle Status Data: %s", e)
            raise

    def _is_status_timestamp_valid(self, status) -> bool:
        """Sanity-check the statusTime field on a vehicle status response.

        The SAIC API occasionally returns a response with a bogus or stale
        statusTime — for example far in the past (a cached/stuck response)
        or in the future (clock skew on the backend). Trusting such a
        response could confuse the activity-detection and interval-adjustment
        logic, which relies on knowing how fresh the data actually is.

        Returns True if the timestamp looks sane (or is absent, since not
        all responses are guaranteed to include it), False if it should be
        treated as untrustworthy.
        """
        status_time = getattr(status, "statusTime", None)
        if status_time is None:
            # Field not present on this response — nothing to validate against,
            # don't reject the response just because it's missing.
            return True

        try:
            status_dt = datetime.fromtimestamp(status_time, tz=timezone.utc)
        except (ValueError, OSError, OverflowError) as e:
            LOGGER.warning(
                "Vehicle status statusTime %s could not be parsed: %s — "
                "treating response as untrustworthy.",
                status_time,
                e,
            )
            return False

        now = datetime.now(timezone.utc)

        if status_dt > now + STATUS_TIMESTAMP_FUTURE_TOLERANCE:
            LOGGER.warning(
                "Vehicle status statusTime %s is %s in the future — "
                "treating response as untrustworthy.",
                status_dt,
                status_dt - now,
            )
            return False

        if status_dt < now - STATUS_TIMESTAMP_MAX_AGE:
            LOGGER.warning(
                "Vehicle status statusTime %s is %s old (older than the "
                "%s sanity limit) — treating response as untrustworthy.",
                status_dt,
                now - status_dt,
                STATUS_TIMESTAMP_MAX_AGE,
            )
            return False

        return True

    def _is_generic_response_charging(self, charging_info):
        """Check if the charging response is generic."""
        try:
            chrgMgmtData = getattr(charging_info, "chrgMgmtData", None)
            if chrgMgmtData:
                if (
                    chrgMgmtData.bmsPackSOCDsp is not None
                    and chrgMgmtData.bmsPackSOCDsp > GENERIC_RESPONSE_SOC_THRESHOLD
                ):
                    LOGGER.debug("Generic Charging Data: %s", chrgMgmtData)
                    return True
            return False
        except Exception as e:
            LOGGER.error("Error. Generic Charging Data: %s", e)
            raise

    def _determine_vehicle_type(self, vehicle_info):
        """Determine the type of vehicle based on its information."""
        vin_info = next((v for v in vehicle_info if v.vin == self.vin), None)
        is_electric = False
        is_combustion = False
        is_hybrid = False

        if not vin_info:
            LOGGER.error(f"No vehicle info found for VIN: {self.vin}")
            return "ICE"  # Default to ICE if unknown

        try:
            for config in vin_info.vehicleModelConfiguration:
                if config.itemCode == "EV":
                    if config.itemValue == "1":
                        is_electric = True
                    elif config.itemValue == "0":
                        is_combustion = True
                if config.itemCode == "BType":
                    if config.itemValue == "1":
                        is_electric = True
                    elif config.itemValue == "0":
                        is_combustion = True
                if config.itemCode == "ENERGY":
                    if config.itemValue == "1":
                        is_hybrid = True
        except Exception as e:
            LOGGER.error("Error determining vehicle type: %s", e)

        # Additional checks
        if (
            "electric" in vin_info.modelName.lower()
            or "ev" in vin_info.modelName.lower()
        ):
            is_electric = True
            is_combustion = False

        if "electric" in vin_info.series.lower() or "ev" in vin_info.series.lower():
            is_electric = True
            is_combustion = False

        if is_electric and not is_combustion:
            return "BEV"
        if is_electric and is_combustion and is_hybrid:
            return "PHEV"
        if is_hybrid and not is_electric:
            return "HEV"
        if not is_electric and is_combustion:
            return "ICE"

        return "ICE"

    def get_sensor_value(self, sensor_name):
        """Retrieve the value for specific sensors."""
        if sensor_name == "last_powered_on":
            return self.last_powered_on_time
        elif sensor_name == "last_powered_off":
            return self.last_powered_off_time
        elif sensor_name == "last_vehicle_activity":
            return self.last_vehicle_activity
        return None

    # ---- AC Temperature Handling ----
    def get_ac_temperature_idx(self, desired_temp: int) -> int:
        """Calculate the temperature index for the SAIC climate control API.

        If the vehicle profile provides a temp_index_map (an explicit
        {temperature: index} lookup), that takes priority — some models (e.g.
        the MGS6 EV / MIS3E) have a non-linear mapping with special values at
        the extremes that no simple formula reproduces. The map was built from
        decrypted iSmart app traffic.

        Otherwise the index is computed from a linear formula, with direction
        set by self.temp_idx_inverted (from VEHICLE_PROFILES):

        Standard (temp_idx_inverted=False) — e.g. MG4, default/unknown models:
            temperature_idx = temp_offset + (desired_temp - min_temp)
            Low temp -> low index, high temp -> high index.

        Inverted (temp_idx_inverted=True):
            temperature_idx = max_idx - (desired_temp - min_temp)
            Low temp -> high index.
        """
        desired_temp = int(max(self.min_temp, min(self.max_temp, desired_temp)))

        # Prefer an explicit lookup map if the profile defines one.
        if self.temp_index_map:
            if desired_temp in self.temp_index_map:
                idx = self.temp_index_map[desired_temp]
            else:
                # Nearest available temperature in the map (defensive; the map
                # should cover the whole min..max range).
                nearest = min(
                    self.temp_index_map.keys(), key=lambda t: abs(t - desired_temp)
                )
                idx = self.temp_index_map[nearest]
            LOGGER.debug(
                "Temperature index (map): %s for desired_temp: %s°C", idx, desired_temp
            )
            return idx

        if self.temp_idx_inverted:
            max_idx = self.temp_offset + (self.max_temp - self.min_temp)
            temperature_idx = max_idx - (desired_temp - self.min_temp)
        else:
            temperature_idx = self.temp_offset + (desired_temp - self.min_temp)
        LOGGER.debug(
            f"Calculated temperature index: {temperature_idx} for desired_temp: {desired_temp}°C"
        )
        return temperature_idx


class GenericResponseException(Exception):
    """Exception raised when a generic response is received."""
