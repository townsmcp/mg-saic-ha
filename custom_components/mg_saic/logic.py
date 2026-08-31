"""Pure logic helpers used by the integration.

These helpers deliberately avoid Home Assistant imports so they can be tested
with the standard library only.
"""

from datetime import timedelta


def normalize_sunroof_action(action):
    """Normalize a sunroof action to `(should_open, action_name)`."""
    if isinstance(action, bool):
        return action, "open" if action else "close"

    action_name = str(action).lower()
    if action_name not in {"open", "close"}:
        raise ValueError(
            f"Invalid sunroof action '{action}'. Expected 'open' or 'close'."
        )

    return action_name == "open", action_name


def build_vehicle_options(vehicles):
    """Return VIN option values mapped to privacy-safe display labels."""
    options = {}
    for vehicle in vehicles:
        vin = str(getattr(vehicle, "vin", vehicle))
        model_name = getattr(vehicle, "modelName", None) or getattr(
            vehicle, "series", None
        )
        label = f"{model_name} (…{vin[-5:]})" if model_name else vin
        options[vin] = label
    return options


def select_update_interval(
    *,
    is_powered_on,
    is_charging,
    is_dc_charging=False,
    idle_duration,
    activity_duration,
    default_update_interval,
    powered_update_interval,
    charging_update_interval,
    dc_charging_update_interval=None,
    grace_period_update_interval,
    after_shutdown_update_interval,
    holiday_mode=False,
    holiday_update_interval=None,
):
    """Return the interval that should be used for the current state.

    Priority order (highest to lowest):
    1. Powered on — always use powered interval
    2. DC charging — use dc_charging_update_interval (typically shorter than AC)
    3. AC charging — use charging_update_interval
    4. Grace period — recent activity but not powered/charging
    5. After shutdown window
    6. Default idle interval
    """
    # Holiday mode: a runtime override to minimise wake-ups while the car is
    # left for long periods. It overrides the idle/grace/after-shutdown cadence,
    # but NOT active charging or a powered-on car — if someone has plugged the
    # car in or is driving it, that was deliberate and they still want updates.
    holiday_active = holiday_mode and holiday_update_interval is not None

    if is_powered_on:
        return powered_update_interval

    if is_dc_charging and dc_charging_update_interval is not None:
        return dc_charging_update_interval

    if is_charging:
        return charging_update_interval

    # Car is idle (not powered, not charging) — holiday mode takes over here.
    if holiday_active:
        return holiday_update_interval

    if (
        activity_duration <= grace_period_update_interval
        or idle_duration <= grace_period_update_interval
    ):
        return grace_period_update_interval

    if idle_duration <= after_shutdown_update_interval:
        return after_shutdown_update_interval

    if not isinstance(default_update_interval, timedelta):
        raise TypeError("default_update_interval must be a timedelta")

    return default_update_interval


# Energy fields that some models (e.g. MG HS PHEV / AS33P) report inflated by
# ~3× — the same quirk that makes totalBatteryCapacity read 72.5 kWh on a
# 24.7 kWh pack. The profile's charging_capacity_correction is applied to each
# of these wherever they are read (#262, #310).
ENERGY_CORRECTION_FIELDS = frozenset(
    {"lastChargeEndingPower", "powerUsageSinceLastCharge"}
)


def apply_energy_correction(field, value, correction):
    """Scale an inflated energy field by the per-model correction factor.

    Returns ``value`` unchanged for fields that aren't inflated, for models
    with no correction configured, or for a missing value. Distance fields are
    never corrected — only the energy fields above.

    Lives here rather than on the sensor because it has to be applied from
    several call sites (both numeric branches of the charging sensor, and the
    coordinator's charge-session maths). Keeping one implementation is what
    stops a repeat of #310, where the correction was added in a branch the
    field never reached and so silently did nothing.
    """
    if value is None or correction is None:
        return value
    if field not in ENERGY_CORRECTION_FIELDS:
        return value
    return value * correction


def odometer_km(basic_status, charging_data, *, factor, saturation):
    """Odometer in km from a poll's data, or None.

    Prefers ``basicVehicleStatus.mileage``, then falls back to the odometer
    carried in the charging data. The fallback reads ``rvsChargeStatus``,
    which is where ``mileage`` actually lives — ``chrgMgmtData`` has no such
    field, so looking there (as this once did) meant the fallback could never
    fire, and any caller relying on it got None (#262).

    Rejects 0, negatives and the uint16 saturation sentinel.
    """
    raw = getattr(basic_status, "mileage", None) if basic_status is not None else None
    if raw is not None and raw > 0 and raw != saturation:
        return raw * factor
    if charging_data is not None:
        source = getattr(charging_data, "rvsChargeStatus", None)
        raw = getattr(source, "mileage", None) if source is not None else None
        if raw is not None and raw > 0 and raw != saturation:
            return raw * factor
    return None


# The API reports -128 for fuelRangeElec on several models when the value
# isn't live (typically while parked) rather than omitting the field.
ELECTRIC_RANGE_SENTINEL = -128


def electric_range_km(basic_status, charging_data, *, factor):
    """Remaining electric range in km, or None.

    Prefers the charging block's figure and falls back to basicVehicleStatus,
    matching the Electric Range sensor. Rejects negatives and the -128
    sentinel; 0 is allowed through, since a flat pack really does have no
    range left.
    """
    rcs = getattr(charging_data, "rvsChargeStatus", None) if charging_data else None
    for source in (rcs, basic_status):
        if source is None:
            continue
        raw = getattr(source, "fuelRangeElec", None)
        if raw is not None and raw >= 0 and raw != ELECTRIC_RANGE_SENTINEL:
            return round(raw * factor, 1)
    return None
