# File: message_poller.py
"""Account-level SAIC alarm message queue poller.

One instance of SAICMGAccountPoller exists per unique (username, region) pair,
regardless of how many vehicles are registered under that account.  It polls
the SAIC alarm message queue once every MESSAGE_POLL_INTERVAL_SECONDS and
routes each message to the correct per-VIN coordinator.

Why one poller per account, not one per VIN?
--------------------------------------------
The SAIC message queue is a server-side, per-account stream — it is not
filtered per vehicle at the API level.  If two coordinators (one per VIN)
both call get_alarm_messages concurrently they get duplicate messages,
consume each other's queue position, and can trigger session token
conflicts.  A single poller serialises all access so:
  - Exactly one get_alarm_messages call is made per 60-second cycle per
    account, regardless of how many VINs are configured.
  - Messages are routed to the correct coordinator by matching the VIN
    field on each message.  Messages with no VIN, or a VIN that isn't
    registered, are logged and discarded.

Why a shared client (not one client per VIN)?
---------------------------------------------
The SAIC backend maintains a single session per account.  When two
SAICMGAPIClient instances log in independently for the same account, each
login invalidates the other's session token, producing a cycle of 401 errors
and re-auth races.  This was the root cause of issues #147 and #195.

The fix: one SAICMGAPIClient per (username, region) account, shared by the
poller and all coordinators on that account.  __init__.async_setup_entry
creates this shared client on the first VIN and passes the same object to
every subsequent VIN for the same account.  All API calls (data fetches,
message polling, remote commands) go through this one client and its single
live session.

API lock per-page (not for entire poll loop)
--------------------------------------------
The api_lock is acquired for each individual page request rather than for
the entire pagination loop.  This prevents the poller from starving
coordinator data fetches on multi-VIN accounts during the (rare) case where
there are many new messages to process.

Lifecycle
---------
The poller is created and started by __init__.async_setup_entry when the
first VIN for an account is set up, and stopped when the last VIN for that
account is unloaded.  Intermediate add/remove of individual VINs is handled
via register_coordinator / unregister_coordinator.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone, timedelta

from .const import LOGGER

# ── Timing ───────────────────────────────────────────────────────────────────

# How often to poll the SAIC alarm message queue per account.
# 60 s matches the MQTT gateway default; the endpoint is lightweight.
MESSAGE_POLL_INTERVAL_SECONDS = 60

# ── Message classification keywords ──────────────────────────────────────────

# messageType string indicating vehicle start / engine-on (confirmed from
# saic-python-mqtt-gateway source — messageType "323").
MESSAGE_TYPE_VEHICLE_START = "323"

# Lower-cased title/content keywords for each event class.
# The SAIC REST API does not expose discrete messageTypes for shutdown or
# charging events in the same way as the MQTT gateway, so we infer from text.
_START_KEYWORDS = {"start", "engine on", "ignition on", "power on"}
_SHUTDOWN_KEYWORDS = {
    "vehicle off", "car off", "turned off", "engine off", "ignition off",
    "vehicle stopped", "parked",
}
_CHARGING_KEYWORDS = {
    "charging", "charge", "plugged", "connected to charger",
    "charging started", "ev connected",
}


class SAICMGAccountPoller:
    """Polls the SAIC alarm message queue once per minute for a single account.

    One instance covers all VINs on that account.  Coordinators register
    themselves via register_coordinator / unregister_coordinator.

    Thread-safety: all methods are designed to be called from within the
    HA event loop (async context).  The _api_lock is an asyncio.Lock and
    must only be acquired from async code.
    """

    def __init__(
        self,
        hass,
        client,
        account_key: tuple[str, str],
        api_lock: asyncio.Lock,
    ) -> None:
        """Initialise the poller.

        Args:
            hass:        Home Assistant instance.
            client:      SAICMGAPIClient already logged in for this account.
            account_key: (username, region) tuple that uniquely identifies
                         the account — used only for logging.
            api_lock:    asyncio.Lock shared across all coordinators on this
                         account.  The poller acquires it before every API
                         call, which serialises get_alarm_messages against
                         concurrent vehicle-data fetches.
        """
        self._hass = hass
        self._client = client
        self._account_key = account_key
        self._api_lock = api_lock

        # VIN → coordinator mapping.  Protected by asyncio single-threaded
        # event loop — no additional lock needed.
        self._coordinators: dict[str, object] = {}

        # Per-account message deduplication state.
        # We track the most-recently-seen message ID and timestamp across
        # all VINs so we never re-process a message even if the VIN changes
        # between polls (e.g. mixed-VIN multi-page responses).
        self._last_seen_message_id: str | int | None = None
        self._last_seen_message_ts: datetime | None = None
        self._first_poll_done: bool = False

        self._poll_task: asyncio.Task | None = None

    # ── Registration ─────────────────────────────────────────────────────────

    def register_coordinator(self, vin: str, coordinator) -> None:
        """Register a coordinator to receive messages for *vin*."""
        self._coordinators[vin] = coordinator
        LOGGER.debug(
            "AccountPoller %s: registered coordinator for VIN %s (%d total)",
            self._account_key,
            vin,
            len(self._coordinators),
        )

    def unregister_coordinator(self, vin: str) -> None:
        """Remove a coordinator.  The poller continues for remaining VINs."""
        self._coordinators.pop(vin, None)
        LOGGER.debug(
            "AccountPoller %s: unregistered VIN %s (%d remaining)",
            self._account_key,
            vin,
            len(self._coordinators),
        )

    @property
    def has_coordinators(self) -> bool:
        """Return True if at least one coordinator is still registered."""
        return bool(self._coordinators)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, config_entry) -> None:
        """Start the background polling task.

        Uses config_entry.async_create_background_task so HA owns the task
        lifecycle and will cancel it automatically on entry unload if we
        somehow miss async_stop.
        """
        if self._poll_task and not self._poll_task.done():
            LOGGER.debug(
                "AccountPoller %s: poll task already running", self._account_key
            )
            return

        LOGGER.info(
            "AccountPoller %s: starting message poll loop (interval %ds)",
            self._account_key,
            MESSAGE_POLL_INTERVAL_SECONDS,
        )
        self._poll_task = config_entry.async_create_background_task(
            self._hass,
            self._poll_loop(),
            f"mg_saic_account_poller_{self._account_key[0]}",
        )

    async def async_stop(self) -> None:
        """Cancel and await the polling task."""
        if self._poll_task and not self._poll_task.done():
            LOGGER.info(
                "AccountPoller %s: stopping message poll loop", self._account_key
            )
            self._poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None

    # ── Internal poll loop ───────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main loop: sleep, poll, route, repeat."""
        # Stagger startup slightly so the initial coordinator refresh
        # completes before we issue our first message check.
        await asyncio.sleep(MESSAGE_POLL_INTERVAL_SECONDS)

        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                LOGGER.debug(
                    "AccountPoller %s: poll loop cancelled", self._account_key
                )
                raise
            except Exception as exc:
                LOGGER.warning(
                    "AccountPoller %s: unhandled error in poll loop: %s",
                    self._account_key,
                    exc,
                )

            await asyncio.sleep(MESSAGE_POLL_INTERVAL_SECONDS)

    async def _poll_once(self) -> None:
        """Fetch new messages, route each to the correct coordinator.

        Paginates through the message queue (page_size=1) until we reach a
        message we have already seen.  Safety-limited to 20 pages so a
        large backlog on first run does not block the loop indefinitely.

        The api_lock is acquired per-page rather than for the entire loop so
        that coordinator data fetches on the same account are not starved while
        we paginate.  Each page acquisition is brief (one lightweight API call).

        401 handling: when the shared session token is refreshed by a concurrent
        coordinator re-auth, the next message poll may see a 401.  This is handled
        by re-logging in via the shared client and retrying once.  Because all
        coordinators share the same client object, the refreshed token is
        immediately available to them too.
        """
        new_messages: list = []
        page = 1
        max_pages = 20

        while page <= max_pages:
            response = None
            async with self._api_lock:
                try:
                    response = await self._client.get_alarm_messages(
                        page_num=page, page_size=1
                    )
                except Exception as exc:
                    exc_str = str(exc)
                    if "401" in exc_str:
                        LOGGER.debug(
                            "AccountPoller %s: 401 on message poll (token invalidated) "
                            "— re-logging in via shared client",
                            self._account_key,
                        )
                        try:
                            await self._client.login()
                            response = await self._client.get_alarm_messages(
                                page_num=page, page_size=1
                            )
                        except Exception as retry_exc:
                            LOGGER.warning(
                                "AccountPoller %s: re-auth and retry failed: %s",
                                self._account_key,
                                retry_exc,
                            )
                            break
                    else:
                        LOGGER.warning(
                            "AccountPoller %s: failed to fetch messages (page %d): %s",
                            self._account_key,
                            page,
                            exc,
                        )
                        break

            if not response or not getattr(response, "messages", None):
                break

            msg = response.messages[0]

            # Stop on a message we have already processed
            if (
                msg.messageId is not None
                and msg.messageId == self._last_seen_message_id
            ):
                break

            # Stop on a message older than our watermark
            if (
                self._last_seen_message_ts is not None
                and getattr(msg, "message_time", None) is not None
                and msg.message_time <= self._last_seen_message_ts
            ):
                break

            new_messages.append(msg)
            page += 1

        if not new_messages:
            LOGGER.debug(
                "AccountPoller %s: no new messages", self._account_key
            )
            return

        # On the very first successful poll, record the watermark and skip
        # acting on historical messages — prevents spurious refreshes every
        # time HA restarts.
        if not self._first_poll_done:
            latest = new_messages[0]
            self._last_seen_message_id = latest.messageId
            self._last_seen_message_ts = getattr(latest, "message_time", None)
            self._first_poll_done = True
            LOGGER.debug(
                "AccountPoller %s: first poll — watermarked at id=%s, "
                "skipping %d historical message(s)",
                self._account_key,
                latest.messageId,
                len(new_messages),
            )
            return

        # Capture the current watermark BEFORE advancing it.
        # The handler uses this to decide which type-323 messages to delete —
        # we keep the one that becomes the new watermark, delete the rest.
        # If we advance first (as was the case before this fix), the new
        # watermark ID matches the message being processed, so the deletion
        # guard incorrectly skips it and nothing ever gets deleted.
        pre_advance_watermark_id = self._last_seen_message_id

        # Advance the watermark to the newest message we fetched
        latest = new_messages[0]
        self._last_seen_message_id = latest.messageId
        self._last_seen_message_ts = getattr(latest, "message_time", None)

        LOGGER.info(
            "AccountPoller %s: %d new message(s) to process",
            self._account_key,
            len(new_messages),
        )

        # Group messages by VIN and route to the registered coordinator
        # Build a dict: vin → list[msg] (preserving newest-first ordering)
        by_vin: dict[str | None, list] = {}
        for msg in new_messages:
            vin = getattr(msg, "vin", None) or None
            by_vin.setdefault(vin, []).append(msg)

        for msg_vin, msgs in by_vin.items():
            coordinator = self._coordinators.get(msg_vin) if msg_vin else None

            if coordinator is None and msg_vin is not None:
                # Message for a VIN not managed by this HA instance — skip.
                LOGGER.debug(
                    "AccountPoller %s: message for unregistered VIN %s — skipping",
                    self._account_key,
                    msg_vin,
                )
                continue

            if coordinator is None:
                # No VIN on message at all — try to find which coordinator
                # should handle it if there's only one registered.
                if len(self._coordinators) == 1:
                    coordinator = next(iter(self._coordinators.values()))
                    LOGGER.debug(
                        "AccountPoller %s: message has no VIN, routing to sole "
                        "registered coordinator",
                        self._account_key,
                    )
                else:
                    LOGGER.debug(
                        "AccountPoller %s: message has no VIN and multiple VINs "
                        "are registered — cannot route, skipping",
                        self._account_key,
                    )
                    continue

            await self._handle_messages_for_coordinator(
                coordinator, msgs, msg_vin, pre_advance_watermark_id
            )

    async def _handle_messages_for_coordinator(
        self,
        coordinator,
        messages: list,
        vin: str | None,
        pre_advance_watermark_id: "str | int | None" = None,
    ) -> None:
        """Classify messages for one VIN, hint the coordinator, trigger a refresh,
        then delete processed vehicle-start messages from the SAIC queue.

        Deletion strategy (mirrors saic-python-mqtt-gateway message.py):
        - After processing, collect all type-323 (vehicle start) messages that
          are NOT the pre-advance watermark ID (i.e. not the most-recently-seen
          message ID from the PREVIOUS poll cycle).
        - The pre-advance watermark must be used here — by the time this handler
          runs, self._last_seen_message_id has already been advanced to the
          newest message in this batch, which would incorrectly match the message
          we're trying to delete and prevent deletion entirely.
        - Delete each of those older start messages individually.
        - This prevents unbounded queue growth on the SAIC server and avoids
          re-processing stale engine-start events after an HA restart.
        - Delete is best-effort: failures are logged at WARNING level and never
          block polling or refresh logic.

        Hint strategy (feature/account-level-message-poller improvement):
        - When a type-323 message is detected, extract its timestamp from
          ``createTime`` (Unix ms, timezone-unambiguous) and call
          ``coordinator.hint_vehicle_started(started_at)`` *before* requesting
          the confirming refresh.
        - This pre-sets ``is_powered_on=True``, stamps ``last_powered_on_time``
          with the actual vehicle start time (not the poll time), and immediately
          switches the coordinator to ``powered_update_interval``.
        - For users with long idle intervals (e.g. 12 h), this is the difference
          between switching to rapid polling within seconds vs. waiting hours.
        """
        should_refresh = False
        refresh_reason: list[str] = []

        # Collect type-323 messages seen this cycle so we can delete them after
        # processing.  We delete all EXCEPT the one that became the watermark
        # (self._last_seen_message_id), mirroring the MQTT gateway pattern.
        vehicle_start_messages_to_delete: list = []

        for msg in messages:
            title = (getattr(msg, "title", None) or "").lower()
            content = (getattr(msg, "content", None) or "").lower()
            msg_type = getattr(msg, "messageType", None) or ""
            msg_id = getattr(msg, "messageId", None)

            LOGGER.debug(
                "AccountPoller %s: processing message type=%s id=%s title='%s' "
                "for VIN %s",
                self._account_key,
                msg_type,
                msg_id,
                getattr(msg, "title", None),
                vin,
            )

            # Engine start — highest priority, act immediately
            if msg_type == MESSAGE_TYPE_VEHICLE_START or any(
                kw in title for kw in _START_KEYWORDS
            ):
                LOGGER.info(
                    "AccountPoller %s: engine-start event for VIN %s "
                    "(type=%s title='%s')",
                    self._account_key,
                    vin,
                    msg_type,
                    getattr(msg, "title", None),
                )
                should_refresh = True
                refresh_reason.append("engine start")

                # ── Hint the coordinator immediately ─────────────────────────
                # The vehicle-start message is processed within one ~60s poll of
                # the car actually starting, so "now" (UTC) is a reliable,
                # timezone-safe anchor for the hint. We only prefer a real
                # createTime (Unix epoch ms — timezone-unambiguous) when it's
                # plausible. We deliberately no longer trust message_time: it's a
                # naive server-local string that the SAIC EU backend stamps in
                # CET/CEST (UTC+1/+2), NOT UTC — treating it as UTC put the
                # pre-set powered-on time up to 2 hours in the FUTURE.
                now_utc = datetime.now(timezone.utc)
                started_at: datetime = now_utc
                hint_source = "current time"
                create_time_ms = getattr(msg, "createTime", None)
                if create_time_ms is not None:
                    try:
                        candidate = datetime.fromtimestamp(
                            create_time_ms / 1000.0, tz=timezone.utc
                        )
                    except (OSError, OverflowError, ValueError) as exc:
                        LOGGER.debug(
                            "AccountPoller %s: could not parse createTime %s: %s",
                            self._account_key,
                            create_time_ms,
                            exc,
                        )
                    else:
                        # Accept createTime only if it's plausible: recent and
                        # not in the future. Otherwise fall back to "now".
                        if (
                            now_utc - timedelta(hours=6)
                            <= candidate
                            <= now_utc + timedelta(minutes=1)
                        ):
                            started_at = candidate
                            hint_source = "createTime"
                        else:
                            LOGGER.debug(
                                "AccountPoller %s: createTime %s implausible "
                                "(now %s) — using current time for the hint",
                                self._account_key,
                                candidate,
                                now_utc,
                            )

                # Final safety net: a power-on time must never be in the future.
                if started_at > now_utc:
                    started_at = now_utc
                    hint_source = "current time"

                if hasattr(coordinator, "hint_vehicle_started"):
                    LOGGER.info(
                        "AccountPoller %s: hinting VIN %s started at %s "
                        "(from %s)",
                        self._account_key,
                        vin,
                        started_at,
                        hint_source,
                    )
                    coordinator.hint_vehicle_started(started_at)
                else:
                    LOGGER.debug(
                        "AccountPoller %s: no usable timestamp for hint (VIN %s)",
                        self._account_key,
                        vin,
                    )

                # ── Queue for deletion ───────────────────────────────────────
                # Delete ALL type-323 messages we process, including the one
                # that became the watermark.  The watermark prevents
                # re-processing even if deletion fails (the deduplication check
                # runs before deletion), so there is no risk of double-acting
                # on a message.  Excluding the watermark message was the
                # original design but in practice only one message arrives per
                # poll cycle, making that exclusion always True and deletion
                # never happening.
                if msg_type == MESSAGE_TYPE_VEHICLE_START and msg_id is not None:
                    vehicle_start_messages_to_delete.append(msg)

                # Engine start is the highest-priority event; no need to
                # inspect the remaining messages — break immediately.
                break

            # Vehicle shutdown
            if any(kw in title for kw in _SHUTDOWN_KEYWORDS) or any(
                kw in content for kw in _SHUTDOWN_KEYWORDS
            ):
                LOGGER.info(
                    "AccountPoller %s: shutdown event for VIN %s (title='%s')",
                    self._account_key,
                    vin,
                    getattr(msg, "title", None),
                )
                should_refresh = True
                refresh_reason.append("vehicle shutdown")

            # Charging / plug-in
            if any(kw in title for kw in _CHARGING_KEYWORDS) or any(
                kw in content for kw in _CHARGING_KEYWORDS
            ):
                LOGGER.info(
                    "AccountPoller %s: charging/plug-in event for VIN %s "
                    "(title='%s')",
                    self._account_key,
                    vin,
                    getattr(msg, "title", None),
                )
                should_refresh = True
                refresh_reason.append("charging detected")

        if should_refresh:
            reason_str = " + ".join(refresh_reason)
            LOGGER.info(
                "AccountPoller %s: triggering refresh for VIN %s — reason: %s",
                self._account_key,
                vin,
                reason_str,
            )
            await coordinator.async_trigger_refresh(reason_str)

        # ── Delete consumed vehicle-start messages ────────────────────────────
        # Delete all type-323 messages processed this cycle.  The watermark
        # (self._last_seen_message_id) already prevents re-processing on the
        # next poll even if deletion fails, so it is safe to delete the message
        # that became the watermark too.  Previously we excluded the watermark
        # message from deletion, but since typically only one message arrives
        # per 60-second poll cycle that exclusion meant nothing was ever deleted.
        #
        # Deletion runs AFTER async_trigger_refresh so a delete error never
        # blocks the refresh.  Each delete is individually suppressed so one
        # bad message ID doesn't prevent the rest from being cleaned up.
        if vehicle_start_messages_to_delete:
            LOGGER.debug(
                "AccountPoller %s: deleting %d consumed vehicle-start message(s) "
                "for VIN %s",
                self._account_key,
                len(vehicle_start_messages_to_delete),
                vin,
            )
            for msg in vehicle_start_messages_to_delete:
                msg_id = getattr(msg, "messageId", None)
                if msg_id is None:
                    continue
                async with self._api_lock:
                    with suppress(Exception):
                        await self._client.delete_message(msg_id)
                        LOGGER.debug(
                            "AccountPoller %s: deleted vehicle-start message "
                            "id=%s for VIN %s",
                            self._account_key,
                            msg_id,
                            vin,
                        )
