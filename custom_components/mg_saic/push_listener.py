# File: push_listener.py
"""Real-time push for one SAIC account.

SAIC sends the car's alerts (it started, it finished charging) to a Firebase
Cloud Messaging registration that the iSmart app owns. This integration makes
a registration of its own, tells SAIC about it when it logs in, and listens for
the pushes. A push says "this car just started" within seconds, so Home
Assistant no longer has to ask SAIC every minute whether anything happened --
and every one of those minute-by-minute asks wakes the car's 12V battery.

One ``PushListener`` serves a whole account: SAIC keeps one session per account,
the registration belongs to that session, and pushes for every car on the
account arrive on the one connection, each naming its VIN.

The rules (what counts as stale, how pushes are matched with the message queue,
when push is judged not to be working) are in ``push_logic.py`` so they can be
tested without Home Assistant. The protocol is in the SAIC client library.

Polling stays as the safety net. The message poller keeps running, slowly while
push is healthy and every minute when it is not (``message_poller.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import suppress
from datetime import datetime, timezone

from .const import EVENT_PUSH_RECEIVED, LOGGER
from .push_logic import (
    CONNECTING_GRACE_SECONDS,
    MAINTENANCE_INTERVAL_SECONDS,
    RETRY_AFTER_FAILURE_SECONDS,
    PushHealth,
    PushReason,
    PushStatus,
    is_stale,
    plausible_start_time,
    renewal_due,
)

try:  # protobuf is an optional extra of the library
    from saic_ismart_client_ng.push import (
        PushCredentials,
        PushRegistrationError,
        SaicPushClient,
    )

    PUSH_LIBRARY_AVAILABLE = True
except ImportError:  # no protobuf, or a library that predates push
    PushCredentials = PushRegistrationError = SaicPushClient = None  # type: ignore[assignment,misc]
    PUSH_LIBRARY_AVAILABLE = False

STORAGE_VERSION = 1
# Pushes' message ids kept across restarts, so a restart doesn't see old ones again.
MAX_SAVED_MESSAGE_IDS = 200
# Wait before re-arming the alarm switches, to stay out of the start-up rush.
REARM_DELAY_SECONDS = 60


def _store_key(account_key) -> str:
    """Storage file name for an account. Hashed, so no username is in a file name."""
    digest = hashlib.sha256(
        "|".join(str(part) for part in account_key).encode()
    ).hexdigest()[:16]
    return f"mg_saic_push_{digest}"


def _vin_key(vin: str) -> str:
    return hashlib.sha256(vin.encode()).hexdigest()[:12]


def _create_store(hass, account_key):
    from homeassistant.helpers.storage import Store

    return Store(hass, STORAGE_VERSION, _store_key(account_key))


async def async_unregister_account(hass, account_key) -> None:
    """Remove the account's push registration and forget it.

    Used when push is switched off for the last car on an account, or the last
    car is deleted. Best effort: the registration disappears from Google's side
    on its own in time if this cannot reach it.
    """
    store = _create_store(hass, account_key)
    try:
        saved = await store.async_load()
    except Exception:  # noqa: BLE001 - nothing to clean up if it can't be read
        return
    if not isinstance(saved, dict):
        return
    try:
        if PUSH_LIBRARY_AVAILABLE and isinstance(saved.get("credentials"), dict):
            credentials = PushCredentials.from_dict(saved["credentials"])
            client = SaicPushClient(lambda _message: None, credentials=credentials)
            with suppress(Exception):
                await asyncio.wait_for(client.unregister(), timeout=20)
    finally:
        with suppress(Exception):
            await store.async_remove()
    LOGGER.debug("Push: removed the saved registration for an account")


class PushListener:
    """Receives SAIC's pushes for one account and acts on them."""

    def __init__(self, hass, account_key, api_lock: asyncio.Lock) -> None:
        self._hass = hass
        self._account_key = account_key
        self._api_lock = api_lock
        self._client = None  # the account's SAICMGAPIClient
        self._poller = None  # the account's message poller

        self._store_instance = None
        self._push = None  # SaicPushClient
        self._token: str | None = None
        self._registered_at: float | None = None
        self._saved_ids: list[str] = []
        self._rearmed: set[str] = set()

        # VIN -> coordinator, only for cars that have push switched on.
        self._coordinators: dict[str, object] = {}

        self.health = PushHealth()
        self._failure: PushReason | None = None
        self._connected_since: float | None = None
        self._disconnected_since: float | None = time.time()
        self._last_status: PushStatus | None = None
        self._next_attempt_at = 0.0
        self._bind_pending = False

        self._tasks: set[asyncio.Task] = set()
        self._maintenance_task: asyncio.Task | None = None
        self._renew_lock = asyncio.Lock()
        self._started = False

    @property
    def _store(self):
        # Made on first use, so nothing here needs Home Assistant until then.
        if self._store_instance is None:
            self._store_instance = _create_store(self._hass, self._account_key)
        return self._store_instance

    # ── What others read ─────────────────────────────────────────────────────

    @property
    def token(self) -> str | None:
        return self._token

    @property
    def connected(self) -> bool:
        return bool(self._push is not None and self._push.connected)

    @property
    def connected_since(self) -> float | None:
        return self._connected_since

    @property
    def status(self) -> PushStatus:
        """Push if it is working, polling if the integration is relying on the
        message queue instead, connecting while it gets going."""
        if self._push is None or self._failure is not None:
            return PushStatus.POLLING
        now = time.time()
        if self.connected:
            return PushStatus.PUSH if self.health.healthy(now) else PushStatus.POLLING
        if (
            self._disconnected_since is not None
            and now - self._disconnected_since > CONNECTING_GRACE_SECONDS
        ):
            return PushStatus.POLLING
        return PushStatus.CONNECTING

    @property
    def reason(self) -> PushReason | None:
        """Why push is not what is in use, or None while it is."""
        if self._failure is not None:
            return self._failure
        if self._push is None:
            return PushReason.REGISTRATION_FAILED
        if self.status is PushStatus.PUSH:
            return None
        if self.connected:
            return PushReason.MISSED_PUSH
        return PushReason.DISCONNECTED

    def diagnostics(self) -> dict:
        """Facts for the Push Status sensor's attributes."""
        now = time.time()
        attrs: dict = {
            "reason": self.reason.value if self.reason else None,
            "connected": self.connected,
            "connected_since": _iso(self._connected_since),
            "registration_age_hours": (
                round((now - self._registered_at) / 3600, 1)
                if self._registered_at
                else None
            ),
            "missed_pushes": self.health.misses,
        }
        if self._poller is not None:
            attrs["message_poll_interval_seconds"] = self._poller.current_interval()
        return attrs

    # ── Wiring ───────────────────────────────────────────────────────────────

    def register_coordinator(self, vin: str, coordinator) -> None:
        self._coordinators[vin] = coordinator

    def unregister_coordinator(self, vin: str) -> None:
        self._coordinators.pop(vin, None)

    @property
    def has_coordinators(self) -> bool:
        return bool(self._coordinators)

    def attach(self, client, poller=None) -> None:
        self._client = client
        if poller is not None:
            self._poller = poller

    # ── Registration ─────────────────────────────────────────────────────────

    async def async_prepare(self) -> str | None:
        """Make sure there is a registration, and return its token.

        Called before the account logs in to SAIC, so the login can carry the
        token. Never raises: when push can't be set up the integration carries
        on with polling alone, and says so on the Push Status sensor.
        """
        if self._push is not None and self._token:
            return self._token
        if not PUSH_LIBRARY_AVAILABLE:
            self._failure = PushReason.NO_LIBRARY
            LOGGER.warning(
                "Push notifications are on, but the SAIC library's push support "
                "isn't available (is the protobuf package installed?). "
                "Carrying on with polling."
            )
            return None

        saved = None
        try:
            saved = await self._store.async_load()
        except Exception as exc:  # noqa: BLE001 - start over if unreadable
            LOGGER.debug("Push: could not read the saved registration: %s", exc)

        credentials = None
        if isinstance(saved, dict):
            with suppress(Exception):
                credentials = PushCredentials.from_dict(saved.get("credentials") or {})
            self._registered_at = saved.get("registered_at")
            self._saved_ids = list(saved.get("persistent_ids") or [])
            self._rearmed = set(saved.get("rearmed") or [])

        push = SaicPushClient(
            self._on_message,
            credentials=credentials,
            persistent_ids=self._saved_ids,
            on_connection_state=self._on_connection_state,
            on_credentials_rejected=self._on_credentials_rejected,
        )
        try:
            if credentials is None:
                async with asyncio.timeout(45):
                    credentials = await push.register()
                self._registered_at = time.time()
                self._saved_ids = []
        except Exception as exc:  # noqa: BLE001 - polling carries on
            self._failure = PushReason.REGISTRATION_FAILED
            self._next_attempt_at = time.time() + RETRY_AFTER_FAILURE_SECONDS
            LOGGER.warning(
                "Push notifications could not be set up (%s). Carrying on with "
                "polling; will try again later.",
                exc,
            )
            with suppress(Exception):
                await push.stop()
            return None

        self._push = push
        self._token = credentials.token
        self._failure = None
        await self._async_save()
        LOGGER.debug("Push: registration ready")
        return self._token

    async def async_bind_if_needed(self) -> None:
        """Log in again with the token if the client was logged in without it."""
        if not self._token or self._client is None:
            return
        if getattr(self._client, "push_token", None) == self._token:
            return
        await self._async_bind(self._token)

    async def _async_bind(self, token: str) -> bool:
        try:
            async with self._api_lock:
                await self._client.async_bind_push_token(token)
        except Exception as exc:  # noqa: BLE001
            self._failure = PushReason.LOGIN_FAILED
            self._bind_pending = True
            LOGGER.warning(
                "Could not log in to SAIC with the new push registration (%s). "
                "Will try again; polling carries on meanwhile.",
                exc,
            )
            self._publish()
            return False
        self._bind_pending = False
        if self._failure is PushReason.LOGIN_FAILED:
            self._failure = None
        return True

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        """Connect and start looking after the connection. Idempotent."""
        if self._started:
            return
        self._started = True
        # Not tied to one config entry: other cars on the account share this,
        # and it must outlive any single one of them being reloaded.
        self._maintenance_task = self._hass.async_create_background_task(
            self._maintenance_loop(), "mg_saic_push_maintenance"
        )
        if self._push is not None:
            try:
                await self._push.start()
            except Exception as exc:  # noqa: BLE001
                self._failure = PushReason.REGISTRATION_FAILED
                LOGGER.warning("Push connection could not be started: %s", exc)
        self._publish()

    async def async_stop(self) -> None:
        """Disconnect. The registration is kept, for the next start."""
        self._started = False
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        if self._push is not None:
            await self._async_save()
            with suppress(Exception):
                await self._push.stop()
        self._coordinators.clear()
        self._poller = None

    def _spawn(self, coro, name: str) -> None:
        task = self._hass.async_create_background_task(coro, f"mg_saic_push_{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ── Connection state ─────────────────────────────────────────────────────

    def _on_connection_state(self, connected: bool) -> None:
        now = time.time()
        if connected:
            self._connected_since = now
            self._disconnected_since = None
        else:
            self._connected_since = None
            if self._disconnected_since is None:
                self._disconnected_since = now
        self._publish()

    def _publish(self) -> None:
        """Tell the poller and the sensors when the status changes."""
        status = self.status
        if status is self._last_status:
            return
        previous, self._last_status = self._last_status, status
        LOGGER.info(
            "Push status for account %s: %s -> %s%s",
            _mask(self._account_key),
            previous.value if previous else "none",
            status.value,
            f" ({self.reason.value})" if self.reason else "",
        )
        if self._poller is not None:
            self._poller.notify_status_changed()
        self._refresh_entities()

    def _refresh_entities(self) -> None:
        for coordinator in list(self._coordinators.values()):
            with suppress(Exception):
                coordinator.async_update_listeners()

    # ── Pushes ───────────────────────────────────────────────────────────────

    async def _on_message(self, message) -> None:
        """Handle one push. Must not raise: the connection depends on it."""
        try:
            self._handle(message)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Push: could not handle a message")

    def _handle(self, message) -> None:
        now = time.time()
        vin = message.vin
        coordinator = self._coordinators.get(vin) if vin else None
        if coordinator is None and not vin and len(self._coordinators) == 1:
            coordinator = next(iter(self._coordinators.values()))
        if coordinator is None:
            LOGGER.debug(
                "Push: type %s for a car not using push here — ignored",
                message.message_type,
            )
            return

        age = message.age_seconds()
        stale = is_stale(age)
        action = "none"
        if message.is_vehicle_start:
            if stale:
                action = "stale"
            else:
                self.health.push_start_seen()
                if coordinator.push_start_ledger.note_push(now):
                    sent = message.sent_at.timestamp() if message.sent_at else None
                    started_at = datetime.fromtimestamp(
                        plausible_start_time(sent, now), tz=timezone.utc
                    )
                    action = "refresh"
                    self._run(coordinator.async_handle_vehicle_start(started_at))
                else:
                    action = "already_handled"
        elif message.is_charge_finished:
            if stale:
                action = "stale"
            elif coordinator.push_charge_ledger.note_push(now):
                action = "refresh"
                self._run(
                    coordinator.async_trigger_refresh("charging finished (push)")
                )
            else:
                action = "already_handled"

        LOGGER.info(
            "Push received: type=%s title=%r age=%s -> %s",
            message.message_type,
            message.title,
            f"{age:.0f}s" if age is not None else "unknown",
            action,
        )
        coordinator.record_push(message, stale=stale)

        data = message.as_event_data()
        data.update(
            {
                "age_seconds": round(age, 1) if age is not None else None,
                "stale": stale,
                "action": action,
            }
        )
        self._hass.bus.async_fire(EVENT_PUSH_RECEIVED, data)

    def _run(self, coroutine) -> None:
        """Run a handler without holding up the connection while it does."""

        async def guarded():
            try:
                await coroutine
            except Exception:  # noqa: BLE001
                LOGGER.exception("Push: handling a message failed")

        self._spawn(guarded(), "handler")

    # ── Health ───────────────────────────────────────────────────────────────

    def note_unannounced_start(self) -> None:
        """The poller found a start in the queue that no push announced."""
        if self.health.suspect_miss(time.time(), self._connected_since):
            LOGGER.debug(
                "Push: a start was in the queue with no push for it — "
                "waiting to see whether the push is just late"
            )

    def _on_credentials_rejected(self) -> None:
        LOGGER.warning(
            "Google has refused the push registration repeatedly — making a new one"
        )
        self._spawn(self.async_renew("registration rejected"), "renew")

    async def async_renew(self, why: str) -> None:
        """Replace the registration and tell SAIC about the new one."""
        if self._push is None:
            return
        async with self._renew_lock:
            LOGGER.info("Push: replacing the registration (%s)", why)
            try:
                async with asyncio.timeout(60):
                    credentials = await self._push.renew()
            except Exception as exc:  # noqa: BLE001
                self._next_attempt_at = time.time() + RETRY_AFTER_FAILURE_SECONDS
                LOGGER.warning("Push: could not make a new registration: %s", exc)
                return
            self._registered_at = time.time()
            self._token = credentials.token
            await self._async_save()
            await self._async_bind(credentials.token)
            self._publish()

    # ── Looking after itself ─────────────────────────────────────────────────

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL_SECONDS)
            try:
                await self._maintain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                LOGGER.exception("Push: maintenance failed")

    async def _maintain(self) -> None:
        now = time.time()
        if self.health.confirm(now):
            LOGGER.warning(
                "A car started and push never announced it, though the connection "
                "was up. Polling every minute for a while and making a new "
                "registration."
            )
            self._spawn(self.async_renew("a start was missed"), "renew")
        elif self._push is None:
            # Without the library there is nothing to retry until a restart.
            if self._failure is not PushReason.NO_LIBRARY and now >= self._next_attempt_at:
                await self._retry_registration()
        elif self._bind_pending and self._token:
            await self._async_bind(self._token)
        elif now >= self._next_attempt_at and renewal_due(self._registered_at, now):
            await self.async_renew("registration is a day old")
        await self._async_save_if_changed()
        self._publish()

    async def _retry_registration(self) -> None:
        token = await self.async_prepare()
        if token is None:
            return
        await self._async_bind(token)
        if self._push is not None:
            with suppress(Exception):
                await self._push.start()

    # ── Storage ──────────────────────────────────────────────────────────────

    async def _async_save(self) -> None:
        if self._push is None or self._push.credentials is None:
            return
        self._saved_ids = list(self._push.persistent_ids)[-MAX_SAVED_MESSAGE_IDS:]
        try:
            await self._store.async_save(
                {
                    "credentials": self._push.credentials.as_dict(),
                    "registered_at": self._registered_at,
                    "persistent_ids": self._saved_ids,
                    "rearmed": sorted(self._rearmed),
                }
            )
        except Exception as exc:  # noqa: BLE001 - a registration can be redone
            LOGGER.debug("Push: could not save the registration: %s", exc)

    async def _async_save_if_changed(self) -> None:
        if self._push is None:
            return
        if list(self._push.persistent_ids)[-MAX_SAVED_MESSAGE_IDS:] != self._saved_ids:
            await self._async_save()

    # ── Alarm switches ───────────────────────────────────────────────────────

    def schedule_rearm(self, vin: str) -> None:
        """Re-arm the car's alarm switches once, the first time push is used.

        SAIC seems to send the charge-finished alert only after its
        notification switch has been turned off and on again. Once is enough,
        so it is remembered; the Push Status sensor doesn't depend on it.
        """
        if self._push is None or self._client is None or _vin_key(vin) in self._rearmed:
            return
        self._spawn(self._async_rearm(vin), "rearm")

    async def _async_rearm(self, vin: str) -> None:
        await asyncio.sleep(REARM_DELAY_SECONDS)
        async with self._api_lock:
            done = await self._client.rearm_alarm_switches(vin)
        if done:
            self._rearmed.add(_vin_key(vin))
            await self._async_save()


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _mask(account_key) -> str:
    username = account_key[0] if account_key else ""
    return f"{str(username)[:1]}***" if username else "***"
