# File: repairs.py
"""Fix flow for the "turn on real-time push" Repair item (see push_issue.py).

New cars have push on from the start. A car added earlier has no setting for
it, which means off, so that an upgrade changes nothing by itself. The item
tells its owner the option is there and turns it on with one click.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow

from .const import CONF_PUSH_NOTIFICATIONS


class EnablePushRepairFlow(RepairsFlow):
    """One click to switch push on for the car."""

    def __init__(self, entry_id: str) -> None:
        self._entry_id = entry_id

    async def async_step_init(self, user_input=None):
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input=None):
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
            if entry is not None:
                # Updating the options reloads the car, which connects push.
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_PUSH_NOTIFICATIONS: True}
                )
            return self.async_create_entry(data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(hass, issue_id_: str, data) -> RepairsFlow:
    return EnablePushRepairFlow((data or {}).get("entry_id", ""))
