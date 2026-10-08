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

from dataclasses import dataclass, asdict, replace
from datetime import datetime, timezone
from typing import Any

# Minimum SOC rise (%) above the lowest point seen since the baseline was set
# for the car to be considered charging. Above the post-drive pack rebound
# (typically 0.1-0.4%), below any charge worth rebasing on.
SOC_CHARGE_RISE_PCT = 0.5

# A SOC rise only counts as a charge if the car hasn't moved since the last
# parked reading. Charging happens standing still; regen needs movement, so a
# moved odometer means the gain came from driving (#354). 0.05 km is below the
# API's 0.1 km odometer resolution, so any real movement clears it.
REGEN_ODOMETER_MOVED_KM = 0.05

# Minimum SOC rise (%) for a plugged-in period to be recorded as a charge.
# Filters out a plug-in that delivered nothing and the small SOC rebound the
# pack reports after a drive.
MIN_CHARGE_SOC_PCT = 0.5

# A fuel-level RISE of at least this many percentage points across a trip is
# treated as a refuel rather than sender noise (#354, @HarryFlatter).
#
# Unlike the SOC side, there is no refuel-session tracking to check against --
# the car reports no "refuelling" state, and a refuel leaves no trace beyond
# the level going up, so this is a magnitude judgement rather than evidence.
# Fuel senders are genuinely noisy (slosh on hills and cornering moves the
# reading by a few points either way), so the threshold has to clear that
# noise floor. 5 points is roughly 2 litres in a 37 L tank -- below any real
# splash-and-dash, comfortably above ordinary slosh.
REFUEL_MIN_RISE_PCT = 5.0

# Abandon (rather than record) a charge session left open longer than this —
# a missed charge-stop shouldn't produce a nonsense figure days later.
MAX_OPEN_CHARGE_SECONDS = 48 * 3600
# A charge that stops while the car stays plugged in, without the car saying
# it has finished, is treated as paused rather than over -- cars pause briefly
# as the battery nears full, and a charger can pause one too. If it is still
# not charging this long after the first reading that saw it stopped, the
# charge is taken to have ended when it stopped.
CHARGE_PAUSE_MAX_SECONDS = 20 * 60

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

# Below this distance a trip's efficiency is not shown as the headline
# figure (#407, @hoffeck). The odometer moves in whole kilometres on the
# cars seen so far, so a trip's distance can be out by up to a kilometre
# either way: a "1 km" trip is anything from just over 0 to just under 2 km,
# and two 1 km trips on an MG4 came out at 2.7 and 9.43 km/kWh. At 2 km
# (1.2 miles) the worst case is still +/-50 %, at 5 km +/-20 %; 2 km is where
# the reported problem stops, and a higher bar would hide more real trips.
# The figures are still worked out and kept in the *_soc / *_counter
# attributes; only the primary ones are left blank.
MIN_EFFICIENCY_TRIP_KM = 2.0

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
    range_km: float | None = None  # remaining electric range at the boundary
    # The car's own record of its most recent charge (rvsChargeStatus
    # startTime / endTime, epoch seconds) as it stood at this boundary. HA
    # only sees a session start or end when it polls -- up to a whole
    # interval late at each end -- so this is what gives the real duration.
    record_start: float | None = None
    record_end: float | None = None

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
                range_km=_f("range_km"),
                record_start=_f("record_start"),
                record_end=_f("record_end"),
            )
        except (KeyError, TypeError, ValueError):
            return None


# The car's charge record must end no later than this after HA saw the session
# end (clock skew between the car, SAIC and HA).
CAR_RECORD_END_SLACK_S = 600
# ...and can't claim a charge longer than this (a corrupt record).
CAR_RECORD_MAX_DURATION_S = 48 * 3600


def _car_charge_window(start, end):
    """The car's own (start, end) for this session, in epoch seconds, or None.

    Only when it's demonstrably THIS session's record: the record's end
    changed during the session (so it isn't the previous charge's), it ends
    inside HA's observed window, and it's a sane span.
    """
    rec_start = getattr(end, "record_start", None)
    rec_end = getattr(end, "record_end", None)
    if not rec_start or not rec_end or rec_end <= rec_start:
        return None
    if rec_end - rec_start > CAR_RECORD_MAX_DURATION_S:
        return None
    if rec_end == getattr(start, "record_end", None):
        return None  # unchanged since the session began: the previous charge
    try:
        seen_start = datetime.fromisoformat(start.ts).timestamp()
        seen_end = datetime.fromisoformat(end.ts).timestamp()
    except (TypeError, ValueError):
        return None
    if not (seen_start <= rec_end <= seen_end + CAR_RECORD_END_SLACK_S):
        return None
    return rec_start, rec_end


def track_charge_record(tracker, snapshot, *, previous_end=None):
    """Follow the car's charge record across restarts, while a session is open.

    The car restarts its record -- startTime, and its Charging Duration
    counter -- every time charging itself restarts. @HarryFlatter's HS PHEV,
    2 Oct (times BST), charged in three stretches:

        00:36:43 -> 01:37:44     38 s pause
        01:38:22 -> 01:54:45     36 s pause
        01:55:21 -> 02:21:33

    At the end only the last stretch was left in the record, so Last Charge
    showed 26 minutes at 19.8 kW for a charge of 1 h 44 min at about 5 kW.

    While the car is charging, startTime is the start of the stretch now
    running and endTime is where the previous one (or the previous charge)
    stopped -- so startTime is later than endTime. Each poll this keeps the
    earliest such start, and when the start moves on it counts an
    interruption and adds up the pause the car reports (new start - the end
    before it).

    ``previous_end`` is the record's end as it stood on the last reading
    BEFORE charging (when there was one). If the first charging reading
    already shows a different end, a stretch began and ended between those
    two polls and its start was never seen: ``missed_start``.

    Returns ``(tracker, changed)``. ``tracker`` is a plain dict (it is stored)
    or None when the car has given nothing usable yet.
    """
    rec_start = getattr(snapshot, "record_start", None)
    rec_end = getattr(snapshot, "record_end", None)
    if not rec_start:
        return tracker, False
    if rec_end and rec_start <= rec_end:
        # A finished record, not a stretch in progress.
        return tracker, False
    if tracker is None:
        return {
            "first_start": rec_start,
            "last_start": rec_start,
            "paused_s": 0.0,
            "interruptions": 0,
            "missed_start": bool(
                previous_end and rec_end and rec_end != previous_end
            ),
        }, True
    last_start = tracker.get("last_start")
    if not last_start or rec_start <= last_start:
        return tracker, False
    tracker = dict(tracker)
    if rec_end and last_start < rec_end < rec_start:
        tracker["paused_s"] = float(tracker.get("paused_s") or 0.0) + (
            rec_start - rec_end
        )
    tracker["interruptions"] = int(tracker.get("interruptions") or 0) + 1
    tracker["last_start"] = rec_start
    return tracker, True


def _session_car_window(car_window, tracker):
    """The whole session from the car's records: the final record (``car_window``,
    already checked to be this session's) widened back to the earliest start
    seen while it was open.

    Returns ``(start, end, paused_s, interruptions, missed_start)``.
    """
    rec_start, rec_end = car_window
    first = tracker.get("first_start") if tracker else None
    if (
        not first
        or first > rec_start
        or rec_end - first > CAR_RECORD_MAX_DURATION_S
    ):
        return rec_start, rec_end, 0.0, 0, False
    paused = float(tracker.get("paused_s") or 0.0)
    interruptions = int(tracker.get("interruptions") or 0)
    if rec_start > (tracker.get("last_start") or rec_start):
        # It restarted once more after the last poll that saw it charging.
        # How long that pause was is not known.
        interruptions += 1
    if paused < 0 or paused >= rec_end - first:
        paused = 0.0
    return first, rec_end, paused, interruptions, bool(tracker.get("missed_start"))


# The longest stretch at either end of a charge that is filled in from the
# nearest power reading (see measured_charge_energy). Past this the reading
# says too little about what happened in between.
MEASURED_ENERGY_MAX_EDGE_S = 3600


def _add_power_reading(tracker, at, power_kw):
    """One more reading on the running total (``tracker`` is already a copy)."""
    gap = at - tracker["last_ts"]
    tracker["kwh"] += (tracker["last_kw"] + power_kw) / 2.0 * gap / 3600.0
    tracker["max_gap_s"] = max(float(tracker.get("max_gap_s") or 0.0), gap)
    tracker["last_ts"] = at
    tracker["last_kw"] = float(power_kw)
    tracker["samples"] = int(tracker.get("samples") or 0) + 1
    return tracker


def _settle_held_zero(tracker):
    """Count a zero reading that was being held back (see track_charge_power)."""
    held = tracker.pop("held_zero_ts", None)
    if held is not None and held > tracker["last_ts"]:
        _add_power_reading(tracker, held, 0.0)
    return tracker


def track_charge_power(tracker, ts, power_kw, *, charging=False):
    """Add one reading of the pack's charging power to the running total.

    Energy between two readings is the average of the two powers times the
    time between them. How good that is depends entirely on how often the
    car is polled: a reading a minute on a DC charger follows the taper; one
    every half hour on AC is fine while the power is steady and misses
    whatever happened in between.

    ``charging``: the car said it was charging at this reading. A reading of
    exactly zero is then suspect. @hoffeck's MG4 EV Urban (#407, 7 Oct 2026)
    gave one 0.0 kW between 9 kW readings on a half-hour AC charge while the
    battery level kept rising; averaged in like any other reading it took
    about 0.3 kWh off a 3.9 kWh charge, which was the whole of the gap
    between this figure and the one worked out from the battery level. So a
    zero while charging is held back until the next reading: if power is
    flowing again it was a blip and is left out (and counted in
    ``ignored_zero_samples``); if the next reading is zero too, or the charge
    has paused or ended, it was real and is counted.

    ``tracker`` is a plain dict (it is stored) or None before the first
    reading. A missing or nonsensical power leaves it unchanged.
    """
    if (
        not isinstance(power_kw, (int, float))
        or isinstance(power_kw, bool)
        or power_kw != power_kw  # NaN
        or power_kw < 0
    ):
        return tracker
    try:
        at = datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return tracker
    if tracker is None:
        return {
            "kwh": 0.0,
            "first_ts": at,
            "first_kw": float(power_kw),
            "last_ts": at,
            "last_kw": float(power_kw),
            "samples": 1,
            "max_gap_s": 0.0,
        }
    held = tracker.get("held_zero_ts")
    if at <= max(tracker["last_ts"], held or 0.0):
        return tracker
    tracker = dict(tracker)
    if held is not None:
        if charging and power_kw > 0:
            # Power either side of it: a blip. Leave it out.
            del tracker["held_zero_ts"]
            tracker["ignored_zero_samples"] = (
                int(tracker.get("ignored_zero_samples") or 0) + 1
            )
        else:
            _settle_held_zero(tracker)
    elif charging and power_kw == 0 and tracker["last_kw"] > 0:
        tracker["held_zero_ts"] = at
        return tracker
    return _add_power_reading(tracker, at, power_kw)


def measured_charge_energy(tracker, *, charge_start=None, charge_end=None):
    """Energy into the pack from the power readings taken during a charge.

    Suggested by @hoffeck (#407): Last Charge Energy is the SOC change times
    the pack size, which is a calculation; pack voltage times current, added
    up over the charge, is a measurement.

    The readings only cover first reading to last. The two ends are filled in
    from the car's own start and end times (epoch seconds), taking the power
    to have been what the nearest reading saw -- on his DC charge the first
    reading came about two minutes in, by which time the battery had gained
    3.8 %. What was filled in is reported separately so it can be judged.

    Returns the keys to add to the charge, or None when there were fewer than
    two readings. ``energy_measured_ignored_zero_samples`` is only there when
    a zero reading was left out (see track_charge_power).
    """
    if tracker and tracker.get("held_zero_ts") is not None:
        # The charge ended on a zero reading: nothing followed to show it was
        # a blip, so it counts.
        tracker = _settle_held_zero(dict(tracker))
    if not tracker or int(tracker.get("samples") or 0) < 2:
        return None
    measured = float(tracker.get("kwh") or 0.0)
    edges = 0.0
    lead = (tracker["first_ts"] - charge_start) if charge_start else 0
    if 0 < lead <= MEASURED_ENERGY_MAX_EDGE_S:
        edges += tracker["first_kw"] * lead / 3600.0
    tail = (charge_end - tracker["last_ts"]) if charge_end else 0
    if 0 < tail <= MEASURED_ENERGY_MAX_EDGE_S:
        edges += tracker["last_kw"] * tail / 3600.0
    total = measured + edges
    if total <= 0:
        return None
    result = {
        "energy_measured_kWh": round(total, 3),
        "energy_measured_estimated_kWh": round(edges, 3),
        "energy_measured_samples": int(tracker["samples"]),
        "energy_measured_max_gap_s": int(round(tracker.get("max_gap_s") or 0)),
    }
    ignored = int(tracker.get("ignored_zero_samples") or 0)
    if ignored:
        result["energy_measured_ignored_zero_samples"] = ignored
    return result


def charge_session_progress(start, snapshot, tracker, *, charging, stretch_s=None):
    """How long the open charge has spent charging, as of ``snapshot``.

    The figure Last Charge reports when the charge ends, worked out while it
    is still going: the stretches already finished, plus the one now running,
    with the pauses left out. It moves when the car is polled, not in between.

    @HarryFlatter's HS PHEV, 4 Oct 2026 (times BST), charged in four stretches
    with three pauses of under a minute. The car's own Charging Duration
    counter restarts with each stretch, so the readings HA took of it were
    11 s, 403 s, 1085 s, 21 s, 261 s and 0 -- nothing that adds up to the
    1 h 43 min 5 s the car spent charging:

        reading   counter   stretches before it   so far
        00:32:13     11 s                    0      11 s
        01:02:57    403 s               1413 s    1816 s
        01:14:18   1085 s               1413 s    2498 s
        01:45:04     21 s               4270 s    4291 s
        02:15:51    261 s               5829 s    6090 s
        (ended)                                   6185 s

    ``start`` is the session's opening snapshot, ``tracker`` the car's record
    as followed by track_charge_record (already updated with this reading).

    ``charging``: a stretch is running now. Its length so far is the car's own
    counter (``stretch_s``, seconds) when that is believable, so that this
    figure is the finished stretches plus what Charging Duration shows; and
    otherwise the time since the stretch started. Not charging means the
    reading landed in a pause, and the car's record then ends where the last
    stretch stopped.

    Returns a dict (it is stored), or None when this reading says nothing new
    (the previous figure then stands).
    """
    if snapshot is None:
        return None
    try:
        seen = datetime.fromisoformat(snapshot.ts).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None
    first = tracker.get("first_start") if tracker else None
    if not first:
        # The car gives no record of its own (or none yet): all there is to go
        # on is when HA's readings were taken, as at the end of the charge.
        elapsed = _duration_seconds(getattr(start, "ts", None), snapshot.ts)
        if elapsed is None:
            return None
        return {
            "duration_s": elapsed,
            "duration_source": "polls",
            "interruptions": 0,
            "paused_s": 0,
            "as_of": snapshot.ts,
        }

    last = tracker.get("last_start") or first
    paused = float(tracker.get("paused_s") or 0.0)
    interruptions = int(tracker.get("interruptions") or 0)
    if paused < 0:
        paused = 0.0
    if charging:
        since_start = max(0.0, seen - last)
        running = since_start
        if (
            isinstance(stretch_s, (int, float))
            and not isinstance(stretch_s, bool)
            and 0 <= stretch_s <= since_start + CAR_RECORD_END_SLACK_S
        ):
            running = float(stretch_s)
        duration = (last - first - paused) + running
    else:
        rec_start = getattr(snapshot, "record_start", None)
        rec_end = getattr(snapshot, "record_end", None)
        if not rec_start or not rec_end or rec_end < rec_start or rec_start < last:
            # The car has not written the stopped stretch's end yet.
            return None
        if rec_start > last:
            # It restarted, and stopped again, since the last reading. How
            # long that pause was is not known.
            interruptions += 1
        duration = rec_end - first - paused
    if duration < 0 or duration > CAR_RECORD_MAX_DURATION_S:
        return None
    return {
        "duration_s": int(duration),
        "duration_source": "car_partial" if tracker.get("missed_start") else "car",
        "interruptions": interruptions,
        "paused_s": int(round(paused)),
        "charge_start_ts": datetime.fromtimestamp(first, timezone.utc).isoformat(),
        "as_of": snapshot.ts,
    }


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


def _overlaps(charge, start_ts, end_ts):
    """True if a completed charge session's window intersects [start_ts, end_ts].

    ``charge`` is a trip_stats manager's ``last_charge`` dict (or None) — the
    same record ``compute_charge_session`` produces, carrying ``start_ts``/
    ``end_ts`` as ISO-8601 strings, which sort correctly as plain strings.
    Returns False on anything malformed rather than raising, since this is
    only ever used to decide whether to trust a heuristic, never something
    load-bearing enough to justify an exception mid-trip-close.
    """
    if not charge:
        return False
    charge_start = charge.get("start_ts")
    charge_end = charge.get("end_ts")
    if not charge_start or not charge_end:
        return False
    return charge_start <= end_ts and charge_end >= start_ts


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
    last_charge: dict[str, Any] | None = None,
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
        # A rise here is not on its own evidence of an external charge — a
        # PHEV/HEV's engine or regen can legitimately raise SOC net across a
        # trip with nothing plugged in at all (#354's mistake, one code path
        # over: any SOC rise treated as proof of an outside event). The
        # positive check available here is the manager's own charge-session
        # tracking: if a completed charge's window actually overlaps this
        # trip, that is real evidence, not an inference from SOC alone.
        #
        # Without that evidence, a negative soc_used_pct is left as a
        # genuine net gain rather than hidden — _efficiency_block already
        # returns all-None below when energy is zero or negative, so the
        # (meaningless) efficiency figures blank themselves out on their
        # own; the raw SOC/energy delta stays visible rather than the whole
        # block vanishing along with a misleading "charged" flag.
        if soc_delta < 0 and _overlaps(last_charge, start.ts, end.ts):
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
        "refuel_detected": False,
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

        if distance_km < MIN_EFFICIENCY_TRIP_KM:
            # Too short for the ratio to mean anything (see the constant).
            # Distance, SOC used and energy stay; so do the *_soc/_counter
            # figures above, for anyone who wants them anyway.
            trip["short_trip"] = True
            for key in (
                "efficiency_km_per_kWh",
                "efficiency_mi_per_kWh",
                "consumption_kWh_per_100km",
                "consumption_kWh_per_100mi",
            ):
                trip[key] = None

    # ── Fuel (ICE/HEV/PHEV) ──────────────────────────────────────────────────
    if is_combustion and start.fuel_pct is not None and end.fuel_pct is not None:
        fuel_used = round(start.fuel_pct - end.fuel_pct, 1)
        # A rise means the level went UP across the trip. How far up decides
        # what it was (#354): @HarryFlatter refuelled ~100 yards into a drive,
        # and because his car keeps one trip open across a short stop, the
        # whole refuel landed inside the trip -- producing "fuel used: -48%"
        # for 4.35 miles of driving. Technically correct, and useless: the
        # figure is dominated by the refuel, not by anything he burned.
        #
        # There is no refuel-session tracking to appeal to, unlike the SOC
        # case above which has real charge sessions -- so this is a magnitude
        # judgement, not evidence. Past the threshold the reading cannot be
        # sender noise and the figures are omitted rather than shown wrong,
        # matching how a confirmed charge already suppresses the electric
        # figures. Below it, a small rise IS most likely noise, so the raw
        # number is still reported honestly rather than being flagged as a
        # refuel that probably never happened.
        if -fuel_used >= REFUEL_MIN_RISE_PCT:
            # Named refuel_detected, not refuelled_during_park (its name until
            # 1.2.9-beta6): nothing here examines whether the car was parked.
            # It compares the fuel level at the start of the trip against the
            # end, so on a car that holds one trip open across a short stop
            # the "park" was never part of the test.
            trip["refuel_detected"] = True
        elif fuel_used < 0:
            trip["fuel_used_pct"] = fuel_used
        else:
            trip["fuel_used_pct"] = fuel_used
            if tank_litres:
                # Surfaced so an owner can see which tank size produced these
                # figures without digging through options — tank sizes are
                # market-split for the same model, so "is it using mine?" is a
                # reasonable question to be able to answer (#354).
                trip["fuel_tank_litres"] = tank_litres
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
    car_record: dict[str, Any] | None = None,
    power_record: dict[str, Any] | None = None,
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

    # start_ts/end_ts stay HA's observed window (the trip-overlap check relies
    # on it). Duration -- and so average power -- prefers the car's own charge
    # record when it's demonstrably this session's: on a 30-minute interval a
    # 28-minute charge was logged as 1 h 31 min at 0.63 kW (~2 kW really).
    #
    # The car restarts its record whenever charging restarts, so on its own
    # the final record only covers the last stretch. ``car_record`` is what
    # was followed while the session was open (track_charge_record): the
    # real start, and the pauses to leave out. duration_s is time spent
    # charging -- start to end, less the pauses the car reported.
    duration_s = _duration_seconds(start.ts, end.ts)
    car_window = _car_charge_window(start, end)
    missed_start = False
    rec_start = rec_end = None
    if car_window is not None:
        rec_start, rec_end, paused_s, interruptions, missed_start = (
            _session_car_window(car_window, car_record)
        )
        duration_s = int(rec_end - rec_start - paused_s)
        result["charge_start_ts"] = datetime.fromtimestamp(rec_start, timezone.utc).isoformat()
        result["charge_end_ts"] = datetime.fromtimestamp(rec_end, timezone.utc).isoformat()
        if interruptions:
            result["interruptions"] = interruptions
            result["paused_s"] = int(round(paused_s))
    if duration_s is not None:
        result["duration_s"] = duration_s
        if car_window is None:
            result["duration_source"] = "polls"
        else:
            # car_partial: an earlier stretch began and ended between two
            # polls, so the start shown is later than the real one.
            result["duration_source"] = "car_partial" if missed_start else "car"

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
    # Range added by the charge (#262, @HarryFlatter). The API's own
    # chrgngAddedElecRng is a live during-session counter that resets, and on
    # the cars seen so far it never leaves 0 even mid-charge — so the useful
    # figure is the difference between the range at each boundary, from the
    # field that demonstrably does work.
    if start.range_km is not None and end.range_km is not None:
        range_added = round(end.range_km - start.range_km, 1)
        result["range_start_km"] = start.range_km
        result["range_end_km"] = end.range_km
        if range_added >= 0:
            result["range_added_km"] = range_added

    if start.odometer_km is not None:
        result["odometer_km"] = start.odometer_km
    # A third figure, alongside the two above: measured from the power
    # readings taken while the charge ran. The headline figure is unchanged.
    measured = measured_charge_energy(
        power_record,
        charge_start=None if missed_start else rec_start,
        charge_end=rec_end,
    )
    if measured:
        result.update(measured)
    # No average when the real start was never seen: the duration is then
    # only a lower bound, and the power worked out from it would be too high.
    if duration_s and duration_s > 0 and energy and not missed_start:
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
        # The latest reading of the open trip taken with no charging cable
        # in. If charging has begun by the time the trip is closed, this is
        # where the battery was before it did -- see close().
        self.open_trip_unplugged: TripSnapshot | None = None
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
        # The previous parked SOC/odometer reading. Used to tell a charge from
        # regen: both raise SOC, but only regen moves the odometer (#354).
        self.last_parked_soc_reading: dict[str, Any] | None = None
        # Last snapshot seen while plugged in but NOT yet charging. Used as the
        # charge baseline when a session opens, so energy delivered before the
        # first "charging" poll is not lost (see note_charge_state).
        self.pre_charge_snapshot: "ChargeSnapshot | None" = None
        # Charging-session tracking (#262): the snapshot taken when a charge
        # started, and the last completed charge. Powers the Last Charge Energy
        # sensor — the API has no "energy added by that charge" field.
        self.open_charge: ChargeSnapshot | None = None
        # The car's own charge record as followed across restarts while the
        # session is open -- see track_charge_record.
        self.open_charge_record: dict[str, Any] | None = None
        # The first reading that saw an open charge stopped but not finished
        # (see CHARGE_PAUSE_MAX_SECONDS). None while charging.
        self.charge_paused_snapshot: ChargeSnapshot | None = None
        # How long the open charge has spent charging so far, as of the
        # latest reading -- see charge_session_progress. None when no charge
        # is open.
        self.charge_progress: dict[str, Any] | None = None
        # The open charge's power readings, added up -- see track_charge_power.
        self.open_charge_power: dict[str, Any] | None = None
        self.last_charge: dict[str, Any] | None = None
        # Phantom counter-reset guard state (#262) -- see
        # logic.SinceChargeCounterGuard. Persisted so held figures survive a
        # restart instead of dropping back to the raw post-reset values.
        self.counter_reset_guard: dict[str, Any] | None = None
        # Last Powered On / Last Powered Off / Last Vehicle Activity as ISO
        # strings, so the real times survive a Home Assistant restart. (They
        # used to be restored from entity states looked up by an entity ID the
        # sensors never had, so every restart replaced them with "24 hours
        # ago" -- #262, @HarryFlatter, 30 Sept.) Keys: last_powered_on,
        # last_powered_off, last_vehicle_activity.
        self.activity_times: dict[str, str] | None = None

    async def async_load(self) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(
            self._hass, STORAGE_VERSION, f"mg_saic_trips_{self._entry_id}_{self._vin}"
        )
        data = await self._store.async_load() or {}
        self.open_snapshot = TripSnapshot.from_dict(data.get("open_snapshot"))
        self.open_trip_unplugged = (
            TripSnapshot.from_dict(data.get("open_trip_unplugged"))
            if self.open_snapshot
            else None
        )
        self.last_trip = data.get("last_trip")
        self.since_charge_baseline = data.get("since_charge_baseline")
        self.last_parked_snapshot = TripSnapshot.from_dict(
            data.get("last_parked_snapshot")
        )
        self.soc_reset_baseline = data.get("soc_reset_baseline")
        self.last_parked_soc_reading = data.get("last_parked_soc_reading")
        self.open_charge = ChargeSnapshot.from_dict(data.get("open_charge"))
        self.open_charge_record = (
            data.get("open_charge_record") if self.open_charge else None
        )
        self.charge_paused_snapshot = (
            ChargeSnapshot.from_dict(data.get("charge_paused_snapshot"))
            if self.open_charge
            else None
        )
        self.pre_charge_snapshot = ChargeSnapshot.from_dict(
            data.get("pre_charge_snapshot")
        )
        self.charge_progress = (
            data.get("charge_progress") if self.open_charge else None
        )
        self.open_charge_power = (
            data.get("open_charge_power") if self.open_charge else None
        )
        self.last_charge = data.get("last_charge")
        self.counter_reset_guard = data.get("counter_reset_guard")
        self.activity_times = data.get("activity_times")

    async def async_save(self) -> None:
        """Persist current open/last-trip state and the since-charge baseline."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "open_snapshot": (
                    self.open_snapshot.to_dict() if self.open_snapshot else None
                ),
                "open_trip_unplugged": (
                    self.open_trip_unplugged.to_dict()
                    if self.open_snapshot and self.open_trip_unplugged
                    else None
                ),
                "last_trip": self.last_trip,
                "since_charge_baseline": self.since_charge_baseline,
                "last_parked_snapshot": (
                    self.last_parked_snapshot.to_dict()
                    if self.last_parked_snapshot
                    else None
                ),
                "soc_reset_baseline": self.soc_reset_baseline,
                "last_parked_soc_reading": self.last_parked_soc_reading,
                "open_charge": (
                    self.open_charge.to_dict() if self.open_charge else None
                ),
                "open_charge_record": self.open_charge_record,
                "charge_paused_snapshot": (
                    self.charge_paused_snapshot.to_dict()
                    if self.charge_paused_snapshot
                    else None
                ),
                "pre_charge_snapshot": (
                    self.pre_charge_snapshot.to_dict()
                    if self.pre_charge_snapshot
                    else None
                ),
                "charge_progress": (
                    self.charge_progress if self.open_charge else None
                ),
                "open_charge_power": (
                    self.open_charge_power if self.open_charge else None
                ),
                "last_charge": self.last_charge,
                "counter_reset_guard": self.counter_reset_guard,
                "activity_times": self.activity_times,
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

    def note_soc_reset_baseline(self, soc_pct, odometer_km, ts, is_charging=None) -> bool:
        """Track SOC while parked to detect a charge and rebase the SOC-based
        "since reset" baseline — the odometer/SOC-only counterpart to
        note_since_charge, entirely independent of the since-charge counter
        fields (#301: those are unreliable on some cars, absent on others).

        ``is_charging`` is True/False where the car reports it, or None where
        charging data is unavailable — this sensor exists precisely to keep
        working on cars whose charging endpoint is unreliable, so it must
        never *depend* on that signal, only prefer it when present.

        Being called only while parked is not on its own enough to rule out
        regen. The car is parked at the END of a downhill leg too, and on a
        descent big enough for regen to outweigh consumption the first parked
        reading after that leg is HIGHER than the one before it — which is
        indistinguishable from a charge if SOC is all you look at. That is
        exactly what @SteveMSJ hit (#354): 80.0% at home, 3 miles downhill to
        a wood, 80.6% on arrival, and the outbound leg silently dropped from
        the figures because arriving looked like plugging in.

        The discriminator is the odometer. Charging happens standing still;
        regen needs movement. So a SOC rise only counts as a charge if the car
        hasn't moved since the previous parked reading.

        Returns True if any tracked state changed (caller may persist).
        """
        if soc_pct is None or odometer_km is None:
            return False

        previous = self.last_parked_soc_reading
        self.last_parked_soc_reading = {
            "soc_pct": round(soc_pct, 1),
            "odometer_km": round(odometer_km, 3),
        }

        if self.soc_reset_baseline is None:
            self.soc_reset_baseline = self._new_soc_baseline(soc_pct, odometer_km, ts)
            return True

        # Rebase on a rise above the LOWEST SOC seen since the baseline was
        # set, not above the baseline itself. The old rule kept a running
        # maximum, so a charge that stopped below a previous peak never
        # rebased: an 80% baseline, 192 km of driving, then a charge back to
        # 79.3% left the odometer baseline 192 km stale while SOC looked
        # almost untouched — 0.7% "used" over 192 km, and an efficiency figure
        # two orders of magnitude too high.
        #
        # Measuring from the low point also catches a slow trickle charge,
        # where no single poll rises far enough to trip the threshold on its
        # own but the total gain does.
        # The car says it is charging — no inference needed.
        if is_charging:
            self.soc_reset_baseline = self._new_soc_baseline(soc_pct, odometer_km, ts)
            return True

        low = self.soc_reset_baseline.get(
            "soc_low_pct", self.soc_reset_baseline.get("soc_pct", soc_pct)
        )
        if soc_pct >= low + SOC_CHARGE_RISE_PCT:
            # A rise above the low-water mark, but from what? Two conditions
            # must BOTH hold for this to be a charge: the car hasn't moved
            # since the last parked reading (charging happens standing
            # still), AND SOC is higher than that SAME reading — not merely
            # above the low-water mark in general.
            #
            # The second condition is not optional. Without it, a LATER poll
            # sitting at an already-explained regen value looks identical to
            # a fresh charge signal: hasn't moved, still above the low. That
            # is exactly what broke the first version of this fix in the
            # field (#354, confirmed on 1.2.9-beta1 by @SteveMSJ): arrive at
            # a spot via regen, correctly hold; a SECOND poll at the same
            # spot, unmoved, sees "hasn't moved AND above the low" all over
            # again and rebases onto its own earlier "this was regen"
            # conclusion, discarding the outbound leg exactly as before.
            # Comparing against the immediate previous reading rather than
            # the low-water mark closes that gap: sitting still at an
            # unchanged SOC is "nothing new happened", not fresh evidence.
            if (
                previous is not None
                and abs(odometer_km - previous["odometer_km"]) < REGEN_ODOMETER_MOVED_KM
                and soc_pct > previous["soc_pct"]
            ):
                self.soc_reset_baseline = self._new_soc_baseline(soc_pct, odometer_km, ts)
            return True  # held (regen, or nothing new) — or rebased, above

        # Still discharging: track the new low so the next charge is measured
        # from the bottom of this cycle.
        if soc_pct < low:
            self.soc_reset_baseline["soc_low_pct"] = round(soc_pct, 1)
            return True
        return True

    @staticmethod
    def _new_soc_baseline(soc_pct, odometer_km, ts) -> dict[str, Any]:
        return {
            "soc_pct": round(soc_pct, 1),
            "odometer_km": round(odometer_km, 3),
            "ts": ts,
            "soc_low_pct": round(soc_pct, 1),
        }

    def _note_charge_progress(self, snapshot, charging, stretch_s) -> None:
        """Refresh the open charge's running total.

        Not reported as a state change: it moves on every reading, and is
        stored whenever something else is (a restart, a pause, the charge
        opening). After a Home Assistant restart the stored figure stands,
        with its ``as_of``, until the first reading of the car replaces it.
        """
        progress = charge_session_progress(
            self.open_charge,
            snapshot,
            getattr(self, "open_charge_record", None),
            charging=charging,
            stretch_s=stretch_s,
        )
        if progress is not None:
            self.charge_progress = progress

    def charge_session(self) -> dict[str, Any] | None:
        """The charge in progress, or failing that the last one finished.

        While a charge is open: its running total (``in_progress`` True). It
        moves at each reading of the car. Otherwise: the last completed
        charge, so the figure a charge ends on stays put until the next one
        starts. None before any charge has been seen, and for the moment
        between a charge opening after a restart and its first reading.
        """
        if getattr(self, "open_charge", None) is not None:
            progress = getattr(self, "charge_progress", None)
            return {**progress, "in_progress": True} if progress else None
        charge = getattr(self, "last_charge", None)
        if not charge or charge.get("duration_s") is None:
            return None
        session = {
            "duration_s": charge["duration_s"],
            "interruptions": int(charge.get("interruptions") or 0),
            "paused_s": int(charge.get("paused_s") or 0),
            "in_progress": False,
        }
        for key in ("duration_source", "charge_start_ts", "charge_end_ts"):
            if charge.get(key) is not None:
                session[key] = charge[key]
        return session

    def note_charge_state(
        self,
        is_charging: bool,
        snapshot: "ChargeSnapshot | None",
        *,
        capacity_kwh: float | None,
        now_iso: str,
        is_plugged_in: bool = False,
        charge_paused: bool = False,
        stretch_s: float | None = None,
        power_kw: float | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        """Open/close a charging session (#262).

        ``charge_paused``: the car is not charging, but it is still plugged in
        and has not said the charge is finished. An open session is then kept
        open rather than closed, so a charge that pauses and resumes is one
        charge even when a poll lands in the pause (see
        CHARGE_PAUSE_MAX_SECONDS). Without it a poll in a half-minute pause
        ended the session, and Last Charge reported only what came after --
        energy included.

        ``stretch_s``: the car's own Charging Duration counter, in seconds.
        Only used for the running total of an open charge
        (``charge_progress``, see charge_session_progress).

        ``power_kw``: the pack's charging power at this reading, for the
        measured energy figure (see track_charge_power).

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
                # Prefer a snapshot taken while plugged in but not yet
                # charging. Charging routinely starts between polls -- on a
                # scheduled/off-peak charge the car can be plugged in for
                # hours first, and the poll interval only drops to the
                # charging cadence once we have SEEN it charging. James's
                # MGS6: plugged in at 16:43 at 68.9%, first charging poll at
                # 18:51 already reading 72.5%. Baselining on that first
                # charging poll silently discarded ~2.7 kWh, and Last Charge
                # Energy reported 4.83 kWh against the charger's 9.1 kWh.
                #
                # Only used when SOC has not dropped since, so a car that sat
                # plugged in losing charge to vampire drain (or one where the
                # pre-charge reading is simply stale) falls back to the
                # charging snapshot rather than inflating the figure.
                baseline = snapshot
                pre = self.pre_charge_snapshot
                if (
                    pre is not None
                    and pre.soc_pct is not None
                    and snapshot.soc_pct is not None
                    and pre.soc_pct <= snapshot.soc_pct
                ):
                    age = _duration_seconds(pre.ts, now_iso)
                    if age is not None and age <= MAX_OPEN_CHARGE_SECONDS:
                        baseline = pre
                self.open_charge = baseline
                self.pre_charge_snapshot = None
                self.charge_paused_snapshot = None
                # Start following the car's own record. The baseline's
                # record end is only "the end before charging" when the
                # baseline is the earlier, not-yet-charging reading.
                self.open_charge_record, _ = track_charge_record(
                    None,
                    snapshot,
                    previous_end=(
                        baseline.record_end if baseline is not snapshot else None
                    ),
                )
                self._note_charge_progress(snapshot, True, stretch_s)
                self.open_charge_power = track_charge_power(
                    None, snapshot.ts, power_kw, charging=True
                )
                return None, True
            # Already charging. The start snapshot stands; keep following the
            # car's record, which restarts whenever charging restarts.
            self.open_charge_record, changed = track_charge_record(
                self.open_charge_record, snapshot
            )
            if getattr(self, "charge_paused_snapshot", None) is not None:
                # It was paused and has resumed. The pause itself is in the
                # car's record, which track_charge_record has just read.
                self.charge_paused_snapshot = None
                changed = True
            self._note_charge_progress(snapshot, True, stretch_s)
            self.open_charge_power = track_charge_power(
                getattr(self, "open_charge_power", None),
                snapshot.ts,
                power_kw,
                charging=True,
            )
            return None, changed

        # Not charging. Remember this as the pre-charge baseline while the car
        # is plugged in, so a session opening on a later poll can reach back
        # to it. Cleared when unplugged so a snapshot from a previous session
        # can never leak into the next one.
        if self.open_charge is None:
            self.pre_charge_snapshot = snapshot if is_plugged_in else None
            self.charge_paused_snapshot = None
            self.charge_progress = None
            self.open_charge_power = None
            return None, False

        # A charge is open and the car is not charging. Paused, or over?
        current = snapshot
        end = snapshot
        paused = getattr(self, "charge_paused_snapshot", None)
        if charge_paused:
            if paused is None:
                self.charge_paused_snapshot = snapshot
                self._note_charge_progress(snapshot, False, None)
                self.open_charge_power = track_charge_power(
                    getattr(self, "open_charge_power", None), snapshot.ts, power_kw
                )
                return None, True
            waited = _duration_seconds(paused.ts, now_iso)
            if waited is not None and waited < CHARGE_PAUSE_MAX_SECONDS:
                self._note_charge_progress(snapshot, False, None)
                self.open_charge_power = track_charge_power(
                    getattr(self, "open_charge_power", None), snapshot.ts, power_kw
                )
                return None, False
            # It never resumed: the charge ended when it stopped.
            end = paused
        elif (
            paused is not None
            and paused.soc_pct is not None
            and snapshot.soc_pct is not None
            and snapshot.soc_pct < paused.soc_pct
        ):
            # Stopped, then unplugged (and perhaps driven) before this
            # reading: the reading taken when it stopped is the charge's end.
            end = paused
        snapshot = end

        # The latest reading, not the one the charge is closed against, is
        # what a following charge would start from.
        self.pre_charge_snapshot = current if is_plugged_in else None
        self.charge_paused_snapshot = None
        start = self.open_charge
        car_record = self.open_charge_record
        power_record = getattr(self, "open_charge_power", None)
        self.open_charge = None
        self.open_charge_record = None
        self.charge_progress = None
        self.open_charge_power = None

        age = _duration_seconds(start.ts, now_iso)
        if age is not None and age > MAX_OPEN_CHARGE_SECONDS:
            # A charge-stop we never saw. Abandon rather than invent a figure.
            return None, True

        charge = compute_charge_session(
            start,
            snapshot,
            capacity_kwh=capacity_kwh,
            car_record=car_record,
            power_record=power_record,
        )
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
        self.open_trip_unplugged = None
        return True

    def note_trip_reading(self, snapshot: TripSnapshot) -> None:
        """Remember a reading of the open trip taken with no cable in.

        Not a state change worth a save on its own: it moves on every poll of
        a drive and is stored whenever something else is.
        """
        if self.open_snapshot is not None and snapshot is not None:
            self.open_trip_unplugged = snapshot

    def close(
        self,
        snapshot: TripSnapshot,
        *,
        capacity_kwh: float | None,
        tank_litres: float | None,
        is_electric: bool,
        is_combustion: bool,
        charging: bool = False,
        at_plug_in: bool = False,
    ) -> dict[str, Any] | None:
        """Close the open trip against ``snapshot`` and return the trip dict
        (synchronous). Returns None (and clears state) if there was no open trip
        or the pair didn't form a plausible trip. Callers persist afterwards.

        ``charging``: the car is already charging at this reading. Some cars
        stay "on" for minutes after parking, so the cable can go in, and the
        battery start to fill, before the power-off that closes the trip is
        seen (#407, @hoffeck's MG4: charging from 15:41:43, power-off seen at
        15:43:52). The battery level at the close is then higher than it was
        when the driving stopped -- on a DC charger higher than at the start
        of the trip -- and the energy used came out at nothing or less, so
        the trip had no efficiency. When charging, the level is taken from
        the last reading of this trip that had no cable in, if that is lower.
        The distance still comes from this reading: the car has not moved.

        ``at_plug_in``: the trip is being closed because the cable went in
        while the car was still on, rather than at power-off.
        """
        start = self.open_snapshot
        before_charge = getattr(self, "open_trip_unplugged", None) or start
        self.open_snapshot = None
        self.open_trip_unplugged = None
        if start is None:
            return None

        end = snapshot
        soc_before_charge = False
        if (
            charging
            and snapshot.soc_pct is not None
            and before_charge.soc_pct is not None
            and before_charge.soc_pct < snapshot.soc_pct
        ):
            end = replace(snapshot, soc_pct=before_charge.soc_pct)
            soc_before_charge = True

        trip = compute_completed_trip(
            start,
            end,
            baseline=self.since_charge_baseline,
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
            last_charge=self.last_charge,
        )
        if trip is not None:
            if at_plug_in:
                trip["closed_at_plug_in"] = True
            if soc_before_charge:
                trip["end_soc_before_charging"] = True
        # The real reading, not the adjusted one, is what the next trip and
        # the parked baseline start from.
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
            last_charge=self.last_charge,
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
        self.open_trip_unplugged = None
        end = snapshot if snapshot is not None else start
        trip = compute_completed_trip(
            start,
            end,
            baseline=None,
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
            last_charge=self.last_charge,
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
