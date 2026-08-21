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

# Reject an odometer delta larger than this (km) as a single trip — protects
# against odometer rollover, the uint16 saturation sentinel slipping through,
# or a garbage reading. A genuine single drive won't exceed this.
MAX_PLAUSIBLE_TRIP_KM = 2000.0

# Minimum odometer movement (km) for a retrospective (never-seen-live) trip to
# be recorded, so odometer rounding / parking shuffles aren't logged as trips.
MIN_RETRO_TRIP_KM = 1.0

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

    Returns ``None`` when no plausible distance can be established. Individual
    electric/fuel figures are ``None`` when their inputs are missing.
    """
    if start is None or end is None:
        return None

    base_km = baseline.get("since_charge_km") if baseline else None
    base_kwh = baseline.get("since_charge_kwh") if baseline else None

    # ── Distance: prefer the since-charge counter, else the odometer delta ────
    # Retrospective trips always use the odometer (the counter may have reset in
    # the unobserved gap).
    distance_km = None if retrospective else _counter_delta(end.since_charge_km, base_km)
    if distance_km is None:
        distance_km = round(end.odometer_km - start.odometer_km, 2)
    if distance_km <= 0 or distance_km > MAX_PLAUSIBLE_TRIP_KM:
        return None

    distance_mi = distance_km / KM_PER_MILE
    trip: dict[str, Any] = {
        "distance_km": round(distance_km, 2),
        "distance_mi": round(distance_mi, 2),
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

    # ── Electric energy (BEV/PHEV) ───────────────────────────────────────────
    if is_electric:
        # Retrospective trips skip the counter (it may have reset in the gap) and
        # use the SOC change only.
        energy = None if retrospective else _counter_delta(end.since_charge_kwh, base_kwh)
        if energy is None and start.soc_pct is not None and end.soc_pct is not None:
            # Derive from SOC change (coarse; the only source for retrospective
            # trips, and the fallback when no counter is available).
            soc_used = round(start.soc_pct - end.soc_pct, 1)
            if soc_used < 0:
                trip["charged_during_park"] = True
            else:
                trip["soc_used_pct"] = soc_used
                if capacity_kwh:
                    energy = round(soc_used / 100.0 * capacity_kwh, 3)
        if energy is not None and energy > 0:
            trip["energy_kWh"] = round(energy, 3)
            trip["efficiency_km_per_kWh"] = round(distance_km / energy, 2)
            trip["efficiency_mi_per_kWh"] = round(distance_mi / energy, 2)
            trip["consumption_kWh_per_100km"] = round(energy / distance_km * 100.0, 2)
            trip["consumption_kWh_per_100mi"] = round(energy / distance_mi * 100.0, 2)

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


# ── Persistent manager ───────────────────────────────────────────────────────

# HA imports are done lazily inside methods so the pure functions above can be
# imported and unit-tested without Home Assistant installed.

STORAGE_VERSION = 1
EVENT_TRIP_COMPLETED = "mg_saic_trip_completed"


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
