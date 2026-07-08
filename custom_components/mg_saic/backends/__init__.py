# File: backends/__init__.py
#
# Backend abstraction layer for the MG/SAIC integration.
#
# The integration supports vehicles served by different SAIC back-end
# infrastructures:
#
#   * Global (EU, Australia, Brazil, Israel, Turkey, Thailand, ...):
#     REST/JSON API, accessed via the saic-ismart-client-ng library and
#     wrapped by SAICMGAPIClient in api.py.  This is the original code path
#     and remains completely unchanged.
#
#   * India: a completely different TAP binary protocol, reverse-engineered
#     by John Lazarus (github.com/john-lazarus/mg-ismart-india-ha).
#     Implemented by IndiaBackend in backends/india.py.
#
# Each config entry maps to exactly ONE backend, selected by the entry's
# region at setup time via create_backend().  The shared Home Assistant
# layer (coordinator, entities, config flow, services) talks only to the
# backend's method surface — the de-facto contract defined by
# SAICMGAPIClient's public methods — and never to a protocol client
# directly.
#
# Capability gating
# -----------------
# Backends do not implement identical feature sets.  Each backend instance
# carries a `supported_features` frozenset of Feature members declaring what
# it implements AND has confirmed against a real vehicle.  Entity platforms
# and services consult backend_supports() before creating entities or
# executing commands.  This sits ABOVE the existing per-vehicle
# "if equipped" checks (has_sunroof, has_heated_seats, ...):
#
#     entity exists  <=>  backend supports feature  AND  vehicle has feature
#
# Anything a backend has not confirmed on a real car defaults to
# unsupported.  Never ship an unconfirmed command to a real vehicle.

from enum import Enum

from ..api import SAICMGAPIClient
from ..const import LOGGER

# Region name (as stored in the config entry's "region" field and defined in
# const.REGION_BASE_URIS) that routes to the India TAP backend.
REGION_INDIA = "India"


class Feature(str, Enum):
    """Features a backend can declare support for.

    Members mirror the command/data families exposed by the backend method
    surface.  Used for capability gating of entities and services.
    """

    # Data retrieval
    STATUS = "status"                            # get_vehicle_status
    CHARGING_DATA = "charging_data"              # get_charging_info (incl. SOC)
    ALARM_MESSAGES = "alarm_messages"            # get_alarm_messages / set_alarm_switches / message poller

    # Vehicle controls
    LOCK = "lock"                                # lock_vehicle / unlock_vehicle
    TAILGATE = "tailgate"                        # open_tailgate
    WINDOWS = "windows"                          # control_windows
    SUNROOF = "sunroof"                          # control_sunroof
    CLIMATE = "climate"                          # start_ac / start_climate / stop_ac
    FRONT_DEFROST = "front_defrost"              # start_front_defrost
    REAR_WINDOW_HEAT = "rear_window_heat"        # control_rear_window_heat
    HEATED_SEATS = "heated_seats"                # control_heated_seat(s)
    HEATED_SEATS_REAR = "heated_seats_rear"      # rear heated seats; front-only backends (India) omit this
    STEERING_WHEEL_HEAT = "steering_wheel_heat"  # control_steering_wheel_heat
    FIND_MY_CAR = "find_my_car"                  # trigger_alarm

    # Charging controls
    CHARGING_CONTROL = "charging_control"        # send_vehicle_charging_control
    CHARGING_PORT_LOCK = "charging_port_lock"    # control_charging_port_lock
    SCHEDULED_CHARGING = "scheduled_charging"    # set_scheduled_charging
    BATTERY_HEATING = "battery_heating"          # send_vehicle_charging_ptc_heat + heating schedule
    TARGET_SOC = "target_soc"                    # set_target_soc
    CURRENT_LIMIT = "current_limit"              # set_current_limit


# The global REST backend implements the full feature surface.  This is the
# original integration behaviour — every feature listed here predates the
# backend split.
GLOBAL_FEATURES: frozenset[Feature] = frozenset(Feature)

# Features implemented AND confirmed on a real vehicle by the India TAP
# client (John Lazarus, mg-ismart-india-ha).  Charging is deliberately
# absent: MG India's platform does not offer charging data or control (it is
# not present in the Comet EV's app), so every charging/SOC/scheduled-
# charging/battery-heating entity is hidden for India accounts.
# ALARM_MESSAGES is absent because the TAP protocol has no message-list
# endpoint — the account message poller must not run for India accounts.
INDIA_FEATURES: frozenset[Feature] = frozenset(
    {
        Feature.STATUS,
        Feature.LOCK,
        Feature.TAILGATE,
        Feature.WINDOWS,
        Feature.SUNROOF,
        Feature.CLIMATE,
        Feature.HEATED_SEATS,
        Feature.FIND_MY_CAR,
    }
)


def backend_supports(client, feature: Feature) -> bool:
    """Return True if *client* (a backend instance) supports *feature*.

    Backwards compatible by design: a client that does not declare
    `supported_features` (e.g. a SAICMGAPIClient constructed directly by
    older code paths or tests) is treated as fully featured, preserving the
    original global behaviour exactly.
    """
    features = getattr(client, "supported_features", None)
    if features is None:
        return True
    return feature in features


def create_backend(entry_data: dict):
    """Create the correct backend for a config entry's data dict.

    Routing rule: region == "India" gets the India TAP backend; every other
    region (including Custom endpoints) gets the existing global REST client,
    constructed exactly as before.

    Returns a backend instance conforming to the SAICMGAPIClient method
    surface, with `supported_features` set.  Does NOT log in — callers
    control login timing and locking, unchanged from previous behaviour.
    """
    region = entry_data.get("region")

    if region == REGION_INDIA:
        # Imported lazily so a problem in the (young) India backend module
        # can never break setup for global users.
        from .india import IndiaBackend

        LOGGER.debug(
            "Creating India TAP backend for username %s",
            entry_data.get("username"),
        )
        return IndiaBackend(
            username=entry_data.get("username"),
            password=entry_data.get("password"),
            vin=entry_data.get("vin"),
            pin_hash=entry_data.get("india_pin_hash"),
            country_code=entry_data.get("country_code"),
        )

    client = SAICMGAPIClient(
        entry_data.get("username"),
        entry_data.get("password"),
        entry_data.get("vin"),
        entry_data.get("country_code") is None,  # username_is_email
        region,
        entry_data.get("country_code"),
        custom_base_uri=entry_data.get("custom_base_uri"),
        region_code=entry_data.get("region_code"),
        tenant_id=entry_data.get("tenant_id"),
    )
    # Declared here (not in api.py) so api.py stays byte-for-byte unchanged.
    client.supported_features = GLOBAL_FEATURES
    return client
