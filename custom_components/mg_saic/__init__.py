# File: __init__.py

import asyncio
from contextlib import suppress

from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady
from homeassistant.core import HomeAssistant

from .backends import Feature, backend_supports, create_backend
from .coordinator import SAICMGDataUpdateCoordinator
from .message_poller import SAICMGAccountPoller
from .const import DOMAIN, LOGGER, PLATFORMS
from .services import async_setup_services, async_unload_services

# ── Domain-level hass.data structure ─────────────────────────────────────────
#
# hass.data[DOMAIN] = {
#   # Per-entry references (keyed by entry.entry_id)
#   "<entry_id>":             SAICMGAPIClient  ← shared client for the account
#   "<entry_id>_coordinator": SAICMGDataUpdateCoordinator
#
#   # Global VIN → resource maps (for service resolution)
#   "clients_by_vin":         { vin: SAICMGAPIClient }
#   "coordinators_by_vin":    { vin: SAICMGDataUpdateCoordinator }
#
#   # Account-level singletons keyed by (username, region)
#   "account_clients":        { account_key: SAICMGAPIClient }   ← THE shared client
#   "account_pollers":        { account_key: SAICMGAccountPoller }
#   "account_locks":          { account_key: asyncio.Lock }
#   "account_login_locks":    { account_key: asyncio.Lock }
#
#   "services_registered":    bool
# }
#
# KEY DESIGN: one SAICMGAPIClient per (username, region) account, shared by all
# config entries (VINs) on that account.  The SAIC server maintains a single
# session per account — concurrent logins from two client instances invalidate
# each other's tokens, causing the cascading 401/re-auth races reported in #147
# and #195.  By sharing one client, all VINs on the same account use the same
# session token, eliminating these races entirely.
#
# The account_lock serialises all API calls that share the same session,
# preventing concurrent requests from different coordinators/the poller from
# interleaving and confusing the server.
#
# The account_login_lock serialises the initial login so that if two entries
# for the same account start simultaneously (HA startup), only one login
# attempt is made.


def _account_key(entry: ConfigEntry) -> tuple[str, str]:
    """Return a hashable identifier for the SAIC account behind *entry*."""
    return (entry.data["username"], entry.data.get("region", ""))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up MG SAIC from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    domain = hass.data[DOMAIN]
    domain.setdefault("clients_by_vin", {})
    domain.setdefault("coordinators_by_vin", {})
    domain.setdefault("account_clients", {})
    domain.setdefault("account_pollers", {})
    domain.setdefault("account_locks", {})
    domain.setdefault("account_login_locks", {})
    domain.setdefault("services_registered", False)

    vin = entry.data.get("vin")
    acct_key = _account_key(entry)

    # ── Ensure per-account singletons exist ──────────────────────────────────
    if acct_key not in domain["account_locks"]:
        domain["account_locks"][acct_key] = asyncio.Lock()
    if acct_key not in domain["account_login_locks"]:
        domain["account_login_locks"][acct_key] = asyncio.Lock()

    api_lock = domain["account_locks"][acct_key]
    login_lock = domain["account_login_locks"][acct_key]

    # ── Get or create the shared account client ───────────────────────────────
    # One SAICMGAPIClient per (username, region).  The SAIC backend maintains a
    # single session per account — two concurrent client instances logging in
    # independently will invalidate each other's tokens (the root cause of #147
    # and #195).  All VINs on the same account share this one client object so
    # there is exactly one live session at all times.
    async with login_lock:
        if acct_key not in domain["account_clients"]:
            LOGGER.debug(
                "Creating shared client for account %s (first VIN: %s)",
                acct_key,
                vin,
            )
            # Backend selection: region "India" gets the TAP backend, every
            # other region gets the global REST client constructed exactly
            # as before (see backends/__init__.py).
            client = create_backend(entry.data)
            try:
                await client.login()
            except Exception as exc:
                LOGGER.error(
                    "Failed to log in to MG SAIC for account %s (VIN %s): %s",
                    acct_key,
                    vin,
                    exc,
                )
                # Release any client-held resources (the India backend owns an
                # aiohttp.ClientSession that would otherwise leak and emit
                # "Unclosed client session" warnings on every failed setup).
                with suppress(Exception):
                    await client.close()
                raise ConfigEntryNotReady(
                    "Could not log in to MG SAIC; Home Assistant will retry "
                    "automatically."
                ) from exc
            domain["account_clients"][acct_key] = client
            LOGGER.debug("Login successful for account %s", acct_key)
        else:
            LOGGER.debug(
                "Reusing existing shared client for account %s (adding VIN: %s)",
                acct_key,
                vin,
            )
            client = domain["account_clients"][acct_key]

    if not vin:
        LOGGER.error("No VIN specified in Config Entry")
        return False

    try:
        vehicles = await client.get_vehicle_info()
        # A transient SAIC server error (e.g. HTTP 500 on /vehicle/list) can
        # return None or an empty list rather than raising. Treat that as
        # "not ready yet" and let HA retry with backoff, rather than a
        # permanent setup failure that requires a manual reload once SAIC
        # recovers. Guarding here also avoids "'NoneType' object is not
        # iterable" when vehicles is None (reported by Johndowne, issue #203;
        # root cause was a SAIC-side 500, not the integration).
        if not vehicles:
            raise ConfigEntryNotReady(
                "SAIC API returned no vehicle list (likely a temporary "
                "server-side error) — HA will retry automatically."
            )
        if not any(v.vin == vin for v in vehicles):
            LOGGER.error("VIN %s not found in account vehicles", vin)
            return False
    except ConfigEntryNotReady:
        # Propagate so HA schedules an automatic retry.
        raise
    except Exception as exc:
        # Any other error verifying the VIN (network blip, transient API
        # failure) is also treated as "not ready" so HA retries rather than
        # permanently failing setup.
        raise ConfigEntryNotReady(
            f"Could not verify VIN {vin} yet: {exc}"
        ) from exc

    # Store a reference to the shared client under this entry's key so that
    # platform files (button.py, climate.py, …) can fetch it as normal via
    # hass.data[DOMAIN][entry.entry_id].  They all get the same object.
    domain[entry.entry_id] = client

    # ── Build and wire up the coordinator ────────────────────────────────────
    coordinator = SAICMGDataUpdateCoordinator(hass, client, entry)
    coordinator.set_api_lock(api_lock)

    # If this is a second (or later) VIN on the same account, stagger startup
    # by a few seconds so both coordinators don't race to hit the SAIC API
    # simultaneously through the shared client at HA boot time.  Without this,
    # the second coordinator's async_config_entry_first_refresh frequently times
    # out waiting for the api_lock while the first coordinator holds it.
    if acct_key in domain["coordinators_by_vin"] or any(
        _account_key(e) == acct_key
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.entry_id in domain
    ):
        LOGGER.debug(
            "VIN %s is a secondary VIN on account %s — staggering startup by 5s",
            vin,
            acct_key,
        )
        await asyncio.sleep(5)

    try:
        await coordinator.async_setup()
    except ConfigEntryNotReady:
        # Propagate so HA marks the entry as "Retrying" and retries automatically.
        raise
    except Exception as exc:
        LOGGER.error("Coordinator setup failed for VIN %s: %s", vin, exc)
        return False

    domain[f"{entry.entry_id}_coordinator"] = coordinator
    domain["clients_by_vin"][vin] = client
    domain["coordinators_by_vin"][vin] = coordinator

    # ── Account-level message poller ─────────────────────────────────────────
    # One SAICMGAccountPoller per (username, region) regardless of VIN count.
    # It uses the shared account client to poll get_alarm_messages once per
    # minute and routes events to the correct per-VIN coordinator.
    #
    # Capability-gated: the poller and alarm-switch registration only exist on
    # backends that support alarm messages (the global REST API).  The India
    # TAP protocol has no message-list endpoint, so India accounts skip this
    # entire section and rely on coordinator polling alone.
    poller = None
    if backend_supports(client, Feature.ALARM_MESSAGES):
        if acct_key not in domain["account_pollers"]:
            LOGGER.debug(
                "Creating new AccountPoller for account %s (first VIN: %s)",
                acct_key,
                vin,
            )
            poller = SAICMGAccountPoller(hass, client, acct_key, api_lock)
            domain["account_pollers"][acct_key] = poller
        else:
            LOGGER.debug(
                "Reusing existing AccountPoller for account %s (adding VIN: %s)",
                acct_key,
                vin,
            )
            poller = domain["account_pollers"][acct_key]

        poller.register_coordinator(vin, coordinator)

        # Start (or no-op if already running) the poller background task.
        poller.start(entry)

        # ── Register alarm switches for this VIN ─────────────────────────────
        # Tells the SAIC server to queue event messages for us.  Each VIN needs
        # its own registration.  Calls are serialised under the api_lock so
        # they cannot race against concurrent data fetches or each other on
        # multi-VIN accounts.
        try:
            async with api_lock:
                await asyncio.wait_for(
                    client.set_alarm_switches(vin=vin),
                    timeout=30,
                )
        except asyncio.TimeoutError:
            LOGGER.warning(
                "set_alarm_switches timed out for VIN %s — "
                "message-driven updates may not function until next restart",
                vin,
            )
        except Exception as exc:
            LOGGER.warning(
                "set_alarm_switches failed for VIN %s: %s — "
                "message-driven updates may not function until next restart",
                vin,
                exc,
            )
    else:
        LOGGER.debug(
            "Backend for account %s does not support alarm messages — "
            "skipping AccountPoller and alarm-switch registration",
            acct_key,
        )

    # ── Finalise ─────────────────────────────────────────────────────────────
    entry.async_on_unload(entry.add_update_listener(update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not domain["services_registered"]:
        await async_setup_services(hass)
        domain["services_registered"] = True

    LOGGER.info(
        "MG SAIC integration setup completed for VIN %s (account %s, %s)",
        vin,
        acct_key,
        f"{len(poller._coordinators)} VIN(s) on poller"
        if poller is not None
        else "no message poller for this backend",
    )
    return True


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    await coordinator.async_update_options(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain = hass.data[DOMAIN]
    vin = entry.data.get("vin")
    acct_key = _account_key(entry)

    # ── Coordinator shutdown ──────────────────────────────────────────────────
    coordinator = domain.pop(f"{entry.entry_id}_coordinator", None)
    # Remove the per-entry client reference (it's a shared object; don't close it yet)
    domain.pop(entry.entry_id, None)

    if vin:
        domain.get("clients_by_vin", {}).pop(vin, None)
        domain.get("coordinators_by_vin", {}).pop(vin, None)

    if coordinator is not None:
        await coordinator.async_shutdown()

    # ── Poller VIN deregistration ─────────────────────────────────────────────
    poller = domain.get("account_pollers", {}).get(acct_key)
    if poller is not None and vin:
        poller.unregister_coordinator(vin)
        LOGGER.debug(
            "VIN %s unregistered from AccountPoller %s (%d remaining)",
            vin,
            acct_key,
            len(poller._coordinators),
        )
        if not poller.has_coordinators:
            LOGGER.info(
                "Last VIN unloaded for account %s — stopping AccountPoller and closing session",
                acct_key,
            )
            await poller.async_stop()
            domain["account_pollers"].pop(acct_key, None)
            domain["account_locks"].pop(acct_key, None)
            domain["account_login_locks"].pop(acct_key, None)

            # Close the shared client session only when the last VIN is gone
            shared_client = domain.get("account_clients", {}).pop(acct_key, None)
            if shared_client is not None:
                with suppress(Exception):
                    await shared_client.close()

    # ── Global cleanup when no entries remain ─────────────────────────────────
    if not domain.get("clients_by_vin"):
        await async_unload_services(hass)
        for key in (
            "clients_by_vin",
            "coordinators_by_vin",
            "account_clients",
            "account_pollers",
            "account_locks",
            "account_login_locks",
            "services_registered",
        ):
            domain.pop(key, None)

    if not domain:
        hass.data.pop(DOMAIN, None)

    return True
