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
        # 0 means "not reported", not "no range left". On an MG HS PHEV
        # mid-charge, rvsChargeStatus.fuelRangeElec sits at 0 for the whole
        # session while imcuVehElecRng climbs 75 -> 120 km, so accepting the 0
        # short-circuits the imcu fallback below and hands every caller a
        # range of zero (#354). That silently produced BOTH of that car's
        # symptoms at once: the range-after-charging projection divides by a
        # zero range and gives up, and the charge-session range delta comes
        # out 0 - 0 = 0, hence "Last Charge Range Added: 0.0 mi" against a
        # charge that really added ~28 miles. A car genuinely at zero range
        # loses nothing here: the imcu fallback answers instead, and if that
        # is also absent, None is more honest than a zero that breaks
        # everything downstream.
        if raw is not None and raw > 0 and raw != ELECTRIC_RANGE_SENTINEL:
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


def resolve_fuel_tank_litres(override_litres, profile_litres):
    """Resolve the petrol tank size and say where it came from.

    Mirrors resolve_battery_capacity, with one real difference: the SAIC API
    reports no tank size at all, so there is no third "api" tier to fall back
    to — only a user override and our per-model figure.

    Returns ``(litres, source)`` where source is ``"user_override"``,
    ``"profile"``, or ``None`` when neither is available (in which case the
    fuel sensors report % used but not litres, L/100km or mpg, as before).

    The override exists because tank sizes are market-split in ways the
    series code can't distinguish — the MG HS PHEV is documented at 37 L for
    some markets, and owners of 2025/26 UK cars report filling far more than
    that (#354). A per-owner override settles it without us having to pick a
    single number for a model that genuinely ships with more than one.
    """
    if override_litres is not None:
        return override_litres, "user_override"
    if profile_litres is not None:
        return profile_litres, "profile"
    return None, None


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


# ── Charging-data freshness (#262) ──────────────────────────────────────────
#
# The charging endpoint fails independently of vehicle status (SAIC-side
# timeouts / return code 4 that can last hours), and when it does every
# charging sensor quietly holds its last value. Nothing previously said so:
# the Data Freshness sensor is driven by vehicle status alone, so it could
# read "live" while the charging figures were hours old. This tracks the
# charging endpoint on its own axis.
#
# States stay lowercase snake_case so automations/templates can match on them;
# translations/<lang>.json -> entity.sensor.charging_data_freshness provides
# the display labels.
CHARGING_DATA_FRESHNESS_LIVE = "live"
CHARGING_DATA_FRESHNESS_STALE = "stale"
CHARGING_DATA_FRESHNESS_NO_DATA = "no_data"
CHARGING_DATA_FRESHNESS_STATES = (
    CHARGING_DATA_FRESHNESS_LIVE,
    CHARGING_DATA_FRESHNESS_STALE,
    CHARGING_DATA_FRESHNESS_NO_DATA,
)
LAST_ERROR_MAX_CHARS = 200


class ChargingFreshnessTracker:
    """Record charging-endpoint outcomes and derive a freshness state.

    - live:    the most recent charging fetch succeeded.
    - stale:   the most recent fetch failed, but an earlier one succeeded, so
               the charging sensors are holding values from ``last_success``.
    - no_data: fetches have been attempted but none has succeeded since Home
               Assistant started, so there is nothing to hold (the charging
               sensors show unknown).
    - None:    no fetch attempted yet.

    Timestamps are supplied by the caller (timezone-aware UTC in the
    integration), which keeps this deterministic and testable.
    """

    def __init__(self):
        self.last_success = None
        self.last_attempt = None
        self.stale_since = None
        self.consecutive_failures = 0
        self.last_error = None
        self._last_attempt_ok = None

    def record_success(self, now):
        """The charging endpoint returned usable data at ``now``."""
        self.last_success = now
        self.last_attempt = now
        self.stale_since = None
        self.consecutive_failures = 0
        self.last_error = None
        self._last_attempt_ok = True

    def record_failure(self, now, reason=None):
        """A charging fetch (or the whole update cycle) failed at ``now``.

        ``stale_since`` marks the first failure of the current run, so the
        outage length is visible without scanning history.
        """
        if self._last_attempt_ok is not False:
            self.stale_since = now
        self.last_attempt = now
        self.consecutive_failures += 1
        # Capped: this lands in a state attribute, and SAIC error text can be
        # long.
        self.last_error = str(reason)[:LAST_ERROR_MAX_CHARS] if reason else None
        self._last_attempt_ok = False

    @property
    def state(self):
        """Current freshness state (see class docstring)."""
        if self._last_attempt_ok is None:
            return None
        if self._last_attempt_ok:
            return CHARGING_DATA_FRESHNESS_LIVE
        if self.last_success is None:
            return CHARGING_DATA_FRESHNESS_NO_DATA
        return CHARGING_DATA_FRESHNESS_STALE

    def attributes(self, now):
        """Supporting evidence for the freshness sensor.

        ``data_age_minutes`` is how old the values the charging sensors are
        currently showing are -- 0 when live, growing while stale.
        """
        attrs = {"consecutive_failures": self.consecutive_failures}
        if self.last_success is not None:
            attrs["last_success"] = self.last_success.isoformat()
            age = (now - self.last_success).total_seconds() / 60
            attrs["data_age_minutes"] = max(0, round(age))
        if self.stale_since is not None:
            attrs["stale_since"] = self.stale_since.isoformat()
        if self.last_error:
            attrs["last_error"] = self.last_error
        return attrs


# ── Phantom since-charge counter resets (#262) ──────────────────────────────
#
# Some cars reset their own since-charge counters without a charge. Captured in
# @HarryFlatter's log (MG HS PHEV): after a ~2 h SAIC outage the first good
# response had mileageSinceLastCharge 6100 -> 0, powerUsageSinceLastCharge
# 266 -> 0, lastChargeEndingPower reset to the pack's current energy, and a
# charge record stamped mid-outage with startTime 0 -- while SOC, plug state,
# charging status and odometer were all unchanged. A genuine charge record in
# the same log carries a real start and end time.
#
# SinceChargeCounterGuard accepts a reset only with positive evidence of a
# charge, and otherwise holds the previous figures and keeps counting on top
# of them. It fails safe: when evidence is ambiguous it ACCEPTS the reset,
# i.e. exactly the behaviour before this guard existed.

# SOC rise (percentage points) that proves a charge while the odometer hasn't
# moved -- parked SOC wobble is a few tenths.
COUNTER_RESET_SOC_RISE_PARKED_PCT = 1.0
# SOC rise that proves a charge even if the car was also driven in between --
# well beyond what regen can add between two polls.
COUNTER_RESET_SOC_RISE_ANY_PCT = 5.0


# Raw odometer units (0.1 km): the charge baseline is only re-saved when it
# moves by more than 1 km.
BASELINE_PERSIST_TOLERANCE = 10


class SinceChargeCounterGuard:
    """Hold the since-charge counters through a reset that wasn't a charge.

    Readings are raw API values: ``km`` (mileageSinceLastCharge), ``kwh``
    (powerUsageSinceLastCharge), ``ending`` (lastChargeEndingPower),
    ``start``/``end`` (the charge record's times), ``soc`` (percent),
    ``odo`` (odometer, raw) and ``plugged`` (any plug/charging indication).

    A reset is a since-charge counter going DOWN, or the charge record's end
    time changing. It is accepted when any of these was seen since the last
    reading (``charge_seen`` spans polls, and is spent once the car drives):

    - the car plugged in / charging (status, gun, or plug flags);
    - SOC up by COUNTER_RESET_SOC_RISE_PARKED_PCT with the odometer unmoved,
      or by COUNTER_RESET_SOC_RISE_ANY_PCT regardless;
    - a new charge record with a real start time before its end time.

    Otherwise it's ignored: each counter that dropped is folded into an
    offset (the counters reset to 0, so everything after it is new usage on
    top of what was shown), and lastChargeEndingPower is held. The next
    genuine charge clears all of it.

    Second SAIC fault, same field: mileageSinceLastCharge sometimes carries
    the ODOMETER instead (@HarryFlatter's HS PHEV: 61120 == odometer 61120
    after the 23 Sep charge, rising with it -- 61150/61150, 61160/61160 --
    until the next plug-in reset it to 0; the 25 Sep charge ended correctly
    at 0). A reading whose km equals the odometer is never trusted: it plays
    no part in reset detection, and the figure is worked out from
    ``baseline_odo`` -- the odometer at the last charge, i.e. odo - km from
    every sane reading (constant while driving, as both rise together). If
    the same reading accepts a genuine charge, the charge just happened, so
    the baseline becomes this odometer and the figure is 0. With no baseline
    it's reported as None (sensors hold their last value) rather than the
    odometer.
    """

    def __init__(self):
        self.last = None
        self.offset_km = 0
        self.offset_kwh = 0
        self.held_ending = None
        self.charge_seen = False
        self.ignored_at = None
        self.ignored_count = 0
        self.baseline_odo = None
        self.odometer_in_km = False
        self._accepted_reset = False

    # -- persistence (stored alongside trip stats) --

    def to_dict(self):
        return {
            "last": self.last,
            "offset_km": self.offset_km,
            "offset_kwh": self.offset_kwh,
            "held_ending": self.held_ending,
            "charge_seen": self.charge_seen,
            "ignored_at": self.ignored_at,
            "ignored_count": self.ignored_count,
            "baseline_odo": self.baseline_odo,
        }

    @classmethod
    def from_dict(cls, data):
        guard = cls()
        if isinstance(data, dict):
            guard.last = data.get("last")
            guard.offset_km = data.get("offset_km") or 0
            guard.offset_kwh = data.get("offset_kwh") or 0
            guard.held_ending = data.get("held_ending")
            guard.charge_seen = bool(data.get("charge_seen"))
            guard.ignored_at = data.get("ignored_at")
            guard.ignored_count = data.get("ignored_count") or 0
            guard.baseline_odo = data.get("baseline_odo")
        return guard

    @property
    def holding(self):
        """True while figures are being held over an ignored reset."""
        return bool(self.offset_km or self.offset_kwh or self.held_ending is not None)

    def attributes(self):
        attrs = {
            "counter_reset_held": self.holding,
            "mileage_since_charge_from_odometer": self.odometer_in_km,
        }
        if self.ignored_at:
            attrs["ignored_counter_reset_at"] = self.ignored_at
            attrs["ignored_counter_resets"] = self.ignored_count
        return attrs

    # -- evidence --

    @staticmethod
    def _dropped(last, reading, key):
        new, old = reading.get(key), last.get(key)
        return new is not None and old is not None and new < old

    @staticmethod
    def _soc_evidence(last, reading):
        new, old = reading.get("soc"), last.get("soc")
        if new is None or old is None:
            return False
        rise = new - old
        if rise >= COUNTER_RESET_SOC_RISE_ANY_PCT:
            return True
        odo_new, odo_old = reading.get("odo"), last.get("odo")
        return (
            rise >= COUNTER_RESET_SOC_RISE_PARKED_PCT
            and odo_new is not None
            and odo_new == odo_old
        )

    @staticmethod
    def _record_evidence(last, reading):
        start, end = reading.get("start"), reading.get("end")
        return (
            bool(start)
            and end is not None
            and start < end
            and start != last.get("start")
        )

    # -- main entry point --

    def adjusted(self, reading):
        """The reading with any held offsets / ending power applied."""
        out = dict(reading)
        if out.get("km") is not None:
            out["km"] = self.offset_km + out["km"]
        if out.get("kwh") is not None:
            out["kwh"] = self.offset_kwh + out["kwh"]
        if self.held_ending is not None:
            out["ending"] = self.held_ending
        return out

    @staticmethod
    def _km_is_odometer(reading):
        km, odo = reading.get("km"), reading.get("odo")
        return km is not None and bool(odo) and km == odo

    def process(self, now_iso, reading):
        """Feed one raw reading. Returns ``(adjusted, event, persist)``.

        ``event`` is None, "ignored" or "accepted" (a reset accepted while
        figures were being held). ``persist`` says the state is worth saving:
        on any event, on every change while holding, and whenever the charge
        baseline moves, so a restart can't lose either.

        ``adjusted["km_from_odometer"]`` is True when SAIC's km was the
        odometer and was replaced (possibly by None).
        """
        bogus = self._km_is_odometer(reading)
        logic_reading = dict(reading, km=None) if bogus else reading
        self._accepted_reset = False
        baseline_before = self.baseline_odo

        adjusted, event, persist = self._process(now_iso, logic_reading)
        odo = reading.get("odo")

        if bogus:
            if self._accepted_reset:
                self.baseline_odo = odo  # a genuine charge, just now
            km_out = None
            if self.baseline_odo is not None and odo >= self.baseline_odo:
                km_out = odo - self.baseline_odo
            adjusted["km"] = km_out
            self.last["km"] = km_out
        elif adjusted.get("km") is not None and odo:
            self.baseline_odo = odo - adjusted["km"]
        adjusted["km_from_odometer"] = bogus
        self.odometer_in_km = bogus

        # Save when the baseline genuinely moves (a charge, or the first one
        # seen) -- not on the odd tenth of a km if SAIC's mileage and odometer
        # tick at slightly different moments while driving.
        if self.baseline_odo is not None and (
            baseline_before is None
            or abs(self.baseline_odo - baseline_before) > BASELINE_PERSIST_TOLERANCE
        ):
            persist = True
        return adjusted, event, persist

    def _process(self, now_iso, reading):
        """Feed one raw reading (km already cleared if it was the odometer). Returns ``(adjusted, event, persist)``.

        ``event`` is None, "ignored" or "accepted" (a reset accepted while
        figures were being held). ``persist`` says the state is worth saving:
        on any event, and on every change while holding, so a restart can't
        drop the held figures back to the raw post-reset ones.
        """
        if reading.get("plugged"):
            self.charge_seen = True
        last = self.last
        self.last = dict(reading)
        if last is None:
            return self.adjusted(reading), None, self.holding

        reset = (
            self._dropped(last, reading, "km")
            or self._dropped(last, reading, "kwh")
            or (
                reading.get("end") is not None
                and last.get("end") is not None
                and reading["end"] != last["end"]
            )
        )
        if not reset:
            if self._dropped(reading, last, "km"):  # km went UP: car driven
                # Plug evidence from before a drive says nothing about a
                # reset after it.
                self.charge_seen = bool(reading.get("plugged"))
            return self.adjusted(reading), None, self.holding and reading != last

        evidence = (
            self.charge_seen
            or self._soc_evidence(last, reading)
            or self._record_evidence(last, reading)
        )
        if evidence:
            self._accepted_reset = True
            was_holding = self.holding
            self.offset_km = self.offset_kwh = 0
            self.held_ending = None
            self.charge_seen = bool(reading.get("plugged"))
            return self.adjusted(reading), ("accepted" if was_holding else None), was_holding

        # Phantom: counters reset to 0, so fold the whole previous raw value
        # of each counter that dropped into its offset.
        if self._dropped(last, reading, "km"):
            self.offset_km += last["km"]
        if self._dropped(last, reading, "kwh"):
            self.offset_kwh += last["kwh"]
        if (
            self.held_ending is None
            and last.get("ending") is not None
            and reading.get("ending") != last.get("ending")
        ):
            self.held_ending = last["ending"]
        self.ignored_at = now_iso
        self.ignored_count += 1
        return self.adjusted(reading), "ignored", True


# ── Code-8 rejection advice ─────────────────────────────────────────────────
#
# SAIC answers several different rejections with return code 8. Until
# 2026-09-24 every one was reported as "remote command limit reached -- start
# the car with the physical key", including a transient rejection that cleared
# by itself 74 seconds later. The notification now quotes SAIC's own message
# and only gives advice that message supports.


def command_rejection_advice(saic_message):
    """What to tell the user, based only on what SAIC's message says."""
    text = (saic_message or "").lower()
    if any(word in text for word in ("limit", "maximum", "exceed", "number of times")):
        return (
            "SAIC says a limit has been reached. The remote-command counter "
            "resets when the vehicle is started with the key."
        )
    if "frequent" in text:
        return "SAIC says commands are being sent too often. Wait a minute and try again."
    return (
        "Try again in a minute. If every command keeps being rejected, check "
        "the log for SAIC's full response."
    )
