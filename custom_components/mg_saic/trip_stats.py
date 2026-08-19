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

# Efficiency ratio helpers.
KM_PER_MILE = 1.609344


@dataclass
class TripSnapshot:
    """A single odometer/SOC/fuel reading taken at a trip boundary."""

    ts: str  # ISO-8601 timestamp string (storage-friendly)
    odometer_km: float
    soc_pct: float | None = None
    fuel_pct: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "TripSnapshot | None":
        if not d:
            return None
        try:
            return cls(
                ts=d["ts"],
                odometer_km=float(d["odometer_km"]),
                soc_pct=None if d.get("soc_pct") is None else float(d["soc_pct"]),
                fuel_pct=None if d.get("fuel_pct") is None else float(d["fuel_pct"]),
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


def compute_completed_trip(
    start: TripSnapshot,
    end: TripSnapshot,
    *,
    capacity_kwh: float | None,
    tank_litres: float | None,
    is_electric: bool,
    is_combustion: bool,
) -> dict[str, Any] | None:
    """Compute a completed-trip dict from two boundary snapshots.

    Returns ``None`` when the pair can't form a plausible trip (no movement,
    implausibly large delta, or missing odometer). Individual electric/fuel
    figures are set to ``None`` (not the whole trip) when their inputs are
    missing or inconsistent (e.g. charged mid-park), so a valid distance-only
    trip is still emitted.
    """
    if start is None or end is None:
        return None

    distance_km = round(end.odometer_km - start.odometer_km, 2)
    # No movement, went backwards, or an implausible jump -> not a real trip.
    if distance_km <= 0 or distance_km > MAX_PLAUSIBLE_TRIP_KM:
        return None

    trip: dict[str, Any] = {
        "distance_km": distance_km,
        "distance_mi": round(distance_km / KM_PER_MILE, 2),
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

    # ── Electric portion (BEV/PHEV) ──────────────────────────────────────────
    if is_electric and start.soc_pct is not None and end.soc_pct is not None:
        soc_used = round(start.soc_pct - end.soc_pct, 1)
        if soc_used < 0:
            # SOC rose while parked/driving => charged in between. The delta no
            # longer reflects consumption, so we don't report electric energy.
            trip["charged_during_park"] = True
        else:
            trip["soc_used_pct"] = soc_used
            if capacity_kwh:
                energy = round(soc_used / 100.0 * capacity_kwh, 3)
                trip["energy_kWh"] = energy
                if energy > 0:
                    distance_mi = distance_km / KM_PER_MILE
                    trip["efficiency_km_per_kWh"] = round(distance_km / energy, 2)
                    trip["efficiency_mi_per_kWh"] = round(distance_mi / energy, 2)
                    trip["consumption_kWh_per_100km"] = round(
                        energy / distance_km * 100.0, 2
                    )
                    trip["consumption_kWh_per_100mi"] = round(
                        energy / distance_mi * 100.0, 2
                    )

    # ── Fuel portion (ICE/HEV/PHEV) ──────────────────────────────────────────
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

    async def async_load(self) -> None:
        from homeassistant.helpers.storage import Store

        self._store = Store(
            self._hass, STORAGE_VERSION, f"mg_saic_trips_{self._entry_id}_{self._vin}"
        )
        data = await self._store.async_load() or {}
        self.open_snapshot = TripSnapshot.from_dict(data.get("open_snapshot"))
        self.last_trip = data.get("last_trip")

    async def async_save(self) -> None:
        """Persist current open/last-trip state."""
        if self._store is None:
            return
        await self._store.async_save(
            {
                "open_snapshot": (
                    self.open_snapshot.to_dict() if self.open_snapshot else None
                ),
                "last_trip": self.last_trip,
            }
        )

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
            capacity_kwh=capacity_kwh,
            tank_litres=tank_litres,
            is_electric=is_electric,
            is_combustion=is_combustion,
        )
        if trip is not None:
            self.last_trip = trip
            self._fire_event(trip)
        return trip

    def _fire_event(self, trip: dict[str, Any]) -> None:
        try:
            self._hass.bus.async_fire(
                EVENT_TRIP_COMPLETED, {"vin": self._vin, **trip}
            )
        except Exception:  # noqa: BLE001 - event firing must never break a poll
            pass
