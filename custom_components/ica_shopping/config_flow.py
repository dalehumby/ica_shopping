import logging

from homeassistant.core import callback
from homeassistant.config_entries import ConfigFlow, ConfigEntry, OptionsFlow
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import selector
from homeassistant.helpers.selector import BooleanSelector

import voluptuous as vol
from typing import Any
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class ICAConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "ICAOptionsFlowHandler":
        return ICAOptionsFlowHandler()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["ica_list_id"],
                data=user_input
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("session_id"): str,
                vol.Required("ica_list_id"): str,
                vol.Optional("todo_entity_id"): selector({
                    "entity": {
                        "domain": "todo",
                        "multiple": False
                    }
                }),
                vol.Optional("remove_striked", default=True): BooleanSelector(),  # 🆕
            }),
        )

class ICAOptionsFlowHandler(OptionsFlow):
    # Do not set self.config_entry here (or define __init__ at all) — modern
    # Home Assistant core exposes config_entry as a read-only property that
    # is only resolved once the flow manager has attached self.hass/self.handler.
    # Assigning it manually raises AttributeError: property has no setter.

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        current_list_id = self.config_entry.options.get("ica_list_id", self.config_entry.data.get("ica_list_id", ""))

        if user_input is not None:
            # Show warning only if list is changed
            new_list_id = user_input.get("ica_list_id", current_list_id)
            if new_list_id != current_list_id:
                _LOGGER.warning("⚠️ ICA list changed from %s to %s. This may trigger Keep-to-ICA resync.", current_list_id, new_list_id)
            return self.async_create_entry(title="", data=user_input)

        # Only shown during editing — not persisted
        schema_dict = {
            vol.Required("session_id", default=self.config_entry.options.get("session_id", self.config_entry.data.get("session_id", ""))): str,
            vol.Required("ica_list_id", default=current_list_id): str,
            vol.Optional("todo_entity_id", default=self.config_entry.options.get("todo_entity_id", self.config_entry.data.get("todo_entity_id", ""))): selector({
                "entity": {
                    "domain": "todo",
                    "multiple": False
                }
            }),
            vol.Optional("remove_striked", default=self.config_entry.options.get("remove_striked", True)): BooleanSelector(),
        }

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "warning_text": "Changing the list may sync existing Keep items to a new ICA list. Make sure to clear or check the list first if needed."
            }
        )
