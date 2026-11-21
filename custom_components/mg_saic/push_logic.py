# File: push_logic.py
"""The rules behind real-time push, kept free of Home Assistant.

Everything here is plain Python so it can be tested without Home Assistant:
which state the push connection is in, how often the message queue should be
polled, whether a push is too old to act on, and how pushes are matched up with
what the message poller finds in the queue.

How the pieces fit
------------------
SAIC sends a push the moment the car starts, and also leaves the same message
in the account's message queue. The message poller reads that queue, so without
care the same start would be handled twice, and a push that never arrived would
go unnoticed. ``PushLedger`` does the matching by counting: every push
start is noted, and when the poller finds N start messages it first uses up N
noted pushes. Any message left over is a start that no push announced, which is
the sign the push connection is connected but not delivering.
"""

from __future__ import annotations

import re
from enum import StrEnum

# While push is working, the queue is only a safety net.
PUSH_POLL_INTERVAL_SECONDS = 30 * 60
# Without push (or when it can't be trusted) the queue is polled as before.
FALLBACK_POLL_INTERVAL_SECONDS = 60

# SAIC keeps undelivered pushes for 28 days, so after downtime old ones arrive
# on reconnect. A push older than this is reported but not acted on.
STALE_PUSH_SECONDS = 10 * 60

# Two start pushes closer together than this are one start delivered twice.
START_DUPLICATE_WINDOW_SECONDS = 90

# A registration that has been in use for a day or more is replaced. A token
# that was days old stopped receiving the car-start alarm while still looking
# healthy, so the integration does not wait to find out.
RENEW_AFTER_SECONDS = 18 * 3600

# When the poller finds a start the push never announced, wait this long before
# deciding the push was missed (the push can arrive just after the queue shows
# the message).
MISSED_PUSH_CONFIRM_SECONDS = 90

# After a missed push, keep polling at the fallback rate for this long unless a
# push start proves delivery is working again.
UNHEALTHY_HOLD_SECONDS = 3 * 3600

# How long a push start is remembered while waiting for the poller to see its
# queue message.
PUSH_START_MEMORY_SECONDS = 2 * 3600

MESSAGE_TYPE_VEHICLE_START = "323"
MESSAGE_TYPE_FINISH_CHARGING = "328"

# Home Assistant event fired for every push received for a car that has push on.
EVENT_PUSH = "mg_saic_push"


class PushStatus(StrEnum):
    """What the Push Status sensor reports."""

    OFF = "off"  # push is not in use for this car
    CONNECTING = "connecting"  # starting up, or waiting to reconnect
    PUSH = "push"  # connected; the message queue is only a slow safety net
    POLLING = "polling"  # push isn't dependable right now; polling every minute


class PushReason(StrEnum):
    """Why push is not in use, for the sensor's attributes."""

    DISABLED = "disabled_in_options"
    UNSUPPORTED = "not_supported_for_this_region"
    NO_LIBRARY = "push_library_missing"
    REGISTRATION_FAILED = "registration_failed"
    DISCONNECTED = "connection_lost"
    MISSED_PUSH = "missed_push"
    LOGIN_FAILED = "login_with_push_token_failed"


def message_poll_interval(status: PushStatus, *, every_car_uses_push: bool) -> int:
    """Seconds between message-queue polls for an account.

    The slow rate needs push to be working AND every car on the account to be
    using it: a car that has push switched off still depends on the poller.
    """
    if status is PushStatus.PUSH and every_car_uses_push:
        return PUSH_POLL_INTERVAL_SECONDS
    return FALLBACK_POLL_INTERVAL_SECONDS


def is_stale(age_seconds: float | None) -> bool:
    """True if a push is too old to treat as news. Unknown age counts as fresh:
    dropping a real start is the worse mistake."""
    return age_seconds is not None and age_seconds > STALE_PUSH_SECONDS


_PERCENT = re.compile(r"(\d{1,3})\s*%")


def percent_in_text(text: str | None) -> int | None:
    """The first percentage in a push's text, or None.

    SAIC's finished-charging push says how full the battery is ("... remaining
    power is 68%"). Anything outside 0-100 is not a battery level.
    """
    if not text:
        return None
    match = _PERCENT.search(text)
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= 100 else None


def plausible_start_time(sent_at: float | None, now: float) -> float:
    """When to say the car started: SAIC's send time if believable, else now.

    Believable means recent and not in the future. A start time ahead of the
    clock would poison the power-on maths downstream, so anything odd is
    replaced by the time the push arrived, which is within seconds of the start.
    """
    if sent_at is not None and now - 6 * 3600 <= sent_at <= now + 60:
        return min(sent_at, now)
    return now


# A connection that has been down this long no longer counts as "connecting":
# the integration is relying on polling, and the sensor says so.
CONNECTING_GRACE_SECONDS = 120

# How often the listener looks after itself (state, renewal, health).
MAINTENANCE_INTERVAL_SECONDS = 30

# After a failed renewal or registration, wait this long before trying again.
RETRY_AFTER_FAILURE_SECONDS = 30 * 60


def renewal_due(registered_at: float | None, now: float) -> bool:
    """True once a registration is old enough to be replaced. Unknown age
    (credentials loaded without a date) is treated as due."""
    if registered_at is None:
        return True
    return now - registered_at >= RENEW_AFTER_SECONDS


# The push connection must have been up this long, without a break, before a
# start the poller found (and no push announced) is blamed on push. A start
# during a dropped connection is not the registration's fault.
CONTINUOUS_CONNECTION_REQUIRED_SECONDS = 35 * 60


class PushLedger:
    """Matches one kind of push (a car's starts, say) against the same messages
    found later in the account's message queue.

    One per car and kind. Times are plain epoch seconds.
    """

    def __init__(self) -> None:
        self._pending: list[float] = []  # pushes not yet seen in the queue
        self._handled: list[float] = []  # times already acted on
        self.total = 0  # pushes ever acted on (never reset)

    def is_duplicate(self, at: float) -> bool:
        """True if something at about this time has already been acted on."""
        return any(
            abs(at - seen) < START_DUPLICATE_WINDOW_SECONDS for seen in self._handled
        )

    def note_push(self, now: float) -> bool:
        """Record a push. True if it is new and should be acted on.

        False when the poller already acted on the same event, which can happen
        when the queue is read in the seconds before the push arrives. The
        queue message is then already used up, so nothing is left pending.
        """
        if self.is_duplicate(now):
            return False
        self._pending.append(now)
        self._remember(now)
        self.total += 1
        return True

    def note_poller_handled(self, now: float) -> None:
        """Record that the poller acted on a message no push had announced."""
        self._remember(now)

    def _remember(self, at: float) -> None:
        self._handled.append(at)
        del self._handled[:-10]

    def reconcile(self, found_in_queue: int, now: float) -> int:
        """Use up pushes against messages the poller has found in the queue.

        Returns how many of the found messages no push announced. Pushes that
        have waited too long for their message are forgotten.
        """
        self._pending = [
            seen for seen in self._pending if now - seen <= PUSH_START_MEMORY_SECONDS
        ]
        if found_in_queue <= 0:
            return 0
        matched = min(found_in_queue, len(self._pending))
        del self._pending[:matched]
        return found_in_queue - matched

    @property
    def pending(self) -> int:
        return len(self._pending)


class PushHealth:
    """Decides when push is connected but not delivering.

    A start the poller finds that no push announced is only a suspicion: the
    push can arrive just after the queue shows the message. If none turns up in
    time, the miss is confirmed and push is treated as unhealthy for a while.
    One per account, since every car shares one registration.
    """

    def __init__(self) -> None:
        self.suspected_at: float | None = None
        self.unhealthy_until: float = 0.0
        self.misses = 0  # confirmed misses, for the diagnostics attributes

    def suspect_miss(self, now: float, connected_since: float | None) -> bool:
        """The poller found a start no push announced. True if it counts."""
        if (
            connected_since is None
            or now - connected_since < CONTINUOUS_CONNECTION_REQUIRED_SECONDS
        ):
            return False
        if self.suspected_at is None:
            self.suspected_at = now
        return True

    def push_start_seen(self) -> None:
        """A start push arrived: delivery works, whatever was suspected."""
        self.suspected_at = None
        self.unhealthy_until = 0.0

    def seconds_until_verdict(self, now: float) -> float | None:
        if self.suspected_at is None:
            return None
        return max(0.0, self.suspected_at + MISSED_PUSH_CONFIRM_SECONDS - now)

    def confirm(self, now: float) -> bool:
        """True exactly once per miss, when the wait is over with no push."""
        if self.suspected_at is None:
            return False
        if now - self.suspected_at < MISSED_PUSH_CONFIRM_SECONDS:
            return False
        self.suspected_at = None
        self.unhealthy_until = now + UNHEALTHY_HOLD_SECONDS
        self.misses += 1
        return True

    def healthy(self, now: float) -> bool:
        return now >= self.unhealthy_until
