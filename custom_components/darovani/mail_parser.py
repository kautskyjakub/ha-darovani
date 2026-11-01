"""Parser e-mailů z rezervačního systému FNUSA (nis-noreply@fnusa.cz).

Potvrzení:  "Rezervace nového termínu v systému NIS: Dárci krve (TO)"
Storno:     "Storno rezervace v systému NIS: Plazmaferézy - Objednávky (TO)"

Z těla se čte termín ("Termín: pondělí 30.11.2026 6:15."), název kalendáře
a u potvrzení odkaz pro úpravu/zrušení s parametrem ReservationKey.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

PRAGUE = ZoneInfo("Europe/Prague")

CALENDAR_KIND = {
    "Dárci krve": "krev",
    "Plazmaferézy - Objednávky": "plazma",
}

MAIL_NEW = "new"
MAIL_CANCEL = "cancel"

_TERM_RE = re.compile(r"Termín:\s*\S+\s+(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})")
_CAL_RE = re.compile(r"Název kalendáře:\s*(.+?)\.\s*$", re.MULTILINE)
_KEY_RE = re.compile(r"ReservationKey=([A-Za-z0-9]+)")
_LINK_RE = re.compile(r"https://rezervace\.fnusa\.cz/Administration\?ReservationKey=[A-Za-z0-9]+")


@dataclass(frozen=True)
class MailEvent:
    """Jedna událost vyčtená z e-mailu."""

    type: str               # MAIL_NEW | MAIL_CANCEL
    kind: str               # krev | plazma
    start: datetime         # časová zóna Europe/Prague
    key: str | None         # ReservationKey (jen u potvrzení)
    admin_url: str | None   # odkaz pro úpravu / zrušení
    message_id: str | None = None


def parse_mail(subject: str, body: str, message_id: str | None = None) -> MailEvent | None:
    """Vrátí MailEvent, nebo None, když e-mail není o rezervaci odběru."""
    if "Storno rezervace" in subject or "Vaše rezervace byla zrušena" in body:
        mail_type = MAIL_CANCEL
    elif "Rezervace nového termínu" in subject or "Byl pro Vás rezervován nový termín" in body:
        mail_type = MAIL_NEW
    else:
        return None

    term = _TERM_RE.search(body)
    if not term:
        return None
    day, month, year, hour, minute = (int(x) for x in term.groups())
    start = datetime(year, month, day, hour, minute, tzinfo=PRAGUE)

    kind = None
    cal = _CAL_RE.search(body)
    if cal:
        kind = CALENDAR_KIND.get(cal.group(1).strip())
    if kind is None:
        # Záloha: podle předmětu
        for name, k in CALENDAR_KIND.items():
            if name in subject:
                kind = k
                break
    if kind is None:
        return None  # jiný kalendář FNUSA – netýká se darování

    key = admin_url = None
    if mail_type == MAIL_NEW:
        link = _LINK_RE.search(body)
        if link:
            admin_url = link.group(0)
            key_match = _KEY_RE.search(admin_url)
            key = key_match.group(1) if key_match else None

    return MailEvent(mail_type, kind, start, key, admin_url, message_id)
