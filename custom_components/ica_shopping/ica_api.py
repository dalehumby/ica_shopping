"""Client for ICA's shopping-list API.

Every call raises ICAError on failure instead of returning an empty
result: an unreachable ICA must never look like an empty list, or the
sync would act on it (that is how outages used to re-push every item).
"""

import logging
import time

import aiohttp

from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    API_ADD_ROW,
    API_LIST_ALL,
    API_ROW,
    API_USER_INFO,
    DOMAIN,
    signal_session_updated,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
# The bearer token from user/information is reused instead of being fetched
# before every request; it is refreshed on a 401 or after this many seconds.
TOKEN_MAX_AGE = 15 * 60


class ICAError(Exception):
    """ICA could not be reached or answered with an error."""


class ICAAuthError(ICAError):
    """The thSessionId cookie is no longer logged in."""


class ICANotFoundError(ICAError):
    """The requested ICA resource does not exist."""


class ICAApi:
    def __init__(self, hass, session_id, entry_id):
        self.hass = hass
        self.session_id = session_id
        self.entry_id = entry_id
        self.session_valid: bool | None = None
        self._http = async_get_clientsession(hass)
        self._token: str | None = None
        self._token_time = 0.0

    def _set_session_valid(self, valid: bool) -> None:
        if valid == self.session_valid:
            return
        self.session_valid = valid
        if valid:
            ir.async_delete_issue(self.hass, DOMAIN, "invalid_session_id")
        else:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "invalid_session_id",
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key="invalid_session_id",
                data={"entry_id": self.entry_id},
            )
        async_dispatcher_send(self.hass, signal_session_updated(self.entry_id), valid)

    async def _fetch_token(self) -> str:
        headers = {"Cookie": f"thSessionId={self.session_id}", "Accept": "application/json"}
        try:
            async with self._http.get(API_USER_INFO, headers=headers, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status in (401, 403):
                    self._set_session_valid(False)
                    raise ICAAuthError(f"user/information returned {resp.status}")
                if resp.status != 200:
                    raise ICAError(f"user/information returned {resp.status}")
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ICAError(f"user/information failed: {err}") from err

        token = data.get("accessToken") if isinstance(data, dict) else None
        if not token or not data.get("loginState"):
            # ICA answers 200 with an anonymous token once thSessionId has
            # expired (loginState: 0); shopping-list calls with it fail.
            self._set_session_valid(False)
            raise ICAAuthError(f"ICA session is not logged in (loginState={data.get('loginState')})")

        self._set_session_valid(True)
        return token

    async def _request(self, method: str, url: str, body: dict | None = None):
        for attempt in (1, 2):
            if not self._token or time.monotonic() - self._token_time > TOKEN_MAX_AGE:
                self._token = await self._fetch_token()
                self._token_time = time.monotonic()

            headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
            try:
                async with self._http.request(
                    method, url, headers=headers, json=body, timeout=REQUEST_TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 1:
                        self._token = None
                        continue
                    if resp.status == 404:
                        raise ICANotFoundError(f"{method} {url} returned 404")
                    if resp.status >= 400:
                        text = (await resp.text())[:200]
                        raise ICAError(f"{method} {url} returned {resp.status}: {text}")
                    if not (await resp.read()):
                        return None
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as err:
                raise ICAError(f"{method} {url} failed: {err}") from err
        raise ICAError(f"{method} {url} still unauthorised after a token refresh")

    async def async_get_list(self, list_id: str) -> dict:
        """Return the ICA list with its rows. Raises if it can't be read."""
        result = await self._request("GET", API_LIST_ALL)
        lists = result.get("items") if isinstance(result, dict) else result
        if not isinstance(lists, list):
            raise ICAError(f"unexpected list/all response: {type(result).__name__}")
        for lst in lists:
            if isinstance(lst, dict) and lst.get("id") == list_id:
                return lst
        raise ICANotFoundError(f"ICA list {list_id} not found")

    async def async_add_row(self, list_id: str, text: str) -> dict:
        """Add a row and return it, including the id ICA assigned."""
        row = await self._request("POST", API_ADD_ROW.format(list_id=list_id), {"text": text})
        if not isinstance(row, dict) or not row.get("id"):
            raise ICAError(f"adding '{text}' returned no row id")
        return row

    async def async_update_row(self, row: dict, text: str) -> dict:
        """Change a row's text, keeping its struck state."""
        updated = await self._request(
            "PUT",
            API_ROW.format(row_id=row["id"]),
            {"text": text, "isStriked": bool(row.get("isStriked"))},
        )
        return updated if isinstance(updated, dict) else {**row, "text": text}

    async def async_delete_row(self, row_id: str) -> None:
        """Delete a row. A row that is already gone counts as deleted."""
        try:
            await self._request("DELETE", API_ROW.format(row_id=row_id))
        except ICANotFoundError:
            _LOGGER.debug("ICA row %s was already gone", row_id)
