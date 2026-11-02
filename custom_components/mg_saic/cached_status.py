# File: cached_status.py
"""SAIC's stored copy of a car's last status -- a read that should not wake it.

Every poll this integration makes asks the car itself for a live reading
(``/vehicle/status`` and ``/vehicle/charging/mgmtData`` are both "ask the car,
then collect the answer" calls), so the car has to wake up to reply. On an
MGS6 with a Bluetooth monitor on its 12 V battery, 13 idle polls in a night
were 13 dips of 0.3-0.5 V, each lasting a few minutes, and nothing in
between (6 Oct 2026); an MG4 owner saw the same hourly (#407, @hoffeck).

The iSmart app makes one more kind of request. A capture of the app opening
(3 Oct 2026) shows it call ``/vehicle/status/cache`` once, before any live
request, and get an answer back in about 0.2 s -- far too fast to have gone
to the car. The reply is a cut-down status with the time it was taken and an
``onlineStatus`` flag:

    basicVehicleStatus   lock, bonnet, boot, three windows, tyre pressures,
                         battery % (extendedData1), electric range, odometer,
                         engine status, remote climate, rear screen heater
                         -- about 20 fields; no 12 V voltage, power mode,
                         doors or temperatures
    gpsPosition          position only
    statusTime           when SAIC last heard this from the car
    onlineStatus         1 in the capture (the car was awake)

It leaves the car asleep (no dip on the 12 V monitor at any read), and the
car updates the copy by itself when plugged in, when a charge starts, when
switched off on arrival and every few hours while parked. Quiet Charging
reads it overnight instead of polling the car; the ``read_cached_status``
action shows it on demand.

The request itself is in mg-saic-client (``get_vehicle_status_cached``,
0.9.7+; it was made here up to 1.4.0-beta22). The library's reply,
``VehicleStatusCacheResp``, has the same four fields as
``CachedVehicleStatus`` below and keeps every field as SAIC sent it.

No Home Assistant imports: used by the coordinator and services.py, and
testable directly.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass
class CachedVehicleStatus:
    """The reply's shape: the same fields as mg-saic-client's
    VehicleStatusCacheResp, kept loose so every field SAIC sends is kept.
    Used where a reply is built here (the tests)."""

    basicVehicleStatus: Optional[Dict[str, Any]] = None
    gpsPosition: Optional[Dict[str, Any]] = None
    statusTime: Optional[int] = None
    onlineStatus: Optional[int] = None


def summarise_cached_status(reply, now=None) -> dict:
    """What the action returns: the stored status, without the position.

    ``status_time`` / ``age_seconds`` say how old SAIC's copy is.
    ``fields`` is the stored basicVehicleStatus as sent. The position is left
    out (``has_position`` says whether one was there): it is not needed to
    answer the questions above and has no business in a pasted result.
    """
    now = now or datetime.now(timezone.utc)
    status_time = getattr(reply, "statusTime", None)
    result: dict = {
        "online_status": getattr(reply, "onlineStatus", None),
        "status_time": None,
        "age_seconds": None,
        "has_position": bool(getattr(reply, "gpsPosition", None)),
        "fields": dict(getattr(reply, "basicVehicleStatus", None) or {}),
    }
    if isinstance(status_time, (int, float)) and not isinstance(status_time, bool) and status_time > 0:
        taken = datetime.fromtimestamp(status_time, timezone.utc)
        result["status_time"] = taken.isoformat()
        result["age_seconds"] = int((now - taken).total_seconds())
    return result
