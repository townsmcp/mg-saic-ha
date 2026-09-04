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

    # Last resort: the IMCU's own vehicle range. Some models never populate a
    # usable fuelRangeElec — the profiles flag them reliable_fuel_range_elec:
    # False — and the Electric Range sensor already reads this field for them.
    # Anything derived from range (the charge-session range delta, the
    # range-after-charging projection) needs the same fallback or it silently
    # produces nothing on exactly those cars (#262).
    #
    # NB no decimal correction: imcu fields are whole km, unlike
    # fuelRangeElec. Confirmed on a car reporting both — imcuVehElecRng 257
    # against fuelRangeElec 2570.
    chrg = getattr(charging_data, "chrgMgmtData", None) if charging_data else None
    raw = getattr(chrg, "imcuVehElecRng", None) if chrg is not None else None
    if raw is not None and raw > 0 and raw != ELECTRIC_RANGE_SENTINEL:
        return float(raw)
    return None
# The API's totalBatteryCapacity is unreliable on several MG series, which is
# why VEHICLE_PROFILES carries known-good figures. 725 (-> 72.5 kWh with the
# x0.1 decimal correction) is a documented placeholder rather than a real pack
# size, seen identically on EC32/AS33P/S12L and others. A car that reports it
# is far more likely to be emitting the placeholder than to genuinely hold
# 72.5 kWh — and a car that genuinely does gets its figure from its profile.
BATTERY_CAPACITY_PLACEHOLDER_RAW = 725

# Sanity bounds for an API-reported capacity, in kWh. Wide on purpose: this
# only has to reject nonsense (0, negatives, absurd magnitudes), not second
# guess a plausible pack.
MIN_PLAUSIBLE_BATTERY_KWH = 5.0
MAX_PLAUSIBLE_BATTERY_KWH = 200.0


def resolve_battery_capacity(
    override_kwh,
    profile_kwh,
    api_raw,
    *,
    factor,
):
    """Resolve the usable battery capacity and say where it came from.

    Precedence is the one the integration has always documented:
    user override > our per-model profile > the API's own figure. Returns
    ``(capacity_kwh, source)`` where source is ``"user_override"``,
    ``"profile"``, ``"api"``, or ``None`` when nothing usable is available.

    Resolving this in one place matters: the Total Battery Capacity sensor
    honoured all three tiers, but ``known_battery_capacity_kwh`` — which the
    charge-session and SOC-efficiency maths read — only ever saw the first
    two. So an unprofiled car showed a populated capacity sensor next to three
    blank sensors derived from it (#262, #302).

    The API tier is guarded: the placeholder is rejected, as are values
    outside a wide plausibility band. A rejected API value yields ``None``,
    which is honest — better a blank capacity than energy figures confidently
    derived from a number the car made up.
    """
    if override_kwh is not None:
        return override_kwh, "user_override"
    if profile_kwh is not None:
        return profile_kwh, "profile"
    if api_raw is None or api_raw == BATTERY_CAPACITY_PLACEHOLDER_RAW:
        return None, None
    capacity = round(api_raw * factor, 2)
    if not MIN_PLAUSIBLE_BATTERY_KWH <= capacity <= MAX_PLAUSIBLE_BATTERY_KWH:
        return None, None
    return capacity, "api"


# Target SOC is reported as an enum, not a percentage.
TARGET_SOC_PERCENT_BY_CODE = {1: 40, 2: 50, 3: 60, 4: 70, 5: 80, 6: 90, 7: 100}

# Below this SOC a range projection amplifies noise too much to be useful: at
# 5% SOC a single percentage point of error swings the result by 20%.
MIN_SOC_PCT_FOR_RANGE_PROJECTION = 12.0


def project_range_at_target(current_range, soc_pct, target_soc_pct, *, min_soc_pct=MIN_SOC_PCT_FOR_RANGE_PROJECTION):
    """Project the range the car will have at ``target_soc_pct``, or None.

    Used when the car won't tell us itself (#262). A PHEV has no target SOC
    concept at all, so its IMCU appears to have nothing to project to and
    returns 0 — but the projection is pure ratio work on the range figure the
    car does report, so it needs no battery capacity and is unaffected by any
    capacity override the user has set. Verified against a BEV that reported
    both: 257 km at 51.7% SOC projected to an 80% target gives 398 km, where
    the car itself said 410.

    Whatever unit ``current_range`` is in comes back out; callers pass km.

    Returns None rather than a poor guess when the inputs can't support one:
    below ``min_soc_pct`` the projection amplifies noise too much, and a
    result below the current range means something is stale, since charging
    to a higher SOC cannot reduce your range.
    """
    if current_range is None or soc_pct is None or target_soc_pct is None:
        return None
    if soc_pct < min_soc_pct or current_range <= 0:
        return None
    if not 0 < target_soc_pct <= 100 or target_soc_pct < soc_pct:
        return None
    projected = round(current_range / soc_pct * target_soc_pct, 1)
    if projected < current_range:
        return None
    return projected


# Fields where a zero means "the car isn't reporting this", not a real value.
# A charge that added no range, or a range-after-charging of zero, is not a
# measurement — it's an absence. Cars differ: an MG IM5 reports a retained
# chrgngAddedElecRng between charges, while an MGS6 and an HS PHEV report 0
# throughout (#262, #326). Publishing that 0 makes an absent field look like a
# working sensor, which is how it went unnoticed for years.
ZERO_MEANS_UNREPORTED_FIELDS = frozenset(
    {"chrgngAddedElecRng", "imcuChrgngEstdElecRng"}
)


def is_unreported_zero(field, raw):
    """True when a falsy reading for this field means 'no data', not zero."""
    return field in ZERO_MEANS_UNREPORTED_FIELDS and not raw
