import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN, signal_list_updated, signal_session_updated

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    sync = entry.runtime_data
    list_id = sync.list_id

    # The old diagnostic sensor published the ICA bearer token as its state,
    # exposing a live credential to every dashboard and the recorder.
    ent_reg = er.async_get(hass)
    if old := ent_reg.async_get_entity_id("sensor", DOMAIN, f"ica_token_{list_id}"):
        ent_reg.async_remove(old)

    device_info = {
        "identifiers": {(DOMAIN, list_id)},
        "name": "ICA – Shopping",
        "manufacturer": "ICA",
    }
    async_add_entities([
        ShoppingListSensor(entry.entry_id, list_id, device_info),
        ICASessionSensor(entry.entry_id, list_id, device_info, sync.api),
    ])


class ShoppingListSensor(SensorEntity):
    """Number of rows on the ICA list, updated after every sync."""

    _attr_has_entity_name = True
    _attr_name = "Shoppinglist"
    _attr_native_unit_of_measurement = "items"
    _attr_should_poll = False

    def __init__(self, entry_id, list_id, device_info):
        self._entry_id = entry_id
        self._attr_unique_id = f"shoppinglist_{list_id}"
        self._attr_device_info = device_info

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_list_updated(self._entry_id), self._async_list_updated)
        )

    @callback
    def _async_list_updated(self, ica_list):
        rows = ica_list.get("rows") or []
        self._attr_native_value = len(rows)
        attributes = {"list_name": ica_list.get("name", "")}
        for i, row in enumerate(rows, start=1):
            attributes[f"vara_{i}"] = row.get("text", "")
        self._attr_extra_state_attributes = attributes
        self.async_write_ha_state()


class ICASessionSensor(SensorEntity):
    """Whether the ICA session cookie is still logged in."""

    _attr_has_entity_name = True
    _attr_name = "Session"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry_id, list_id, device_info, api):
        self._entry_id = entry_id
        self._api = api
        self._attr_unique_id = f"ica_session_{list_id}"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        if self._api.session_valid is None:
            return None
        return "valid" if self._api.session_valid else "invalid"

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, signal_session_updated(self._entry_id), self._async_session_updated)
        )

    @callback
    def _async_session_updated(self, _valid):
        self.async_write_ha_state()
