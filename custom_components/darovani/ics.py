"""Pozvánka do pracovního kalendáře jako iCalendar e-mail (pro Outlook).

Proč ne Google Kalendář: pozvánka z Googlu nese adresu kalendáře jako
odesílatele, nejde u ní nastavit „Mimo kancelář“ a připomínku si Outlook
nepřevezme. Vlastní e-mail s text/calendar (METHOD:REQUEST) tohle umí:
  * ORGANIZER s čitelným jménem,
  * X-MICROSOFT-CDO-BUSYSTATUS:OOF -> v Outlooku „Mimo kancelář“,
  * VALARM -P1D -> připomenutí den předem.
Zrušení je stejný e-mail s METHOD:CANCEL a vyšším SEQUENCE.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

PRODID = "-//kautskyjakub//ha-darovani//CS"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: řádky delší než 75 oktetů se zalamují."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode("utf-8"))
            current = b""
        current += b
    parts.append(current.decode("utf-8"))
    return "\r\n ".join(parts)


def build_ics(
    *,
    uid: str,
    sequence: int,
    day: date,
    summary: str,
    organizer_email: str,
    organizer_name: str,
    attendee_email: str,
    cancel: bool = False,
    now: datetime | None = None,
) -> str:
    """Celodenní událost „Mimo kancelář“ s připomenutím den předem."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    method = "CANCEL" if cancel else "REQUEST"
    lines = [
        "BEGIN:VCALENDAR",
        f"PRODID:{PRODID}",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        f"METHOD:{method}",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"SEQUENCE:{sequence}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_escape(summary)}",
        f'ORGANIZER;CN="{_escape(organizer_name)}":mailto:{organizer_email}',
        f"ATTENDEE;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=FALSE:mailto:{attendee_email}",
        "CLASS:PRIVATE",
        "TRANSP:OPAQUE",
        f"STATUS:{'CANCELLED' if cancel else 'CONFIRMED'}",
        "X-MICROSOFT-CDO-BUSYSTATUS:OOF",
        "X-MICROSOFT-CDO-INTENDEDSTATUS:OOF",
        "X-MICROSOFT-CDO-ALLDAYEVENT:TRUE",
        "X-MICROSOFT-DISALLOW-COUNTER:TRUE",
    ]
    if not cancel:
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(summary)}",
            "TRIGGER:-P1D",
            "END:VALARM",
        ]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def build_invite_mail(
    *,
    ics: str,
    subject: str,
    text: str,
    from_email: str,
    from_name: str,
    to_email: str,
    cancel: bool = False,
) -> str:
    """MIME e-mail s pozvánkou; vrací base64url pro Gmail API (pole raw)."""
    method = "CANCEL" if cancel else "REQUEST"
    msg = EmailMessage()
    msg["From"] = formataddr((from_name, from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_email.split("@")[-1])
    msg.set_content(text)
    msg.add_alternative(ics, subtype="calendar", params={"method": method, "charset": "UTF-8"})
    msg.add_attachment(
        ics.encode("utf-8"),
        maintype="application",
        subtype="ics",
        filename="invite.ics",
    )
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
