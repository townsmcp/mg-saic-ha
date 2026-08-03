# File: event.py

from homeassistant.components.event import EventEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import re
from .const import DOMAIN, LOGGER, SAIC_RETURN_CODE_UNREACHABLE
from .utils import create_device_info

# Event types this entity can fire. Only types listed here may be triggered —
# attempting to fire any other type raises a ValueError (HA core enforces
# this in EventEntity._trigger_event).
EVENT_TYPE_COMMAND_ERROR = "command_error"
EVENT_TYPE_COMMAND_LIMIT_REACHED = "command_limit_reached"

EVENT_TYPES = [
    EVENT_TYPE_COMMAND_ERROR,
    EVENT_TYPE_COMMAND_LIMIT_REACHED,
]

# SAIC return code -> plain-English explanation, so the Logbook shows readable
# text instead of the raw exception string.
_RETURN_CODE_REASONS = {
    SAIC_RETURN_CODE_UNREACHABLE: (  # 4
        "The car couldn't be reached — it may be asleep or out of signal. "
        "Please try again shortly."
    ),
    8: (
        "The remote-command limit was reached. Start the car with the physical "
        "key to reset it."
    ),
}


def _extract_return_code(text: str):
    """Pull a SAIC 'return code: N' out of an error string, if present."""
    match = re.search(r"return code[:=]?\s*(\d+)", text.lower())
    return int(match.group(1)) if match else None


def _humanize_source(source: str) -> str:
    """Turn an internal source id into a readable action label.

    e.g. "Error setting HVAC mode" -> "Setting HVAC mode",
         "service.locking_vehicle" -> "Locking vehicle",
         "front_defrost" -> "Front defrost".
    """
    if not source:
        return "Remote command"
    text = str(source)
    if text.lower().startswith("error "):
        text = text[len("error "):]
    text = text.replace("service.", "").replace(".", " ").replace("_", " ").strip()
    if not text:
        return "Remote command"
    return text[:1].upper() + text[1:]


def _humanize_command_error(source: str, error: str) -> dict:
    """Build Logbook-ready attributes from a raw command failure.

    Backward-compatible: the original `source` and `error` keys are preserved
    (this event entity has existed since 1.0.5, so automations may read them),
    and the friendly `action`/`reason`/`code` keys are added alongside.
    """
    raw = str(error)
    low = raw.lower()
    code = _extract_return_code(raw)

    if code in _RETURN_CODE_REASONS:
        reason = _RETURN_CODE_REASONS[code]
    elif "too frequent" in low or "maximum number of remote commands" in low:
        code = 8
        reason = _RETURN_CODE_REASONS[8]
    elif "timeout" in low or "timed out" in low:
        reason = "Timed out waiting for the SAIC servers. Please try again."
    elif "front defrost blocked" in low:
        reason = (
            "Front defrost was blocked because the air conditioning is already "
            "running."
        )
    elif code is not None:
        reason = f"The command failed (SAIC code {code}). Please try again."
    else:
        reason = "The command could not be completed. Please try again."

    # Original keys kept for backward compatibility; friendly keys added.
    attrs = {
        "source": source,
        "error": raw,
        "action": _humanize_source(source),
        "reason": reason,
    }
    if code is not None:
        attrs["code"] = code
    return attrs


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the MG SAIC command error event entity."""
    coordinator = hass.data[DOMAIN][f"{entry.entry_id}_coordinator"]
    client = hass.data[DOMAIN][entry.entry_id]

    if not coordinator.data.get("info"):
        LOGGER.error("Vehicle info is not available. Event entity cannot be set up.")
        return

    vin_info = coordinator.vin_info
    vin = vin_info.vin

    async_add_entities(
        [SAICMGCommandErrorEvent(coordinator, client, entry, vin_info, vin)]
    )


class SAICMGCommandErrorEvent(CoordinatorEntity, EventEntity):
    """Event entity that surfaces remote command failures in the HA Logbook.

    This is a passive, fire-and-forget complement to the persistent
    notification raised for CommandsLimitReachedException: the notification
    is for "needs user action now", while this event entity gives a
    queryable history of every command failure (including, but not limited
    to, command-limit-reached events) for later review in the Logbook or in
    automations.

    The coordinator calls record_command_error()/record_command_limit_reached()
    on this entity directly; entities themselves don't need to know about
    this platform.
    """

    _attr_should_poll = False

    def __init__(self, coordinator, client, entry, vin_info, vin):
        """Initialize the command error event entity."""
        super().__init__(coordinator)
        self._client = client
        self._vin = vin
        self._vin_info = vin_info
        self._attr_name = f"{vin_info.brandName} {vin_info.modelName} Command Errors"
        self._attr_unique_id = f"{entry.entry_id}_{vin}_command_errors_event"
        self._attr_icon = "mdi:alert-circle-outline"
        self._attr_event_types = EVENT_TYPES
        self._attr_translation_key = "command_errors"
        self._device_info = create_device_info(coordinator, entry.entry_id)

    @property
    def device_info(self):
        """Return device info."""
        return self._device_info

    @property
    def available(self):
        """This entity is always available — it has no dependency on the
        latest coordinator poll succeeding, since it only reflects command
        outcomes, not vehicle telemetry."""
        return True

    async def async_added_to_hass(self):
        """Register this entity with the coordinator once added to HA."""
        await super().async_added_to_hass()
        self.coordinator.register_command_error_event_entity(self)

    async def async_will_remove_from_hass(self):
        """Deregister from the coordinator when removed."""
        self.coordinator.register_command_error_event_entity(None)
        await super().async_will_remove_from_hass()

    def record_command_error(self, source: str, error: str) -> None:
        """Fire a generic command_error event.

        Args:
            source: short identifier of what failed, e.g. "climate.set_hvac_mode"
            error: the error message/exception string
        """
        self._trigger_event(
            EVENT_TYPE_COMMAND_ERROR,
            _humanize_command_error(source, error),
        )
        self.async_write_ha_state()

    def record_command_limit_reached(self, source: str) -> None:
        """Fire a command_limit_reached event.

        Args:
            source: short identifier of which command triggered the limit,
                e.g. "climate.set_hvac_mode"
        """
        limit_message = (
            "Vehicle reached the maximum number of remote commands. "
            "Start the vehicle with the physical key to reset."
        )
        self._trigger_event(
            EVENT_TYPE_COMMAND_LIMIT_REACHED,
            {
                # Original keys kept for backward compatibility.
                "source": source,
                "message": limit_message,
                # Friendly keys, consistent with command_error.
                "action": _humanize_source(source),
                "reason": limit_message,
                "code": 8,
            },
        )
        self.async_write_ha_state()
