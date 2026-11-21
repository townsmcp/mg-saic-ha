# File: push_issue.py
"""The Repair item that suggests real-time push to cars set up before it existed.

Kept apart from repairs.py (which Home Assistant loads for the fix flow) so the
integration's setup does not need the Repairs component to import.
"""

from __future__ import annotations

from .backends import Feature
from .const import CONF_PUSH_NOTIFICATIONS, DOMAIN

ISSUE_PREFIX = "enable_push_"


def issue_id(entry_id: str) -> str:
    return f"{ISSUE_PREFIX}{entry_id}"


def async_sync_push_issue(hass, entry, coordinator) -> None:
    """Show the item while the car could use push but has no setting for it."""
    from homeassistant.helpers import issue_registry as ir

    undecided = (
        CONF_PUSH_NOTIFICATIONS not in entry.options
        and coordinator.backend_supports(Feature.PUSH_NOTIFICATIONS)
    )
    if undecided:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id(entry.entry_id),
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="enable_push",
            translation_placeholders={"vehicle": entry.title},
            data={"entry_id": entry.entry_id},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))


def async_remove_push_issue(hass, entry) -> None:
    from homeassistant.helpers import issue_registry as ir

    ir.async_delete_issue(hass, DOMAIN, issue_id(entry.entry_id))
