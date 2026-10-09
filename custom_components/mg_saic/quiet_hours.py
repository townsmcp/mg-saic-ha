# File: quiet_hours.py
"""Quiet hours: no live polls overnight, only reads of SAIC's stored status.

Every poll asks the car itself for a live reading, so the car wakes to answer.
On an MGS6 that shows as a dip on a monitor fitted to the 12 V battery at
every poll; on an MG HS PHEV charging on a myenergi zappi, owners see the
headlights and brake lights come on at every poll while it charges (#269).

SAIC also keeps a copy of the car's last status (see cached_status.py), and
reading that does not contact the car. Tested on an MGS6, 6-7 Oct 2026:

- 28 reads, no 12 V dip at any of them.
- The car updates the copy by itself when it is plugged in and when a charge
  starts (to the second), when it is switched off after a drive, and about
  every 4 hours while parked. Not during a charge, and not for an unlock.
- The copy carries a "charging now" flag, ``extendedData2``: 1 while
  charging, 0 otherwise. In @HarryFlatter's HS PHEV logs the same field in
  the live status is 1 exactly while the car is charging.
- 3 reads in 28 came back with nonsense values, so only the time and the
  charging flag are used.

So, between a start and an end time the owner sets, while the Live Polling
switch is off and the car is switched off:

- each scheduled poll becomes a read of the stored copy instead;
- when that read shows a charge has started since the last live poll, one
  live poll records it (and, on an HS, flashes the lights once);
- one live poll when the charge should have finished, from the car's own
  "time remaining" or the battery level, target and power. Owners report the
  lights only flash while charging, so by then it normally will not. If the
  car has not finished (a smart tariff pausing it), nothing more is asked
  until the charging flag shows it has started again;
- at the end time Live Polling comes back on, with one live poll to catch up.

A car switched on during quiet hours is polled as normal, and a refresh the
owner asks for (Update Vehicle Data) always reaches the car.

No Home Assistant imports: the coordinator does the scheduling, and this is
testable directly.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any, Optional

CONF_QUIET_HOURS = "quiet_hours"
CONF_QUIET_HOURS_START = "quiet_hours_start"
CONF_QUIET_HOURS_END = "quiet_hours_end"
# The Live Polling switch's state, and when it was last set (ISO, UTC), so it
# survives a restart.
CONF_QUIET_HOURS_LIVE_POLLING = "quiet_hours_live_polling"
CONF_QUIET_HOURS_LIVE_POLLING_AT = "quiet_hours_live_polling_at"

DEFAULT_QUIET_HOURS_START = time(hour=22, minute=0)
DEFAULT_QUIET_HOURS_END = time(hour=7, minute=0)

# Added to the expected finish so the check lands after the charge, not on
# its last minutes (when an HS would still be charging, and flashing).
FINISH_CHECK_MARGIN = timedelta(minutes=15)
# Never sooner than this after the poll that worked it out.
FINISH_CHECK_MIN = timedelta(minutes=15)
# When nothing says how long the charge will take.
FINISH_CHECK_FALLBACK = timedelta(hours=2)
# Below this the "power" is a car waiting, not charging, and gives no estimate.
MIN_CHARGING_POWER_KW = 0.3


def parse_hhmm(value: Any, default: time) -> time:
    """A stored "HH:MM" (or "HH:MM:SS") as a time; ``default`` if unusable."""
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if not isinstance(value, str):
        return default
    parts = value.strip().split(":")
    if len(parts) not in (2, 3):
        return default
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return time(hour=hour, minute=minute)


def format_hhmm(value: time) -> str:
    """The form a time is stored in."""
    return f"{value.hour:02d}:{value.minute:02d}"


def in_quiet_window(now: time, start: time, end: time) -> bool:
    """Whether ``now`` (local time of day) falls between start and end.

    The window usually crosses midnight (22:00 to 07:00). It includes the
    start and excludes the end. Equal start and end means no window at all.
    """
    now = now.replace(second=0, microsecond=0, tzinfo=None)
    start = start.replace(tzinfo=None)
    end = end.replace(tzinfo=None)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end


def last_scheduled_flip(
    now: datetime, start: time, end: time
) -> Optional[tuple[datetime, bool]]:
    """The most recent start or end time at or before ``now``, and its state.

    ``now`` is local and timezone-aware. The state is what that time sets
    Live Polling to: off at the start, on at the end. None with no window.
    """
    if start == end:
        return None
    flips = []
    for days_back in (0, 1):
        day = (now - timedelta(days=days_back)).date()
        for at, state in ((start, False), (end, True)):
            moment = datetime.combine(day, at.replace(tzinfo=None), tzinfo=now.tzinfo)
            if moment <= now:
                flips.append((moment, state))
    return max(flips, key=lambda flip: flip[0]) if flips else None


def live_polling_after_restart(
    saved_on: Any,
    saved_at: Optional[datetime],
    now: datetime,
    start: time,
    end: time,
) -> bool:
    """Live Polling after a restart: as it was left, unless a flip was missed.

    ``saved_on`` / ``saved_at``: the switch's state and when it was set, as
    saved. ``now``: local, timezone-aware. Switched on by hand at 23:30 and
    Home Assistant restarting at 01:00, it stays on. Restarting after the end
    time has passed since, it takes the end time's state; with nothing saved,
    whatever the clock says.
    """
    from_clock = not in_quiet_window(now.time(), start, end)
    if not isinstance(saved_on, bool) or saved_at is None:
        return from_clock
    if saved_at.tzinfo is None or saved_at > now:
        return from_clock
    flip = last_scheduled_flip(now, start, end)
    if flip is not None and flip[0] > saved_at:
        return flip[1]
    return saved_on


def parse_saved_at(value: Any) -> Optional[datetime]:
    """A saved ISO timestamp as an aware datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def stored_charging_flag(summary: Optional[dict]) -> Optional[int]:
    """The stored copy's "charging now" flag (1/0), or None if absent."""
    fields = (summary or {}).get("fields") or {}
    flag = fields.get("extendedData2")
    if isinstance(flag, bool) or not isinstance(flag, int):
        return None
    return flag


def stored_status_epoch(reply: Any) -> Optional[int]:
    """When SAIC last heard from the car (the reply's statusTime), or None."""
    raw = getattr(reply, "statusTime", None) if reply is not None else None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return int(raw)
    return None


def stored_charge_started(
    flag: Optional[int],
    stored_epoch: Optional[int],
    *,
    known_charging: bool,
    last_live_status_epoch: Optional[int],
) -> bool:
    """Whether the stored copy says a charge has started that we have not seen.

    The flag must be on, the integration must not already think the car is
    charging, and the copy must be newer than the status the last live poll
    returned -- so a flag left over from before that poll can't fire it again.
    """
    if flag != 1 or known_charging or stored_epoch is None:
        return False
    if last_live_status_epoch is None:
        return True
    return stored_epoch > last_live_status_epoch


def remaining_charge_time(
    *,
    remaining_minutes: Any = None,
    remaining_valid_flag: Any = None,
    soc_pct: Optional[float] = None,
    target_pct: Optional[float] = None,
    capacity_kwh: Optional[float] = None,
    power_kw: Optional[float] = None,
) -> Optional[timedelta]:
    """How long the charge in progress should take to finish, or None.

    The car's own figure (``chrgngRmnngTime``, minutes) when it gives one: it
    marks "no value" with 1023 and the validity flag ``chrgngRmnngTimeV`` set
    to 1. Otherwise the battery left to fill at the power going in now.
    """
    valid_flag = remaining_valid_flag in (None, 0)
    if (
        valid_flag
        and isinstance(remaining_minutes, (int, float))
        and not isinstance(remaining_minutes, bool)
        and 0 < remaining_minutes < 1023
    ):
        return timedelta(minutes=float(remaining_minutes))
    numbers = (soc_pct, target_pct, capacity_kwh, power_kw)
    if any(
        not isinstance(v, (int, float)) or isinstance(v, bool) for v in numbers
    ):
        return None
    if power_kw < MIN_CHARGING_POWER_KW or capacity_kwh <= 0:
        return None
    left_pct = target_pct - soc_pct
    if left_pct <= 0:
        return timedelta(0)
    return timedelta(hours=left_pct / 100.0 * capacity_kwh / power_kw)


def finish_check_delay(remaining: Optional[timedelta]) -> timedelta:
    """How long to wait before the "has it finished?" poll."""
    if remaining is None:
        return FINISH_CHECK_FALLBACK
    return max(remaining + FINISH_CHECK_MARGIN, FINISH_CHECK_MIN)
