# File: trip_stats.py
"""Trip and efficiency statistics for MG SAIC vehicles (#301).

This module derives per-trip statistics (distance, energy/fuel used, and
efficiency) from the odometer, state-of-charge and fuel-level snapshots the
coordinator already collects, plus the known battery capacity and (for
combustion models) a per-model fuel-tank size.

Design (see discussion #301)
----------------------------
The SAIC message queue fires a type-323 "vehicle start" message on every
ignition-on, which the account poller turns into an immediate data refresh,
so the coordinator sees a fresh odometer/SOC/fuel reading right at the start
of a drive. There is no shutdown message, but the coordinator independently
detects the power-on -> power-off transition (``is_powered_on``) on a poll
and records ``last_powered_off_time``. So a trip is:

    OPEN   on the power-on transition   -> snapshot (odometer, soc, fuel, ts)
    CLOSE  on the power-off transition  -> snapshot again, compute the trip

Closing on power-off (rather than on the *next* start) means the end SOC/fuel
are captured before any charging or refuelling begins, which removes the
"charged between drives" ambiguity for the energy maths.

The maths in this file is deliberately pure and free of Home Assistant
imports so it can be unit-tested directly. ``TripStatsManager`` wraps it with
the HA ``Store`` persistence and event firing.

Accuracy notes
--------------
* Distance (odometer delta) is exact once the car is off.
* SOC and fuel level are integer percentages, so energy/fuel figures are
  coarse for very short trips.
* Trip *duration* is start-message time to power-off *detection* time, so it
  can trail the real shutdown by up to a poll interval — treat as approximate.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

# Minimum SOC rise (%) for a plugged-in period to be recorded as a charge.
# Filters out a plug-in that delivered nothing and the small SOC rebound the
# pack reports after a drive.
MIN_CHARGE_SOC_PCT = 0.5

# Abandon (rather than record) a charge session left open longer than this —
# a missed charge-stop shouldn't produce a nonsense figure days later.
MAX_OPEN_CHARGE_SECONDS = 48 * 3600

# Reject an odometer delta larger than this (km) as a single trip — protects
# against odometer rollover, the uint16 saturation sentinel slipping through,
# or a garbage reading. A genuine single drive won't exceed this.
MAX_PLAUSIBLE_TRIP_KM = 2000.0

# Minimum odometer movement (km) for a retrospective (never-seen-live) trip to
# be recorded, so odometer rounding / parking shuffles aren't logged as trips.
MIN_RETRO_TRIP_KM = 1.0

# Some cars' since-charge counters (mileageSinceLastCharge/powerUsageSinceLast
# Charge) reset spuriously — without an actual charge — including, it turns
# out, exactly at a trip's closing poll (#301, confirmed live on a BEV: SOC
# fell smoothly through the reset, so it wasn't a real charge). When that
# happens the counter-based distance computes as (post-reset value) minus a
# baseline that was JUST rebased to match it in the same poll, i.e. 0 - 0 = 0
# — a valid-looking number, not a missing one, so it would otherwise silently
# report "no trip" even though the odometer clearly moved. If the odometer
# shows a real drive (>= ODOMETER_SANITY_MIN_KM) while the counter says less
# than COUNTER_TRUST_MIN_KM, the counter is discarded for this trip (distance
# AND energy) and the odometer/SOC fallback is used instead.
ODOMETER_SANITY_MIN_KM = 1.0
COUNTER_TRUST_MIN_KM = 0.5

# A trip open longer than this (seconds) is assumed stuck — the power-off poll
# was missed — and is force-closed so it stops blocking new trips. Set well
# beyond any plausible single drive.
MAX_OPEN_TRIP_SECONDS = 24 * 3600

# Efficiency ratio helpers.
KM_PER_MILE = 1.609344


@dataclass
class TripSnapshot:
    """A single reading taken at a trip boundary.

    Carries both the raw odometer/SOC/fuel (fallback) and the car's own
    cumulative since-last-charge counters, which are the preferred source for
    distance and electric energy — see compute_completed_trip.
    """

    ts: str  # ISO-8601 timestamp string (storage-friendly)
    odometer_km: float
    soc_pct: float | None = None
    fuel_pct: float | None = None
    # Car's cumulative counters since the last charge (reset to ~0 at each
    # charge). Preferred for distance/energy because they're the car's own
    # measurements and don't depend on when the trip snapshot was taken.
    since_charge_km: float | None = None
    since_charge_kwh: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TripSnapshot | None":
        if not d:
            return None

        def _f(key):
            v = d.get(key)
            return None if v is None else float(v)

        try:
            return cls(
                ts=d["ts"],
                odometer_km=float(d["odometer_km"]),
                soc_pct=_f("soc_pct"),
                fuel_pct=_f("fuel_pct"),
                since_charge_km=_f("since_charge_km"),
                since_charge_kwh=_f("since_charge_kwh"),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class ChargeSnapshot:
    """A reading taken at the start or end of a charging session (#262).

    ``pack_energy_kwh`` is the car's own estimate of the energy sitting in the
    pack, derived as ``lastChargeEndingPower - powerUsageSinceLastCharge``
    (both already decimal-corrected, and scaled by the per-model energy
    correction where one applies). That identity holds at both boundaries: at
    the end of a charge the since-charge counter is ~0, so the expression
    collapses to lastChargeEndingPower itself.
    """

    ts: str  # ISO-8601 timestamp string (storage-friendly)
    soc_pct: float | None = None
    pack_energy_kwh: float | None = None
    odometer_km: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "ChargeSnapshot | None":
        if not d:
            return None

        def _f(key):
            v = d.get(key)
            return None if v is None else float(v)

        try:
            return cls(
                ts=d["ts"],
                soc_pct=_f("soc_pct"),
                pack_energy_kwh=_f("pack_energy_kwh"),
                odometer_km=_f("odometer_km"),
            )
        except (KeyError, TypeError, ValueError):
            return None


def _duration_seconds(start_ts: str, end_ts: str) -> int | None:
    try:
        start = datetime.fromisoformat(start_ts)
        end = datetime.fromisoformat(end_ts)
    except (TypeError, ValueError):
        return None
    delta = (end - start).total_seconds()
    if delta < 0:
        return None
    return int(delta)


def _counter_delta(current, baseline_value):
    """Delta of a cumulative since-charge counter against the last-close
    baseline. If it went backwards, the counter reset (a charge happened since
    the last close), so the current value IS the delta. Returns None if the
    current value is unavailable.
    """
    if current is None:
        return None
    if baseline_value is None or current < baseline_value:
        return round(current, 3)
    return round(current - baseline_value, 3)


def _efficiency_block(distance_km, distance_mi, energy_kwh):
    """The 5-key energy/efficiency block for one (distance, energy) pairing.
    Shared by the primary, _counter, and _soc figures so all three stay
    consistent. Returns all-None when either input is missing/non-positive.
    """
    if (
        energy_kwh is None
        or energy_kwh <= 0
        or distance_km is None
        or distance_km <= 0
    ):
        return {
            "energy_kWh": None,
            "efficiency_km_per_kWh": None,
            "efficiency_mi_per_kWh": None,
            "consumption_kWh_per_100km": None,
            "consumption_kWh_per_100mi": None,
        }
    distance_mi = distance_mi if distance_mi is not None else distance_km / KM_PER_MILE
    return {
        "energy_kWh": round(energy_kwh, 3),
        "efficiency_km_per_kWh": round(distance_km / energy_kwh, 2),
        "efficiency_mi_per_kWh": round(distance_mi / energy_kwh, 2),
        "consumption_kWh_per_100km": round(energy_kwh / distance_km * 100.0, 2),
        "consumption_kWh_per_100mi": round(energy_kwh / distance_mi * 100.0, 2),
    }


def compute_completed_trip(
    start: TripSnapshot,
    end: TripSnapshot,
    *,
    baseline: dict[str, Any] | None = None,
    capacity_kwh: float | None,
    tank_litres: float | None,
    is_electric: bool,
    is_combustion: bool,
    retrospective: bool = False,
) -> dict[str, Any] | None:
    """Compute a completed-trip dict for the drive ending at ``end``.

    Distance and electric energy come from the car's own cumulative counters
    (``mileageSinceLastCharge`` / ``powerUsageSinceLastCharge``) diffed against
    ``baseline`` — the counter values at the previous trip's close (or ~0 after
    a charge). This is the car's own measurement and, crucially, doesn't depend
    on when the trip's *open* snapshot was taken, so a late/fragmented open no
    longer skews the numbers. Falls back to the odometer delta (and SOC×capacity
    for energy) when the counters aren't available (e.g. non-charging models, or
    a charging-endpoint dropout).

    ``retrospective=True`` marks a trip reconstructed after the fact — one that
    was never observed live (the car wasn't polled while powered) or an open
    trip force-closed as stale. Such trips span an unknown window that may
    include a charge, so the since-charge counters can't be trusted: distance
    comes from the odometer and energy from the SOC change only. The trip is
    flagged ``retrospective: True`` / ``timing: approximate`` so it's
    distinguishable, and its timestamps bound the gap rather than the drive.

    Beyond the primary (unprefixed) distance/energy/efficiency figures — which
    keep picking counter-preferred-with-odometer/SOC-fallback exactly as
    before, for backward compatibility — this also exposes the counter-only
    and odometer+SOC-only figures independently as ``*_counter`` / ``*_soc``
    (energy) and ``distance_*_counter`` / ``distance_*_odometer`` (distance)
    attributes, so both can be compared directly (#301: some cars' counters
    appear to over-report energy even when not obviously reset). The counter
    figures are raw/unfiltered here — shown even when ``counter_reset_detected``
    discarded them from the primary selection, since a bogus counter reading is
    itself useful to see.

    Returns ``None`` when no plausible distance can be established. Individual
    electric/fuel figures are ``None`` when their inputs are missing.
    """
    if start is None or end is None:
        return None

    base_km = baseline.get("since_charge_km") if baseline else None
    base_kwh = baseline.get("since_charge_kwh") if baseline else None

    # Odometer delta is always computable and never resets mid-trip — used as
    # the fallback distance, the odometer-side of the *_soc figures, and the
    # sanity check against the counter below.
    odometer_delta_km = round(end.odometer_km - start.odometer_km, 2)
    odometer_delta_mi = odometer_delta_km / KM_PER_MILE

    # Raw counter-derived distance/energy — unfiltered by the reset sanity
    # check, so the *_counter attributes show what the counter actually said
    # even when it's discarded from the primary figures below.
    raw_counter_km = None if retrospective else _counter_delta(end.since_charge_km, base_km)
    raw_counter_kwh = (
        None
        if retrospective or not is_electric
        else _counter_delta(end.since_charge_kwh, base_kwh)
    )

    # SOC-derived energy, computed independently whenever SOC data allows it —
    # not just as a fallback for when the counter is missing. Paired with the
    # odometer distance (not the counter distance) for the *_soc figures, so
    # it's a fully self-consistent "odometer + SOC only" view.
    soc_used_pct = None
    soc_energy_kwh = None
    charged_during_park = False
    if is_electric and start.soc_pct is not None and end.soc_pct is not None:
        soc_delta = round(start.soc_pct - end.soc_pct, 1)
        if soc_delta < 0:
            charged_during_park = True
        else:
            soc_used_pct = soc_delta
            if capacity_kwh:
                soc_energy_kwh = round(soc_delta / 100.0 * capacity_kwh, 3)

    # ── Distance: prefer the since-charge counter, else the odometer delta ────
    # Retrospective trips always use the odometer (the counter may have reset in
    # the unobserved gap).
    counter_km = raw_counter_km

    # Sanity check: if the odometer shows a real drive but the counter says
    # (near) nothing, the counter reset mid-trip without an actual charge — a
    # known SAIC data-quality quirk, not tied to any one model. Trusting a
    # bogus ~0 counter value here would silently drop the whole trip (0 looks
    # like valid data, not "missing"), so discard the counter for BOTH distance
    # and energy and fall back to the odometer/SOC path instead. (This only
    # affects the primary figures — the raw *_counter attributes still show it.)
    counter_reset_detected = (
        counter_km is not None
        and odometer_delta_km >= ODOMETER_SANITY_MIN_KM
        and counter_km < COUNTER_TRUST_MIN_KM
    )
    if counter_reset_detected:
        counter_km = None

    distance_km = counter_km
    if distance_km is None:
        distance_km = odometer_delta_km
    if distance_km <= 0 or distance_km > MAX_PLAUSIBLE_TRIP_KM:
        return None

    distance_mi = distance_km / KM_PER_MILE
    trip: dict[str, Any] = {
        "distance_km": round(distance_km, 2),
        "distance_mi": round(distance_mi, 2),
        # Always available regardless of which source is primary — lets any
        # trip's distance be checked against the other source directly.
        "distance_km_counter": round(raw_counter_km, 2) if raw_counter_km is not None else None,
        "distance_mi_counter": (
            round(raw_counter_km / KM_PER_MILE, 2) if raw_counter_km is not None else None
        ),
        "distance_km_odometer": odometer_delta_km,
        "distance_mi_odometer": round(odometer_delta_mi, 2),
        "start_ts": start.ts,
        "end_ts": end.ts,
        "duration_s": _duration_seconds(start.ts, end.ts),
        # Electric
        "soc_used_pct": None,
        "energy_kWh": None,
        "efficiency_km_per_kWh": None,
        "efficiency_mi_per_kWh": None,
        "consumption_kWh_per_100km": None,
        "consumption_kWh_per_100mi": None,
        "charged_during_park": False,
        # Fuel
        "fuel_used_pct": None,
        "fuel_used_litres": None,
        "fuel_consumption_L_per_100km": None,
        "fuel_economy_mpg_uk": None,
        "fuel_economy_mpg_us": None,
        "refuelled_during_park": False,
    }
    if counter_reset_detected:
        # Distance came from the odometer (see above) because the since-charge
        # counter reset mid-trip without an actual charge; the counter's energy
        # figure is equally untrustworthy for this trip, so force the SOC
        # fallback below rather than trusting a near-zero counter value.
        trip["counter_reset_detected"] = True

    # ── Electric energy (BEV/PHEV) ───────────────────────────────────────────
    if is_electric:
        trip["charged_during_park"] = charged_during_park
        trip["soc_used_pct"] = soc_used_pct

        # Primary (unprefixed): counter-preferred, SOC-fallback — unchanged
        # behaviour from before this attribute expansion.
        primary_energy = None if retrospective or counter_reset_detected else raw_counter_kwh
        if primary_energy is None:
            primary_energy = soc_energy_kwh
        trip.update(_efficiency_block(distance_km, distance_mi, primary_energy))

        # Counter-only view: counter distance + counter energy, both raw/
        # unfiltered — a fully self-consistent "trust the counter" figure.
        for key, value in _efficiency_block(
            raw_counter_km, None, raw_counter_kwh
        ).items():
            trip[f"{key}_counter"] = value

        # Odometer+SOC-only view: odometer distance + SOC energy — a fully
        # self-consistent "trust SOC" figure, computed independently of
        # whether the counter was available or trusted for this trip.
        for key, value in _efficiency_block(
            odometer_delta_km, odometer_delta_mi, soc_energy_kwh
        ).items():
            trip[f"{key}_soc"] = value

    # ── Fuel (ICE/HEV/PHEV) ──────────────────────────────────────────────────
    if is_combustion and start.fuel_pct is not None and end.fuel_pct is not None:
        fuel_used = round(start.fuel_pct - end.fuel_pct, 1)
        if fuel_used < 0:
            trip["refuelled_during_park"] = True
        else:
            trip["fuel_used_pct"] = fuel_used
            if tank_litres:
                litres = round(fuel_used / 100.0 * tank_litres, 2)
                trip["fuel_used_litres"] = litres
                if litres > 0:
                    l_per_100km = round(litres / distance_km * 100.0, 2)
                    trip["fuel_consumption_L_per_100km"] = l_per_100km
                    if l_per_100km > 0:
                        # HA has no fuel-consumption device class, so provide
                        # mpg here for imperial users (both gallon definitions).
                        trip["fuel_economy_mpg_uk"] = round(282.481 / l_per_100km, 1)
                        trip["fuel_economy_mpg_us"] = round(235.215 / l_per_100km, 1)

    if retrospective:
        # Reconstructed after the fact: distance is sound but the drive happened
        # somewhere in the gap, so timestamps bound the gap (duration overstated)
        # and multiple short hops may be merged into one.
        trip["retrospective"] = True
        trip["timing"] = "approximate"

    return trip


def compute_since_charge_efficiency(
    distance_km: float | None, energy_kwh: float | None
) -> dict[str, Any] | None:
    """Efficiency from the API's own since-last-charge distance and energy.

    Needs no persistence — both inputs come straight from the charging
    endpoint (``mileageSinceLastCharge`` / ``powerUsageSinceLastCharge``).
    """
    if not distance_km or not energy_kwh or distance_km <= 0 or energy_kwh <= 0:
        return None
    distance_mi = distance_km / KM_PER_MILE
    return {
        "distance_km": round(distance_km, 2),
        "distance_mi": round(distance_mi, 2),
        "energy_kWh": round(energy_kwh, 3),
        "efficiency_km_per_kWh": round(distance_km / energy_kwh, 2),
        "efficiency_mi_per_kWh": round(distance_mi / energy_kwh, 2),
        "consumption_kWh_per_100km": round(energy_kwh / distance_km * 100.0, 2),
        "consumption_kWh_per_100mi": round(energy_kwh / distance_mi * 100.0, 2),
    }


def compute_soc_since_reset_efficiency(
    baseline_soc_pct: float | None,
    current_soc_pct: float | None,
    baseline_odometer_km: float | None,
    current_odometer_km: float | None,
    capacity_kwh: float | None,
) -> dict[str, Any] | None:
    """Efficiency since the SOC-detected reset point, as an SOC/odometer-only
    alternative to compute_since_charge_efficiency's counter-only figure (#301).

    Independent of the car's own since-last-charge counters entirely — uses
    only the odometer (never resets) and SOC×capacity (reported to 0.1%, so
    accurate even over short distances). Requested because two of the fields
    it replaces (mileageSinceLastCharge/powerUsageSinceLastCharge) are
    reported unreliably on some cars (spurious resets) and not reported at all
    on others (permanently Unknown, e.g. some MGS5s) — this sensor works on
    both, since it never touches those fields.

    The "reset point" here is whenever SOC was last seen to rise while parked
    (a charge) — see TripStatsManager.note_soc_reset_baseline, which only
    evaluates this while parked so a mid-drive regen uptick can't trigger it.
    Because the baseline isn't necessarily "at 100% right after a full
    charge" (a partial charge, or any other SOC rise, also triggers it), this
    is honestly a "since reset" figure rather than a charge-accurate one, but
    it uses the same epoch boundary as the counter-based figure it's
    replacing/complementing.

    Returns ``None`` when there's no baseline yet or SOC hasn't dropped.
    """
    if (
        baseline_soc_pct is None
        or current_soc_pct is None
        or baseline_odometer_km is None
        or current_odometer_km is None
    ):
        return None
    distance_km = round(current_odometer_km - baseline_odometer_km, 2)
    soc_used_pct = round(baseline_soc_pct - current_soc_pct, 1)
    if distance_km <= 0 or soc_used_pct <= 0 or not capacity_kwh:
        return None
    energy_kwh = round(soc_used_pct / 100.0 * capacity_kwh, 3)
    if energy_kwh <= 0:
        return None
    distance_mi = distance_km / KM_PER_MILE
    return {
        "distance_km": distance_km,
        "distance_mi": round(distance_mi, 2),
        "soc_used_pct": soc_used_pct,
        "baseline_soc_pct": baseline_soc_pct,
        "energy_kWh": energy_kwh,
        "efficiency_km_per_kWh": round(distance_km / energy_kwh, 2),
        "efficiency_mi_per_kWh": round(distance_mi / energy_kwh, 2),
        "consumption_kWh_per_100km": round(energy_kwh / distance_km * 100.0, 2),
        "consumption_kWh_per_100mi": round(energy_kwh / distance_mi * 100.0, 2),
    }




def compute_charge_session(
    start: "ChargeSnapshot",
    end: "ChargeSnapshot",
    *,
    capacity_kwh: float | None,
) -> dict[str, Any] | None:
    """Energy delivered into the battery during one charging session (#262).

    Requested by @HarryFlatter: the API reports charging *power* live and
    ``powerUsageSinceLastCharge`` (energy taken *out* since the last charge),
    but nothing for "how much did that charge put *in*" — which is what you
    need when you're charging on someone else's supply and want to settle up.
    There is no ``lastChargeStartingPower`` field to subtract, so it has to be
    measured across the session.

    Two independent figures are produced, in the same
    show-both-and-let-the-car-tell-us style as the trip sensors:

    * ``soc``     — (SOC rise) × usable capacity. Always available on a car
      that reports SOC and has a known capacity, and SOC is reported to 0.1 %.
    * ``counter`` — the delta of the car's own pack-energy figure
      (``lastChargeEndingPower - powerUsageSinceLastCharge``). Independent of
      the capacity we hold for the model, but it relies on the car refreshing
      lastChargeEndingPower promptly at the end of the session.

    The SOC figure is the headline value because it is available on every
    model; the counter figure rides along as an attribute so the two can be
    compared on real cars. Both are *battery-side* energy — always less than
    the energy drawn at the wall, which also covers charger and cable losses.

    Returns ``None`` when neither method can produce a plausible figure.
    """
    if start is None or end is None:
        return None

    result: dict[str, Any] = {"start_ts": start.ts, "end_ts": end.ts}

    duration_s = _duration_seconds(start.ts, end.ts)
    if duration_s is not None:
        result["duration_s"] = duration_s

    energy_soc = None
    if start.soc_pct is not None and end.soc_pct is not None:
        soc_added = round(end.soc_pct - start.soc_pct, 1)
        result["soc_start_pct"] = start.soc_pct
        result["soc_end_pct"] = end.soc_pct
        result["soc_added_pct"] = soc_added
        if soc_added >= MIN_CHARGE_SOC_PCT and capacity_kwh:
            energy_soc = round(soc_added / 100.0 * capacity_kwh, 3)

    energy_counter = None
    if start.pack_energy_kwh is not None and end.pack_energy_kwh is not None:
        delta = round(end.pack_energy_kwh - start.pack_energy_kwh, 3)
        # Guard against the car not having refreshed lastChargeEndingPower yet
        # (delta <= 0) or reporting something larger than the pack can hold.
        if delta > 0 and (capacity_kwh is None or delta <= capacity_kwh * 1.05):
            energy_counter = delta

    if energy_soc is None and energy_counter is None:
        return None

    energy = energy_soc if energy_soc is not None else energy_counter
    result["energy_added_kWh"] = energy
    result["method"] = "soc" if energy_soc is not None else "counter"
    if energy_soc is not None:
        result["energy_added_kWh_soc"] = energy_soc
    if energy_counter is not None:
        result["energy_added_kWh_counter"] = energy_counter
    if start.odometer_km is not None:
        result["odometer_km"] = start.odometer_km
    if duration_s and duration_s > 0 and energy:
        result["average_power_kW"] = round(energy / (duration_s / 3600.0), 2)
    return result


# HA imports are done lazily inside methods so the pure functions above can be
# imported and unit-tested without Home Assistant installed.

STORAGE_VERSION = 1
EVENT_TRIP_COMPLETED = "mg_saic_trip_completed"
EVENT_CHARGE_COMPLETED = "mg_saic_charge_completed"


class TripStatsManager:
    """Owns the open/last trip state for one VIN and persists it across restarts.

    Lifecycle:
      * ``async_load`` once during coordinator setup.
      * ``open`` when a drive is detected (power_mode on) and no trip is open.
      * ``close`` when the drive ends (power_mode off) -> stores ``last_trip``,
        fires the ``mg_saic_trip_completed`` event, clears the open snapshot.
      * ``async_save`` persists after each open/close.

    Only the *open* snapshot needs to survive a restart (so a trip in progress
    isn't lost), plus the last completed trip so the sensors repopulate
    immediately after a restart rather than showing Unknown.
    """

    def __init__(self, hass, entry_id: str, vin: str) -> None:
        self._hass = hass
        self._vin = vin
        self._entry_id = entry_id
        self._store = None  # created in async_load
        self.open_snapshot: TripSnapshot | None = None
        self.last_trip: dict[str, Any] | None = None
        # Since-charge counter values at the last trip close (rebased to ~0 when
        # a charge resets the counter). Distance/energy for the next trip diff
        # against this — see note_since_charge / close.
        self.since_charge_baseline: dict[str, Any] | None = None
        # The most recent reading taken while parked with no trip open. Used to
        # reconstruct trips that were never seen live (the car wasn't polled
        # while powered) — see detect_missed_trip.
        self.last_parked_snapshot: TripSnapshot | None = None
        # SOC/odometer at the last-seen "since reset" epoch boundary — a charge
        # (SOC rise) observed while parked. Powers the SOC-based Efficiency
        # Since Charge (SOC) sensor, entirely independent of the since-charge
        # counter fields — see note_soc_reset_baseline.
        self.soc_reset_baseline: dict[str, Any] | None = None
        # Charging-session tracking (#262): the snapshot taken when a charge
        # started, and the last completed charge. Powers the Last Charge Energy
        # sensor — the API has no "energy added by that charge" field.
        self.open_charge: ChargeSnapshot | None = None
        self.last_charge: dict[str, Any] | None = None

    async def async_load(self) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(
            self._hass, STORAGE_VERSION, f"mg_saic_trips_{self._entry_id}_{self._vin}"
        )
        data = await self._store.async_load() or {}
        self.open_snapshot = TripSnapshot.from_dict(data.get("open_snapshot"))
        self.last_trip = data.get("last_trip")
        self.since_charge_baseline = data.get("since_charge_baseline")
        self.last_parked_snapshot = TripSnapshot.from_dict(
            data.get("last_parked_snapshot")
        )
        self.soc_reset_baseline = data.get("soc_reset_baseline")
        self.open_charge = ChargeSnapshot.from_dict(data.get("open_charge"))
        self.last_charge = data.get("last_charge")

    async def async_save(self) -> None:
        """Persist current open/last-trip state and the since-charge baseline."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "open_snapshot": (
                    self.open_snapshot.to_dict() if self.open_snapshot else None
                ),
                "last_trip": self.last_trip,
                "since_charge_baseline": self.since_charge_baseline,
                "last_parked_snapshot": (
                    self.last_parked_snapshot.to_dict()
                    if self.last_parked_snapshot
                    else None
                ),
                "soc_reset_baseline": self.soc_reset_baseline,
                "open_charge": (
                    self.open_charge.to_dict() if self.open_charge else None
                ),
                "last_charge": self.last_charge,
            }
        )

    def note_since_charge(self, km, kwh) -> bool:
        """Track the since-charge counters each poll to catch a charge reset.

        When the counter drops below the stored baseline, a charge has zeroed it,
        so rebase to the new low. Returns True if the baseline changed (caller
        may persist). Called every poll from the coordinator.
        """
        if km is None:
            return False
        if self.since_charge_baseline is None:
            self.since_charge_baseline = {
                "since_charge_km": round(km, 3),
                "since_charge_kwh": round(kwh, 3) if kwh is not None else 0.0,
            }
            return True
        if km < self.since_charge_baseline.get("since_charge_km", 0.0):
            self.since_charge_baseline = {
                "since_charge_km": round(km, 3),
                "since_charge_kwh": round(kwh, 3) if kwh is not None else 0.0,
            }
            return True
        return False

    def note_soc_reset_baseline(self, soc_pct, odometer_km, ts) -> bool:
        """Track SOC while parked to detect a charge (SOC rise) and rebase the
        SOC-based "since reset" baseline — the odometer/SOC-only counterpart to
        note_since_charge, entirely independent of the since-charge counter
        fields (#301: those are unreliable on some cars, absent on others).

        Only ever called while parked (the coordinator gates this), so a
        mid-drive regen SOC uptick can never be mistaken for a charge here.
        Returns True if the baseline changed (caller may persist).
        """
        if soc_pct is None or odometer_km is None:
            return False
        if self.soc_reset_baseline is None or soc_pct > self.soc_reset_baseline.get(
            "soc_pct", -1.0
        ):
            self.soc_reset_baseline = {
                "soc_pct": round(soc_pct, 1),
                "odometer_km": round(odometer_km, 3),
                "ts": ts,
            }
            return True
        return False

    def note_charge_state(
        self,
        is_charging: bool,
        snapshot: "ChargeSnapshot | None",
        *,
        capacity_kwh: float | None,
        now_iso: str,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Open/close a charging session (#262).

        Returns ``(completed_charge_or_None, state_changed)``; the caller
        persists when state_changed and fires an event for a completed charge.

        Called only on polls where charging data was actually returned — a
        failed charging fetch drops charging_data to None and flips is_charging
        to False, which would otherwise look exactly like the charge ending.
        That matters here: on some cars (#262) the charging endpoint reliably
        goes quiet the moment a session completes, so treating a dropout as an
        end-of-charge would record a phantom session on every outage.
        """
        if snapshot is None:
            return None, False

        if is_charging:
            if self.open_charge is None:
                self.open_charge = snapshot
                return None, True
            # Already charging — nothing to do. The start snapshot stands.
            return None, False

        if self.open_charge is None:
            return None, False

        start = self.open_charge
        self.open_charge = None

        age = _duration_seconds(start.ts, now_iso)
        if age is not None and age > MAX_OPEN_CHARGE_SECONDS:
            # A charge-stop we never saw. Abandon rather than invent a figure.
            return None, True

        charge = compute_charge_session(start, snapshot, capacity_kwh=capacity_kwh)
        if charge is None:
            return None, True
        self.last_charge = charge
        return charge, True

    def open(self, snapshot: TripSnapshot) -> bool:
        """Record the start-of-drive snapshot (synchronous). Returns True if a
        new trip was opened.

        If a trip is already open we keep the *earlier* start — a duplicate
        power-on shouldn't reset the odometer baseline mid-drive. Synchronous so
        two rapid polls can't both open (the second sees open_snapshot set).
        Callers persist via async_save afterwards.
        """
        if self.open_snapshot is not None:
            return False
        self.open_snapshot = snapshot
        return True

    def close(
        self,
        snapshot: TripSnapshot,
        *,
        capacity_kwh: float | None,
        tank_litres: float | None,
        is_electric: bool,
        is_combustion: bool,
    ) -> dict[str, Any] | None:
        """Close the open trip against ``snapshot`` and return the trip dict
        (synchronous). Returns None (and clears state) if there was no open trip
        or the pair didn't form a plausible trip. Callers persist afterwards.
        """
        start = self.open_snapshot
        self.open_snapshot = None
        if start is None:
            return None

        trip = compute_completed_trip(
            start,
            snapshot,
            baseline=self.since_charge_baseline,
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
        )
        return self._finalise(trip, snapshot)

    def _finalise(
        self, trip: dict[str, Any] | None, end: TripSnapshot
    ) -> dict[str, Any] | None:
        """Common tail for close / detect_missed_trip / force_close_if_stale:
        rebase the since-charge baseline to ``end``, mark ``end`` as the latest
        parked reading, store & fire the trip if one was produced.
        """
        if end.since_charge_km is not None:
            self.since_charge_baseline = {
                "since_charge_km": round(end.since_charge_km, 3),
                "since_charge_kwh": (
                    round(end.since_charge_kwh, 3)
                    if end.since_charge_kwh is not None
                    else 0.0
                ),
            }
        self.last_parked_snapshot = end
        if trip is not None:
            self.last_trip = trip
            self._fire_event(trip)
        return trip

    def detect_missed_trip(
        self,
        snapshot: TripSnapshot,
        *,
        capacity_kwh: float | None,
        tank_litres: float | None,
        is_electric: bool,
        is_combustion: bool,
    ) -> dict[str, Any] | None:
        """Reconstruct a trip that was never seen live.

        Called on a parked poll when no trip is open. If the odometer has
        advanced since the last parked reading, a drive happened between polls
        (the car wasn't polled while powered). Records it as a retrospective
        trip — odometer distance, SOC-based energy, approximate timestamps — and
        advances the parked baseline. Returns the trip, or None when there was
        no baseline yet or no meaningful movement.
        """
        start = self.last_parked_snapshot
        if (
            start is None
            or snapshot.odometer_km - start.odometer_km < MIN_RETRO_TRIP_KM
        ):
            # Nothing to reconstruct — just advance the parked baseline.
            self.last_parked_snapshot = snapshot
            return None
        trip = compute_completed_trip(
            start,
            snapshot,
            baseline=None,  # counters can't be trusted across the unseen gap
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
            retrospective=True,
        )
        return self._finalise(trip, snapshot)

    def force_close_if_stale(
        self,
        now_iso: str,
        snapshot: TripSnapshot | None,
        *,
        capacity_kwh: float | None,
        tank_litres: float | None,
        is_electric: bool,
        is_combustion: bool,
    ) -> dict[str, Any] | None:
        """Force-close a trip that has been open implausibly long.

        If the power-off poll was missed (or the car reports 'on' indefinitely),
        a trip can stay open forever and block all new trips. When the open trip
        is older than MAX_OPEN_TRIP_SECONDS, close it as a retrospective trip
        against the current reading (or abandon it, clearing state, if we have no
        reading). Returns the trip, or None if nothing was stale.
        """
        if self.open_snapshot is None:
            return None
        age = _duration_seconds(self.open_snapshot.ts, now_iso)
        if age is None or age < MAX_OPEN_TRIP_SECONDS:
            return None
        start = self.open_snapshot
        self.open_snapshot = None
        end = snapshot if snapshot is not None else start
        trip = compute_completed_trip(
            start,
            end,
            baseline=None,
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
            retrospective=True,
        )
        return self._finalise(trip, end)

    def _fire_event(self, trip: dict[str, Any]) -> None:
        try:
            self._hass.bus.async_fire(
                EVENT_TRIP_COMPLETED, {"vin": self._vin, **trip}
            )
        except Exception:  # noqa: BLE001 - event firing must never break a poll
            pass

    def fire_charge_event(self, charge: dict[str, Any]) -> None:
        """Fire mg_saic_charge_completed so automations can react to a finished
        charge (#262) — the same contract as the trip event."""
        try:
            self._hass.bus.async_fire(
                EVENT_CHARGE_COMPLETED, {"vin": self._vin, **charge}
            )
        except Exception:  # noqa: BLE001 - event firing must never break a poll
            pass
