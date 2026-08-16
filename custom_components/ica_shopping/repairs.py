"""Repairs for the ICA Shopping integration."""

import voluptuous as vol

from homeassistant.components.repairs import (
    ConfirmRepairFlow,
    RepairsFlow,
    RepairsFlowResult,
)
from homeassistant.core import HomeAssistant


class InvalidSessionIdRepairFlow(RepairsFlow):
    """Let the user paste a fresh ICA session cookie without deleting the entry."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> RepairsFlowResult:
        if user_input is not None:
            entry_id = self.data["entry_id"]
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None:
                return self.async_abort(reason="entry_not_found")
            self.hass.config_entries.async_update_entry(
                entry,
                options={**entry.options, "session_id": user_input["session_id"]},
            )
            await self.hass.config_entries.async_reload(entry_id)
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required("session_id"): str}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str] | None,
) -> RepairsFlow:
    """Create the fix flow for an ICA Shopping issue."""
    if issue_id == "invalid_session_id" and data and data.get("entry_id"):
        return InvalidSessionIdRepairFlow()
    return ConfirmRepairFlow()
