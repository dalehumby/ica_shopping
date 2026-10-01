"""ICA Shopping: mirror a to-do list (e.g. Google Keep) into an ICA shopping list.

The to-do list is the source of truth: items are added, read and corrected
there (voice, Nest Hub, the HA app). The ICA list is a copy of its open items
for the self-checkout scanner. The only thing read back from ICA is that a row
was struck off (scanned) or deleted, which means bought: the to-do item is
removed.

To-do items and ICA rows are linked by ID (to-do uid -> ICA row id) and the
links are persisted, so duplicate names, renames and letter case never confuse
the sync. Every change, whatever triggered it, goes through one reconcile pass
under one lock.
"""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import timedelta

from homeassistant.components.todo import TodoItemStatus
from homeassistant.components.todo.const import DATA_COMPONENT as TODO_DATA_COMPONENT
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import Store

from .const import DOMAIN, signal_list_updated
from .ica_api import ICAApi, ICAError

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["sensor"]

STORAGE_VERSION = 1
LINKS_STORAGE_KEY = "ica_shopping_links"
OLD_BASELINE_STORAGE_KEY = "ica_keep_synced_list"
DEBOUNCE_SECONDS = 1
SYNC_INTERVAL = timedelta(minutes=10)
MAX_ICA_ROWS = 250
WIPE_GUARD_MIN_LINKS = 3
# An ICA row adopted into the to-do list is linked when its new to-do item
# shows up; if it never does (the to-do write failed), adopt it again.
PENDING_ADOPTION_TIMEOUT = 60 * 60


async def async_setup(hass: HomeAssistant, config) -> bool:
    async def async_handle_refresh(call: ServiceCall) -> None:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            # Registered here rather than per entry so that a disabled or
            # failed integration is an explicit error, not an unknown action
            # that only appears after the next restart.
            raise HomeAssistantError("ICA Shopping is not loaded (disabled or failed to set up)")
        for entry in entries:
            await entry.runtime_data.async_reconcile(raise_errors=True)

    hass.services.async_register(DOMAIN, "refresh", async_handle_refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry) -> bool:
    keep_entity_id = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    if not keep_entity_id:
        _LOGGER.error("No to-do entity configured; ICA Shopping has nothing to sync from")
        return False

    session_id = entry.options.get("session_id", entry.data["session_id"])
    api = ICAApi(hass, session_id=session_id, entry_id=entry.entry_id)
    sync = ICASync(hass, entry, api, keep_entity_id)
    entry.runtime_data = sync

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    sync.async_start()
    entry.async_on_unload(sync.async_stop)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_options_updated(hass: HomeAssistant, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


class ICASync:
    """Keeps one ICA list in line with one to-do list."""

    def __init__(self, hass: HomeAssistant, entry, api: ICAApi, keep_entity_id: str) -> None:
        self.hass = hass
        self.entry = entry
        self.api = api
        self.keep_entity_id = keep_entity_id
        self.list_id = entry.options.get("ica_list_id", entry.data["ica_list_id"])
        self.remove_struck = entry.options.get("remove_striked", entry.data.get("remove_striked", True))

        self._store = Store(hass, STORAGE_VERSION, f"{LINKS_STORAGE_KEY}_{entry.entry_id}_{self.list_id}")
        # to-do uid -> {"row_id": ICA row id, "text": text last pushed to ICA}
        self._links: dict[str, dict] | None = None
        # ICA row id -> {"text": lowercased text, "since": epoch} for rows
        # added in the ICA app and copied to the to-do list, awaiting the uid.
        self._pending: dict[str, dict] = {}

        self._lock = asyncio.Lock()
        # Not helpers.debounce.Debouncer: it drops a call that comes due while
        # the previous one is still running ("any call is good"), but a sync
        # reads the to-do list when it starts, so items added mid-sync were
        # missed. A plain timer that queues on the lock always gets a fresh run.
        self._unsub_debounce = None
        self._unsubs: list = []
        self._unsub_items = None
        self._subscribed_entity = None
        self._last_signature = None

    # --- triggers -----------------------------------------------------------

    @callback
    def async_start(self) -> None:
        # Item-level updates (incl. renames, which don't change the entity's
        # state) come from the to-do entity's subscription; the state listener
        # re-subscribes when the to-do integration reloads its entity.
        self._unsubs.append(
            async_track_state_change_event(self.hass, [self.keep_entity_id], self._async_keep_state_changed)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._async_interval, SYNC_INTERVAL))
        self._unsubs.append(async_at_started(self.hass, self._async_hass_started))

    @callback
    def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._unsub_items:
            self._unsub_items()
            self._unsub_items = None
        self._subscribed_entity = None
        if self._unsub_debounce:
            self._unsub_debounce()
            self._unsub_debounce = None

    async def _async_hass_started(self, _hass) -> None:
        self._async_attach()
        await self.async_reconcile()

    async def _async_interval(self, _now) -> None:
        await self.async_reconcile()

    async def _async_debounced(self, _now) -> None:
        self._unsub_debounce = None
        await self.async_reconcile()

    def _get_keep_entity(self):
        component = self.hass.data.get(TODO_DATA_COMPONENT)
        return component.get_entity(self.keep_entity_id) if component else None

    @callback
    def _async_attach(self) -> None:
        entity = self._get_keep_entity()
        if entity is self._subscribed_entity:
            return
        if self._unsub_items:
            self._unsub_items()
            self._unsub_items = None
        self._subscribed_entity = entity
        if entity is not None:
            self._unsub_items = entity.async_subscribe_updates(self._async_items_updated)

    @callback
    def _async_keep_state_changed(self, event: Event[EventStateChangedData]) -> None:
        self._async_attach()
        entity = self._subscribed_entity
        self._async_items_updated(entity.todo_items if entity else None)

    @callback
    def _async_items_updated(self, items) -> None:
        # Called on every write of the to-do entity, including polls that
        # changed nothing; only an actual change to the items triggers a sync.
        signature = (
            None
            if items is None
            else tuple(sorted((i.uid or "", i.summary or "", str(i.status)) for i in items))
        )
        if signature == self._last_signature:
            return
        self._last_signature = signature
        if self._unsub_debounce:
            self._unsub_debounce()
        self._unsub_debounce = async_call_later(self.hass, DEBOUNCE_SECONDS, self._async_debounced)

    # --- reconcile ----------------------------------------------------------

    async def async_reconcile(self, raise_errors: bool = False) -> None:
        async with self._lock:
            try:
                await self._async_reconcile()
            except (ICAError, HomeAssistantError) as err:
                if raise_errors:
                    raise HomeAssistantError(f"ICA sync failed: {err}") from err
                _LOGGER.warning("ICA sync skipped: %s", err)
            except Exception:
                if raise_errors:
                    raise
                _LOGGER.exception("ICA sync failed")

    def _keep_items(self) -> dict:
        self._async_attach()
        entity = self._subscribed_entity
        state = self.hass.states.get(self.keep_entity_id)
        items = entity.todo_items if entity else None
        if items is None or state is None or state.state in ("unavailable", "unknown"):
            # Unknown is not empty: syncing against it would delete every row.
            raise HomeAssistantError(f"{self.keep_entity_id} is not available")
        return {i.uid: i for i in items if i.uid}

    async def _async_reconcile(self) -> None:
        ica_list = await self.api.async_get_list(self.list_id)
        rows = {r["id"]: r for r in ica_list.get("rows") or [] if isinstance(r, dict) and r.get("id")}
        keep = self._keep_items()
        links = await self._async_load_links(keep, rows)

        # Guards against a glitch on either side wiping the other. Scanning at
        # the checkout strikes rows one by one and the to-do list empties item
        # by item, so a whole side vanishing at once with several links still
        # in place is treated as suspect, never as "everything bought/deleted".
        # A single last item going is normal (e.g. deleting the one misheard
        # item left on the list) and is synced as usual.
        if len(links) >= WIPE_GUARD_MIN_LINKS and not keep:
            # Wait: the next non-empty to-do list resolves it either way
            # (items still missing then are deleted from ICA as usual).
            _LOGGER.warning("%s is empty but %s items are linked; not syncing", self.keep_entity_id, len(links))
            return
        if len(links) >= WIPE_GUARD_MIN_LINKS and not rows:
            # Re-push the to-do list rather than clearing it.
            _LOGGER.warning("ICA list is empty but %s items are linked; re-adding them", len(links))
            links.clear()
            await self._async_save()

        # 1. Linked pairs: did either side change? A pair that is settled here
        # leaves `keep` too, or step 2 would see its to-do item (read before
        # it was removed) as new and push it straight back into ICA.
        for uid, link in list(links.items()):
            row = rows.get(link["row_id"])
            item = keep.get(uid)

            if row is None or row.get("isStriked"):
                # Struck off at the checkout (or deleted in ICA): bought.
                if row is not None and self.remove_struck:
                    await self.api.async_delete_row(row["id"])
                    del rows[row["id"]]
                if item is not None:
                    await self._async_finish_keep_item(item)
                    _LOGGER.info("Bought in ICA, cleared from to-do list: %s", item.summary)
                del links[uid]
                keep.pop(uid, None)
                await self._async_save()
                continue

            if item is None or item.status == TodoItemStatus.COMPLETED:
                # Deleted or ticked off in the to-do list, e.g. a misheard
                # item removed on the Nest Hub.
                await self.api.async_delete_row(row["id"])
                del rows[row["id"]]
                if item is not None and self.remove_struck:
                    await self._async_remove_keep_item(uid)
                _LOGGER.info("Removed from to-do list, deleted from ICA: %s", link["text"])
                del links[uid]
                keep.pop(uid, None)
                await self._async_save()
                continue

            text = (item.summary or "").strip()
            if text and text != link["text"]:
                # Renamed in the to-do list (e.g. fixing a misheard word).
                rows[row["id"]] = await self.api.async_update_row(row, text)
                _LOGGER.info("Renamed in ICA: %s -> %s", link["text"], text)
                link["text"] = text
                await self._async_save()

        # 2. To-do items with no ICA row yet.
        for uid, item in keep.items():
            if uid in links:
                continue
            if item.status == TodoItemStatus.COMPLETED:
                if self.remove_struck:
                    await self._async_remove_keep_item(uid)
                continue
            text = (item.summary or "").strip()
            if not text:
                continue
            row_id = self._claim_pending(text, rows)
            if row_id is None:
                if len(rows) >= MAX_ICA_ROWS:
                    _LOGGER.error("ICA list is full (%s rows); not adding more", len(rows))
                    break
                row = await self.api.async_add_row(self.list_id, text)
                rows[row["id"]] = row
                row_id = row["id"]
                _LOGGER.info("Added to ICA: %s", text)
            links[uid] = {"row_id": row_id, "text": text}
            await self._async_save()

        # 3. ICA rows not linked to any to-do item.
        linked_rows = {link["row_id"] for link in links.values()}
        linked_texts = {link["text"].lower() for link in links.values()}
        self._pending = {rid: p for rid, p in self._pending.items() if rid in rows}
        for row_id, row in list(rows.items()):
            if row_id in linked_rows or row_id in self._pending:
                continue
            text = (row.get("text") or "").strip()
            if row.get("isStriked"):
                if self.remove_struck:
                    await self.api.async_delete_row(row_id)
                    del rows[row_id]
                continue
            if not text or text.lower() in linked_texts:
                # A duplicate of a linked row, left behind by older versions.
                await self.api.async_delete_row(row_id)
                del rows[row_id]
                _LOGGER.info("Deleted duplicate ICA row: %s", text)
                continue
            # Added in the ICA app: copy it to the to-do list. The to-do
            # service doesn't return the new uid, so the row is linked when
            # the item appears (step 2, via _claim_pending).
            self._pending[row_id] = {"text": text.lower(), "since": time.time()}
            linked_texts.add(text.lower())
            await self._async_save()
            await self.hass.services.async_call(
                "todo", "add_item", {"entity_id": self.keep_entity_id, "item": text}, blocking=True
            )
            _LOGGER.info("Added in ICA, copied to to-do list: %s", text)

        async_dispatcher_send(
            self.hass, signal_list_updated(self.entry.entry_id), {**ica_list, "rows": list(rows.values())}
        )

    def _claim_pending(self, text: str, rows: dict) -> str | None:
        key = text.lower()
        now = time.time()
        for row_id, pending in list(self._pending.items()):
            if now - pending["since"] > PENDING_ADOPTION_TIMEOUT:
                del self._pending[row_id]
            elif pending["text"] == key and row_id in rows:
                del self._pending[row_id]
                return row_id
        return None

    async def _async_finish_keep_item(self, item) -> None:
        if self.remove_struck:
            await self._async_remove_keep_item(item.uid)
        elif item.status != TodoItemStatus.COMPLETED:
            await self.hass.services.async_call(
                "todo",
                "update_item",
                {"entity_id": self.keep_entity_id, "item": item.uid, "status": "completed"},
                blocking=True,
            )

    async def _async_remove_keep_item(self, uid: str) -> None:
        try:
            await self.hass.services.async_call(
                "todo", "remove_item", {"entity_id": self.keep_entity_id, "item": [uid]}, blocking=True
            )
        except HomeAssistantError as err:
            # Already gone (e.g. removed by the to-do list's own sync).
            _LOGGER.debug("Could not remove %s from %s: %s", uid, self.keep_entity_id, err)

    # --- persistence --------------------------------------------------------

    async def _async_load_links(self, keep: dict, rows: dict) -> dict:
        if self._links is not None:
            return self._links

        data = await self._store.async_load()
        if data is not None:
            self._links = data.get("links", {})
            self._pending = data.get("pending", {})
            return self._links

        # First run (fresh install or upgrade from the name-based versions):
        # pair existing to-do items with ICA rows by name, one to one. This is
        # the only place items are matched by name.
        by_text = defaultdict(list)
        for row in rows.values():
            by_text[(row.get("text") or "").strip().lower()].append(row["id"])
        self._links = {}
        for uid, item in keep.items():
            text = (item.summary or "").strip()
            if text and by_text.get(text.lower()):
                self._links[uid] = {"row_id": by_text[text.lower()].pop(0), "text": text}
        _LOGGER.info("Linked %s existing to-do items to ICA rows by name", len(self._links))
        await self._async_save()
        await Store(
            self.hass, STORAGE_VERSION, f"{OLD_BASELINE_STORAGE_KEY}_{self.entry.entry_id}_{self.list_id}"
        ).async_remove()
        return self._links

    async def _async_save(self) -> None:
        await self._store.async_save({"links": self._links, "pending": self._pending})
