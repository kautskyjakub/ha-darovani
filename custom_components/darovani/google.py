"""Google Kalendář + Gmail přes OAuth účet propojený v integraci.

Kalendář: vytvoření / úprava / smazání události odběru. Každá událost nese
v `extendedProperties.private` značku `darovani_id`, takže ji integrace
pozná i po restartu a nevytváří duplicity.

Gmail: jen čtení e-mailů od nis-noreply@fnusa.cz (potvrzení a storna),
odkud se bere odkaz pro úpravu/zrušení rezervace.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow

from .const import FNUSA_MAIL_SENDER

_LOGGER = logging.getLogger(__name__)

CAL_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GoogleError(Exception):
    """Chyba volání Google API."""


class GoogleClient:
    def __init__(self, session: config_entry_oauth2_flow.OAuth2Session, calendar_id: str) -> None:
        self._session = session
        self.calendar_id = calendar_id or "primary"

    async def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        await self._session.async_ensure_token_valid()
        resp = await self._session.async_request(method, url, **kwargs)
        if resp.status == 204:
            return None
        if resp.status == 410 and method == "DELETE":
            return None  # už smazáno
        if resp.status >= 400:
            text = await resp.text()
            raise GoogleError(f"Google API {method} {url.split('?')[0]} -> {resp.status}: {text[:300]}")
        return await resp.json()

    # --- Kalendář ---------------------------------------------------------

    def _cal_url(self, suffix: str = "") -> str:
        return f"{CAL_API}/calendars/{self.calendar_id}/events{suffix}"

    async def create_event(self, body: dict[str, Any]) -> str:
        data = await self._call("POST", self._cal_url(), json=body)
        return data["id"]

    async def patch_event(self, event_id: str, body: dict[str, Any]) -> None:
        await self._call("PATCH", self._cal_url(f"/{event_id}"), json=body)

    async def delete_event(self, event_id: str) -> None:
        try:
            await self._call("DELETE", self._cal_url(f"/{event_id}"))
        except GoogleError as err:
            if "404" in str(err):
                return
            raise

    async def find_events(self, start: datetime, end: datetime, query: str | None = None) -> list[dict[str, Any]]:
        params = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "maxResults": "50",
        }
        if query:
            params["q"] = query
        data = await self._call("GET", self._cal_url(), params=params)
        return data.get("items", [])

    async def find_by_marker(self, darovani_id: str) -> dict[str, Any] | None:
        data = await self._call(
            "GET",
            self._cal_url(),
            params={"privateExtendedProperty": f"darovani_id={darovani_id}", "maxResults": "5"},
        )
        items = [i for i in data.get("items", []) if i.get("status") != "cancelled"]
        return items[0] if items else None

    async def find_matching(self, start: datetime, title_part: str = "Darování") -> dict[str, Any] | None:
        """Najde existující (ručně vytvořenou) událost začínající v daný čas."""
        items = await self.find_events(start - timedelta(minutes=1), start + timedelta(minutes=1), title_part)
        for item in items:
            st = item.get("start", {}).get("dateTime")
            if st and datetime.fromisoformat(st) == start and title_part.lower() in item.get("summary", "").lower():
                return item
        return None

    # --- Gmail ------------------------------------------------------------

    async def list_fnusa_messages(self, newer_than_days: int = 400) -> list[str]:
        ids: list[str] = []
        page: str | None = None
        for _ in range(5):
            params = {"q": f"from:{FNUSA_MAIL_SENDER} newer_than:{newer_than_days}d", "maxResults": "100"}
            if page:
                params["pageToken"] = page
            data = await self._call("GET", f"{GMAIL_API}/messages", params=params)
            ids.extend(m["id"] for m in data.get("messages", []))
            page = data.get("nextPageToken")
            if not page:
                break
        return ids

    async def get_message(self, message_id: str) -> tuple[str, str, datetime | None]:
        """Vrátí (předmět, čistý text, čas přijetí)."""
        data = await self._call("GET", f"{GMAIL_API}/messages/{message_id}", params={"format": "full"})
        payload = data.get("payload", {})
        subject = next(
            (h["value"] for h in payload.get("headers", []) if h.get("name", "").lower() == "subject"), ""
        )
        received = None
        if data.get("internalDate"):
            received = datetime.fromtimestamp(int(data["internalDate"]) / 1000).astimezone()
        return subject, _extract_text(payload), received


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _extract_text(payload: dict[str, Any]) -> str:
    """Najde text/plain část; když chybí, převede HTML na hrubý text."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}).get("data")
        if body and mime == "text/plain":
            plain.append(_decode(body))
        elif body and mime == "text/html":
            html.append(_decode(body))
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain)
    if html:
        import re

        text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", "\n".join(html), flags=re.I)
        text = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>.*?</a>", r"\1", text, flags=re.I | re.S)
        return re.sub(r"<[^>]+>", "", text)
    return ""
