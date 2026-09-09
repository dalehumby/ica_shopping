import logging
from homeassistant.helpers.storage import Store
from homeassistant.helpers.event import async_call_later
from .const import DOMAIN, DATA_ICA
from homeassistant.helpers import entity_registry

from .ica_api import ICAApi

_LOGGER = logging.getLogger(__name__)

async def _trigger_sensor_update(hass, list_id):
    registry = entity_registry.async_get(hass)  # ✅ utan await
    target_unique_id = f"shoppinglist_{list_id}"
    sensor_entity = None

    for entity in registry.entities.values():
        if entity.unique_id == target_unique_id:
            sensor_entity = entity.entity_id
            break

    if not sensor_entity:
        _LOGGER.debug("ℹ️ Kunde inte hitta sensor med unique_id %s", target_unique_id)
        return

    _LOGGER.debug("🔁 Triggar update för %s", sensor_entity)
    await hass.services.async_call(
        "homeassistant", "update_entity",
        {"entity_id": sensor_entity},
        blocking=True
    )

STORAGE_VERSION = 1
STORAGE_KEY = "ica_keep_synced_list"
MAX_ICA_ITEMS = 250
MAX_KEEP_ITEMS = 100
DEBOUNCE_SECONDS = 1

async def async_setup(hass, config):
    return True

async def async_setup_entry(hass, entry):
    _LOGGER.debug("⚙️ ICA Shopping initieras via UI config entry")
    session_id = entry.options.get("session_id", entry.data["session_id"])
    list_id = entry.options.get("ica_list_id", entry.data["ica_list_id"])
    api = ICAApi(hass, session_id=session_id, entry_id=entry.entry_id)
    hass.data.setdefault(DOMAIN, {})[DATA_ICA] = api
    hass.data[DOMAIN]["current_list_id"] = list_id  # 🔁 spara aktuellt list ID
    keep_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
    if not keep_entity:
        _LOGGER.error("❌ Ingen todo-entity vald – ICA-integrationen kan inte synka utan en källa.")
        return False

 
    # --- Keep → ICA debounce sync ---
    debounce_unsub = None

    async def schedule_sync(_now=None):
        nonlocal debounce_unsub
        debounce_unsub = None
        list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
        keep_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))


        _LOGGER.debug("🔁 Debounced Keep → ICA sync")
        try:
            result = await hass.services.async_call(
                "todo", "get_items",
                {"entity_id": keep_entity},
                blocking=True, return_response=True
            )

            items = result.get(keep_entity, {}).get("items", [])
            summaries = [i.get("summary", "").strip() for i in items if isinstance(i, dict)]
            if len(summaries) > MAX_KEEP_ITEMS:
                summaries = summaries[:MAX_KEEP_ITEMS]

            lists = await api.fetch_lists()
            rows = next((l.get("rows", []) for l in lists if l.get("id") == list_id), [])
            if len(rows) >= MAX_ICA_ITEMS:
                _LOGGER.error("🚫 ICA-listan full (%s). Inga varor tillagda.", len(rows))
                return

            existing = [r.get("text", "").strip().lower() for r in rows if isinstance(r, dict)]
            space = MAX_ICA_ITEMS - len(rows)
            to_add = [s for s in summaries if s.lower() not in existing][:space]
            
            any_added = False
            for text in to_add:
                success = await api.add_to_list(list_id, text)
                if success:
                    _LOGGER.info("📥 Lade till '%s' i ICA", text)
                    any_added = True

            if any_added:
                await _trigger_sensor_update(hass, list_id)
                    
                    
        except Exception as e:
            _LOGGER.error("💥 Fel vid sync_keep_to_ica: %s", e)

    def call_service_listener(event):
        nonlocal debounce_unsub
        data = event.data.get("service_data", {})
        service = event.data.get("service")
        # Lyssna på "status: completed" via update_item
        if service == "update_item":
            status = data.get("status")
            text = data.get("rename")  # detta är varunamnet
            if status == "completed" and text:
                item = text.strip().lower()
                hass.data[DOMAIN].setdefault("recent_keep_removes", set()).add(item)
                _LOGGER.debug("🟡 Avlyssnad remove via update_item: %s", item)

                # 🆕 Direktborttagning från ICA
                async def remove_from_ica_direct():
                    try:
                        list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))
                        lists = await api.fetch_lists()
                        rows = next((l.get("rows", []) for l in lists if l.get("id") == list_id), [])
                        ica_rows_dict = {
                            row.get("text", "").strip().lower(): row.get("id")
                            for row in rows if isinstance(row, dict)
                        }
                        row_id = ica_rows_dict.get(item)
                        if row_id:
                            await api.remove_item(row_id)
                            _LOGGER.info("❌ Direkt borttagning av '%s' från ICA (pga completed i Keep)", item)
                    except Exception as e:
                        _LOGGER.error("💥 Fel vid direkt ICA-radering: %s", e)

                hass.async_create_task(remove_from_ica_direct())

                # Planera även en sync mot Keep
                if debounce_unsub:
                    debounce_unsub()
                debounce_unsub = async_call_later(hass, DEBOUNCE_SECONDS, schedule_sync)
      
        entity_ids = data.get("entity_id", [])
        keep_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
        item = data.get("item")
        if isinstance(item, str):
            item = item.strip().lower()
        elif isinstance(item, list) and item:   
            item = item[0].strip().lower()  # plockar första om flera finns
        else:
            item = None

        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        if keep_entity not in entity_ids or not item:
            return

        # Spåra senaste add/remove från Keep
        if service == "add_item":
            hass.data[DOMAIN].setdefault("recent_keep_adds", set()).add(item)
            _LOGGER.debug("📌 Noterat 'add_item' i Keep: %s", item)

        elif service == "remove_item":
            hass.data[DOMAIN].setdefault("recent_keep_removes", set()).add(item)
            _LOGGER.debug("📌 Noterat 'remove_item' i Keep: %s", item)

        if debounce_unsub:
            debounce_unsub()
        debounce_unsub = async_call_later(hass, DEBOUNCE_SECONDS, schedule_sync)


    entry.async_on_unload(hass.bus.async_listen("call_service", call_service_listener))

    # --- Registrera refresh-tjänst ---
    async def handle_refresh(call):

        _LOGGER.debug("🔄 ICA refresh triggered via service")
        try:
            remove_striked = entry.options.get("remove_striked", True)
            keep_entity = entry.options.get("todo_entity_id", entry.data.get("todo_entity_id"))
            list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))

            # Baseline of items we last confirmed were actually in ICA. Used
            # below to tell "removed in ICA" (was in baseline, now gone) apart
            # from "added in Keep but never synced yet" (never in baseline) —
            # e.g. added while the session token was expired. Without this,
            # any Keep item not yet mirrored to ICA looks identical to one
            # that was deleted in the ICA app, and gets wiped from Keep too.
            baseline_store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}_{list_id}")
            known_ica_items = set(await baseline_store.async_load() or [])

            lists = await api.fetch_lists()
            the_list = next((l for l in lists if l.get("id") == list_id), None)
            if not the_list:
                _LOGGER.warning("❌ Kunde inte hitta ICA-lista %s", list_id)
                return
                
            rows = the_list.get("rows", [])

            if remove_striked:
                checked_rows = [r for r in rows if r.get("isStriked") is True and r.get("id")]
                for r in checked_rows:
                    await api.remove_item(r["id"])
                    _LOGGER.info("🧹 Rensade avbockad vara '%s' från ICA", r.get("text", ""))
                rows = [r for r in rows if r.get("id") not in [cr["id"] for cr in checked_rows]]
            
            
            if len(rows) >= MAX_ICA_ITEMS:
                _LOGGER.error("🚫 ICA-listan är full (%s varor). Refresh stoppad.", len(rows))
                return

            ica_items = [row.get("text", "").strip() for row in rows if isinstance(row, dict)]
            ica_items_lower = [x.lower() for x in ica_items]
            ica_rows_dict = {
                row.get("text", "").strip().lower(): row.get("id")
                for row in rows if isinstance(row, dict)
            }

            result = await hass.services.async_call(
                "todo", "get_items",
                {"entity_id": keep_entity},
                blocking=True, return_response=True
            )
            keep_items = result.get(keep_entity, {}).get("items", [])
            keep_summaries = [i.get("summary", "").strip() for i in keep_items if isinstance(i, dict)]
            keep_lower = [x.lower() for x in keep_summaries]


            # 1️⃣ Hitta completed-items i Keep som fortfarande finns i ICA
            keep_completed = [
                i.get("summary", "").strip().lower()
                for i in keep_items
                if i.get("status") == "completed"
            ]


            # ❌ Radera completed från Keep – endast om remove_striked är aktivt
            if remove_striked:
                for text in keep_completed:
                    await hass.services.async_call(
                        "todo", "remove_item",
                        {"entity_id": keep_entity, "item": text}
                    )
                    _LOGGER.info("🧹 Tog bort '%s' från Keep (pga status: completed + remove_striked)", text)


            # 2️⃣ Lägg till dem i listan att radera från ICA
            for text in keep_completed:
                row_id = ica_rows_dict.get(text)
                if row_id:
                    await api.remove_item(row_id)
                    _LOGGER.info("❌ Tog bort '%s' från ICA (baserat på Keep: completed)", text)




            # Hämta senaste ändringar från Keep
            recent_adds = hass.data[DOMAIN].setdefault("recent_keep_adds", set())
            recent_removes = hass.data[DOMAIN].setdefault("recent_keep_removes", set())

            # Lägg till i Keep det som saknas i Keep, och som INTE nyss tagits bort
            to_add = []
            for item in ica_items:
                key = item.lower()
                if key not in keep_lower and key not in recent_removes:
                    _LOGGER.debug("➕ Planerar att lägga till i Keep: %s", item)
                    to_add.append(item)
                else:
                    _LOGGER.debug("⛔ Hoppar över '%s' – finns i recent_removes eller redan i Keep", item)

            
            max_add = MAX_ICA_ITEMS - len(keep_items)
            to_add = to_add[:max_add]

            for item in to_add:
                await hass.services.async_call(
                    "todo", "add_item",
                    {"entity_id": keep_entity, "item": item}
                )
                _LOGGER.info("✅ Lagt till '%s' i Keep", item)

            # 3️⃣ Lägg till i ICA det som finns aktivt i Keep men saknas i ICA
            # (t.ex. varor tillagda i Keep medan sessionen var utgången, som
            # den vanliga debounce-synken aldrig lyckades pusha vidare).
            # known_ica_items utesluts här: om varan tidigare bekräftats finnas
            # i ICA och nu saknas betyder det att den bockats av/tagits bort i
            # ICA (t.ex. skannad i kassan) – då ska den INTE återskapas i ICA,
            # bara tas bort ur Keep (se to_remove_from_keep nedan).
            to_push_to_ica = [
                s for s in keep_summaries
                if s.strip().lower() not in keep_completed
                and s.strip().lower() not in ica_items_lower
                and s.strip().lower() not in recent_removes
                and s.strip().lower() not in known_ica_items
            ]
            space = MAX_ICA_ITEMS - len(rows)
            to_push_to_ica = to_push_to_ica[:max(space, 0)]

            for text in to_push_to_ica:
                success = await api.add_to_list(list_id, text)
                if success:
                    _LOGGER.info("📤 (refresh) Lade till '%s' i ICA", text)

            # Ta bort från Keep det som inte finns i ICA – men bara om vi
            # tidigare bekräftat att varan faktiskt fanns i ICA (known_ica_items).
            # Annars kan en vara som lagts till i Keep men ännu inte hunnit
            # synkas till ICA (t.ex. under en utgången session) felaktigt tolkas
            # som borttagen i ICA och raderas ur Keep. Vid osäkerhet: behåll.
            to_remove_from_keep = [
                i.get("summary") for i in keep_items
                if i.get("summary", "").strip().lower() not in ica_items_lower
                and i.get("summary", "").strip().lower() in known_ica_items
            ]

            for summary in to_remove_from_keep:
                if summary:
                    await hass.services.async_call(
                        "todo", "remove_item",
                        {"entity_id": keep_entity, "item": summary}
                    )
                    _LOGGER.info("🗑️ Tagit bort '%s' från Keep", summary)

            # Ta bort från ICA om det just tagits bort i Keep
            to_remove_from_ica = [item for item in recent_removes if item in ica_rows_dict]

            for text in to_remove_from_ica:
                row_id = ica_rows_dict.get(text)
                if row_id:
                    await api.remove_item(row_id)
                    _LOGGER.info("❌ Tog bort '%s' från ICA (baserat på Keep-radering)", text)

            # Uppdatera sensor
            await _trigger_sensor_update(hass, list_id)

            # Spara ny baseline: hämta det faktiska ICA-läget efter alla
            # ändringar ovan, så nästa refresh kan skilja på en riktig
            # borttagning i ICA och en vara som bara inte synkats än.
            final_lists = await api.fetch_lists()
            final_list = next((l for l in final_lists if l.get("id") == list_id), None)
            if final_list:
                final_ica_set = {
                    r.get("text", "").strip().lower()
                    for r in final_list.get("rows", [])
                    if isinstance(r, dict)
                }
                await baseline_store.async_save(list(final_ica_set))

            # Rensa eventspårning efter allt är klart
            if "recent_keep_adds" in hass.data[DOMAIN]:
                hass.data[DOMAIN]["recent_keep_adds"].clear()
            if "recent_keep_removes" in hass.data[DOMAIN]:
                hass.data[DOMAIN]["recent_keep_removes"].clear()


        except Exception as e:
            _LOGGER.error("💥 Fel vid refresh: %s", e)


    hass.services.async_register(DOMAIN, "refresh", handle_refresh)

    # --- Ladda sensorer (korrekt sätt) ---
    await hass.config_entries.async_forward_entry_setups(entry, ["sensor"])

    # --- Reload vid options-ändring ---
    entry.async_on_unload(entry.add_update_listener(_options_update_listener))

    return True

async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, ["sensor"])

async def _options_update_listener(hass, entry):
    _LOGGER.debug("♻️ Optioner har ändrats, laddar om entry")

    prev_list_id = hass.data[DOMAIN].get("current_list_id")
    new_list_id = entry.options.get("ica_list_id", entry.data.get("ica_list_id"))

    if prev_list_id and prev_list_id != new_list_id:
        _LOGGER.warning("⚠️ List ID changed from %s to %s – this may cause syncing of previous Keep items to the new ICA list.", prev_list_id, new_list_id)

    hass.data[DOMAIN]["current_list_id"] = new_list_id

    await hass.config_entries.async_reload(entry.entry_id)
