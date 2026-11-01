"""Řízení celého modulu: stav, plán, rezervace, rušení, odklad, notifikace.

Jak to funguje zjednodušeně:
  * Každých ~20 minut se stáhnou volné termíny krve i plazmy.
  * Z historie a rezervací se spočítá plán na 3 měsíce dopředu.
  * Když je další krok volný k rezervaci a nemocnice už vypsala termíny,
    přijde jedna notifikace s návrhy (ranní časy mají přednost).
  * Výběr termínu spustí "tok" operací. Operace, která potřebuje SMS kód,
    si ho vyžádá a čeká, až ho uživatel zadá (notifikace nebo karta).
  * Gmail se čte kvůli odkazům pro správu rezervace a stornům;
    Google kalendář se drží v souladu s rezervacemi.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util, slugify

from . import planner
from .api import (
    AdminSession,
    FnusaClient,
    FnusaCodeRejected,
    FnusaError,
    FnusaSlotTaken,
    Person,
    Terms,
)
from .const import (
    ACTION_BOOK,
    ACTION_LATER,
    ACTION_OTP,
    ACTION_UNDO_DONE,
    CONF_BIRTH_DATE,
    CONF_BLOOD_TYPE,
    CONF_EMAIL,
    CONF_FIRST_NAME,
    CONF_INSURANCE_NO,
    CONF_LAST_NAME,
    CONF_PHONE,
    CONF_TITLE_AFTER,
    CONF_TITLE_BEFORE,
    CONF_WEIGHT,
    DEFAULT_OPTIONS,
    EVENT_LOCATION,
    EVENT_PLACE_NOTE,
    FNUSA_ADMIN_URL,
    KIND_BLOOD,
    KIND_DURATION_MIN,
    KIND_EVENT_TITLE,
    KIND_LABEL,
    KINDS,
    MAIL_POLL_FAST_MINUTES,
    MAIL_POLL_MINUTES,
    NOTIFY_GROUP,
    OPT_ALLOWED_USERS,
    OPT_BLOOD_INTERVAL_MONTHS,
    OPT_BLOOD_TO_PLASMA_DAYS,
    OPT_DASHBOARD_PATH,
    OPT_HORIZON_DAYS,
    OPT_NOTIFY_SERVICE,
    OPT_PLASMA_INTERVAL_DAYS,
    OPT_PLASMA_TO_BLOOD_DAYS,
    OPT_PREFERRED_LATEST,
    OPT_QUIET_END,
    OPT_QUIET_START,
    OPT_SLACK_DAYS,
    RES_CANCELLED,
    RES_DONE,
    RES_RESERVED,
    RES_SKIPPED,
    SIGNAL_UPDATE,
    STATUS_BOOKING,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TO_BOOK,
    STATUS_WAITING_CODE,
    STORAGE_KEY,
    STORAGE_VERSION,
    TERMS_POLL_MINUTES,
)
from .google import GoogleClient, GoogleError
from .mail_parser import MAIL_CANCEL, MAIL_NEW, parse_mail

_LOGGER = logging.getLogger(__name__)

WEEKDAYS = ["po", "út", "st", "čt", "pá", "so", "ne"]
WEEKDAYS_LONG = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]

AWAIT_BOOKING_CODE = "booking_code"
AWAIT_ADMIN_CODE = "admin_code"

TAG_OFFER = "darovani-nabidka"
TAG_CODE = "darovani-kod"
TAG_RESULT = "darovani-vysledek"


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS[d.weekday()]} {d.day}. {d.month}."


def fmt_slot(s: datetime) -> str:
    local = dt_util.as_local(s)
    return f"{fmt_day(local.date())} · {local.hour}:{local.minute:02d}"


def fmt_slot_long(s: datetime) -> str:
    local = dt_util.as_local(s)
    return f"{WEEKDAYS_LONG[local.weekday()]} {local.day}. {local.month}. v {local.hour}:{local.minute:02d}"


def _parse_time(value: str, fallback: str) -> time:
    try:
        hh, mm = (int(x) for x in (value or fallback).split(":")[:2])
        return time(hh, mm)
    except (ValueError, TypeError):
        hh, mm = (int(x) for x in fallback.split(":"))
        return time(hh, mm)


class DonationManager:
    """Jedna instance na config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        fnusa: FnusaClient,
        google: GoogleClient | None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.fnusa = fnusa
        self.google = google
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry.entry_id}")
        self.data: dict[str, Any] = {}
        self.terms: dict[str, Terms] = {}
        self.terms_updated: datetime | None = None
        self.mail_updated: datetime | None = None
        self.last_error: str | None = None
        self._nonce: tuple[str, datetime] | None = None
        self._admin: dict[str, AdminSession] = {}
        self._lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._fast_mail_until: datetime | None = None
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Nastavení a životní cyklus
    # ------------------------------------------------------------------

    @property
    def opts(self) -> dict[str, Any]:
        return {**DEFAULT_OPTIONS, **self.entry.options}

    @property
    def rules(self) -> planner.Rules:
        o = self.opts
        return planner.Rules(
            blood_interval_months=int(o[OPT_BLOOD_INTERVAL_MONTHS]),
            blood_to_plasma_days=int(o[OPT_BLOOD_TO_PLASMA_DAYS]),
            plasma_interval_days=int(o[OPT_PLASMA_INTERVAL_DAYS]),
            plasma_to_blood_days=int(o[OPT_PLASMA_TO_BLOOD_DAYS]),
            slack_days=int(o[OPT_SLACK_DAYS]),
        )

    # --- Přístup jen pro vlastníka ----------------------------------------

    def allowed_users(self) -> set[str]:
        """Kdo smí modul používat.

        Výslovně nastavení uživatelé, jinak vlastník mobilní aplikace,
        na kterou chodí notifikace (mobile_app si pamatuje user_id).
        Nikdo jiný – ani administrátor – kartu, služby ani notifikační
        akce nepoužije.
        """
        configured = self.opts.get(OPT_ALLOWED_USERS) or []
        if configured:
            return set(configured)
        svc = (self.opts.get(OPT_NOTIFY_SERVICE) or "").replace("notify.mobile_app_", "")
        owners: set[str] = set()
        for entry in self.hass.config_entries.async_entries("mobile_app"):
            name = slugify(entry.data.get("device_name") or entry.title or "")
            if svc and name == svc and entry.data.get("user_id"):
                owners.add(entry.data["user_id"])
        return owners

    def is_allowed(self, user_id: str | None) -> bool:
        return bool(user_id) and user_id in self.allowed_users()

    @property
    def preferred_latest(self) -> time:
        return _parse_time(self.opts[OPT_PREFERRED_LATEST], "10:00")

    @property
    def person(self) -> Person:
        d = self.entry.data
        return Person(
            first_name=d[CONF_FIRST_NAME],
            last_name=d[CONF_LAST_NAME],
            birth_date=d[CONF_BIRTH_DATE],
            email=d[CONF_EMAIL],
            phone=d[CONF_PHONE],
            weight=int(d[CONF_WEIGHT]),
            blood_type=d[CONF_BLOOD_TYPE],
            title_before=d.get(CONF_TITLE_BEFORE, ""),
            title_after=d.get(CONF_TITLE_AFTER, ""),
            insurance_no=d.get(CONF_INSURANCE_NO, ""),
        )

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        self.data = stored or {}
        self.data.setdefault("reservations", {})
        self.data.setdefault("processed_mail", [])
        self.data.setdefault("notified", {})
        self.data.setdefault("blocked_until", None)
        self.data.setdefault("block_reason", None)
        self.data.setdefault("snoozed_until", None)
        self.data.setdefault("flow", None)
        # Rozpracovaný tok po restartu nemá platný kód – zahodit čekání
        if self.data["flow"]:
            self.data["flow"]["awaiting"] = None

        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick_terms, timedelta(minutes=TERMS_POLL_MINUTES))
        )
        self._unsubs.append(
            async_track_time_interval(self.hass, self._tick_mail, timedelta(minutes=MAIL_POLL_FAST_MINUTES))
        )
        self._unsubs.append(
            self.hass.bus.async_listen("mobile_app_notification_action", self._on_notification_action)
        )
        self.hass.async_create_task(self.async_refresh(full_mail=True))

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._save()

    async def _save(self) -> None:
        await self._store.async_save(self.data)

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def remove() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)

        return remove

    @callback
    def _changed(self) -> None:
        async_dispatcher_send(self.hass, f"{SIGNAL_UPDATE}_{self.entry.entry_id}")
        for cb in list(self._listeners):
            cb()

    # ------------------------------------------------------------------
    # Pravidelná obnova
    # ------------------------------------------------------------------

    async def _tick_terms(self, _now: datetime) -> None:
        await self.async_refresh(mail=False)

    async def _tick_mail(self, now: datetime) -> None:
        fast = self._fast_mail_until and now < self._fast_mail_until
        due = not self.mail_updated or now - self.mail_updated >= timedelta(minutes=MAIL_POLL_MINUTES)
        if fast or due:
            await self._sync_mail()
            self._mark_done()
            await self._save()
            self._changed()

    async def async_refresh(self, mail: bool = True, full_mail: bool = False) -> None:
        """Stáhne termíny (a e-maily), přepočítá plán, případně pošle nabídku."""
        for kind in KINDS:
            try:
                self.terms[kind] = await self.fnusa.available_terms(kind)
                self.last_error = None
            except FnusaError as err:
                self.last_error = str(err)
                _LOGGER.warning("Volné termíny (%s): %s", kind, err)
        self.terms_updated = dt_util.now()
        if mail:
            await self._sync_mail(full=full_mail)
        self._mark_done()
        await self._maybe_offer()
        await self._save()
        self._changed()

    # ------------------------------------------------------------------
    # Rezervace v úložišti
    # ------------------------------------------------------------------

    def _res(self) -> dict[str, dict[str, Any]]:
        return self.data["reservations"]

    @staticmethod
    def _start(res: dict[str, Any]) -> datetime:
        return dt_util.parse_datetime(res["start"])

    def _end(self, res: dict[str, Any]) -> datetime:
        return self._start(res) + timedelta(minutes=KIND_DURATION_MIN[res["kind"]])

    def _find(self, kind: str, start: datetime, statuses: tuple[str, ...] = (RES_RESERVED,)) -> dict[str, Any] | None:
        for res in self._res().values():
            if res["kind"] == kind and res["status"] in statuses and self._start(res) == start:
                return res
        return None

    def _add_reservation(self, kind: str, start: datetime, source: str, **extra: Any) -> dict[str, Any]:
        rid = uuid.uuid4().hex[:12]
        res = {
            "id": rid,
            "kind": kind,
            "start": start.isoformat(),
            "status": RES_RESERVED,
            "key": None,
            "admin_url": None,
            "fnusa_id": None,
            "gcal_id": None,
            "source": source,
            "created": dt_util.now().isoformat(),
        }
        res.update(extra)
        self._res()[rid] = res
        return res

    def _mark_done(self) -> None:
        now = dt_util.now()
        for res in self._res().values():
            if res["status"] == RES_RESERVED and self._end(res) < now:
                res["status"] = RES_DONE

    def upcoming(self) -> list[dict[str, Any]]:
        now = dt_util.now()
        items = [r for r in self._res().values() if r["status"] == RES_RESERVED and self._end(r) >= now]
        return sorted(items, key=self._start)

    def history(self) -> list[dict[str, Any]]:
        items = [r for r in self._res().values() if r["status"] in (RES_DONE, RES_SKIPPED)]
        return sorted(items, key=self._start, reverse=True)

    def _donations(self) -> list[planner.Donation]:
        now = dt_util.now()
        out = []
        for res in self._res().values():
            if res["status"] in (RES_DONE, RES_RESERVED):
                start = self._start(res)
                out.append(
                    planner.Donation(
                        kind=res["kind"],
                        start=dt_util.as_local(start),
                        reserved=res["status"] == RES_RESERVED and start >= now - timedelta(hours=3),
                        ref=res["id"],
                    )
                )
        return out

    def plan(self) -> list[planner.Step]:
        blocked = self.data.get("blocked_until")
        return planner.build_plan(
            self._donations(),
            dt_util.now().date(),
            self.rules,
            horizon_days=int(self.opts[OPT_HORIZON_DAYS]),
            blocked_until=date.fromisoformat(blocked) if blocked else None,
        )

    def next_bookable(self) -> planner.Step | None:
        return next((s for s in self.plan() if s.state == planner.STEP_BOOKABLE), None)

    def slots_for(self, kind: str, earliest: date) -> list[datetime]:
        terms = self.terms.get(kind)
        if not terms:
            return []
        now = dt_util.now() + timedelta(hours=2)
        return [s for s in planner.slots_from(terms.slots, earliest) if s > now]

    # ------------------------------------------------------------------
    # Gmail + Google kalendář
    # ------------------------------------------------------------------

    async def _sync_mail(self, full: bool = False) -> None:
        if not self.google:
            return
        try:
            ids = await self.google.list_fnusa_messages(400 if full or not self.data["processed_mail"] else 30)
        except GoogleError as err:
            _LOGGER.warning("Gmail: %s", err)
            return
        processed = set(self.data["processed_mail"])
        new_events = []
        for mid in ids:
            if mid in processed:
                continue
            try:
                subject, body, received = await self.google.get_message(mid)
            except GoogleError as err:
                _LOGGER.warning("Gmail zpráva %s: %s", mid, err)
                continue
            processed.add(mid)
            ev = parse_mail(subject, body, mid)
            if ev:
                new_events.append((received or dt_util.now(), ev))
        # Chronologicky – rezervace dřív než její storno
        for _received, ev in sorted(new_events, key=lambda x: x[0]):
            await self._apply_mail(ev)
        self.data["processed_mail"] = sorted(processed)[-500:]
        self.mail_updated = dt_util.now()
        if self._fast_mail_until and not any(not r.get("key") for r in self.upcoming()):
            self._fast_mail_until = None

    async def _apply_mail(self, ev: Any) -> None:
        start = ev.start
        if ev.type == MAIL_NEW:
            res = self._find(ev.kind, start, (RES_RESERVED, RES_DONE))
            if res is None:
                res = self._add_reservation(ev.kind, start, "email")
            res["key"] = res.get("key") or ev.key
            res["admin_url"] = res.get("admin_url") or ev.admin_url
            if res["status"] == RES_RESERVED and self._end(res) > dt_util.now():
                await self._sync_event(res)
        elif ev.type == MAIL_CANCEL:
            res = self._find(ev.kind, start, (RES_RESERVED,))
            if res:
                res["status"] = RES_CANCELLED
                await self._remove_event(res)

    def _event_body(self, res: dict[str, Any]) -> dict[str, Any]:
        start = dt_util.as_local(self._start(res))
        end = dt_util.as_local(self._end(res))
        lines = [EVENT_PLACE_NOTE, ""]
        if res.get("admin_url"):
            lines.append(f"Upravit / zrušit rezervaci: {res['admin_url']}")
        else:
            lines.append("Odkaz pro úpravu / zrušení se doplní po potvrzovacím e-mailu.")
        lines.append("")
        lines.append("Spravuje Home Assistant – Darování.")
        return {
            "summary": KIND_EVENT_TITLE[res["kind"]],
            "location": EVENT_LOCATION,
            "description": "\n".join(lines),
            "start": {"dateTime": start.isoformat(), "timeZone": "Europe/Prague"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Europe/Prague"},
            "reminders": {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": 1440}, {"method": "popup", "minutes": 0}],
            },
            "extendedProperties": {"private": {"darovani_id": res["id"], "darovani_kind": res["kind"]}},
        }

    async def _sync_event(self, res: dict[str, Any]) -> None:
        """Vytvoří nebo aktualizuje událost v Google kalendáři."""
        if not self.google:
            return
        body = self._event_body(res)
        try:
            if not res.get("gcal_id"):
                existing = await self.google.find_by_marker(res["id"]) or await self.google.find_matching(
                    self._start(res)
                )
                if existing:
                    res["gcal_id"] = existing["id"]
            if res.get("gcal_id"):
                await self.google.patch_event(res["gcal_id"], body)
            else:
                res["gcal_id"] = await self.google.create_event(body)
        except GoogleError as err:
            _LOGGER.warning("Google kalendář: %s", err)

    async def _remove_event(self, res: dict[str, Any]) -> None:
        if not self.google:
            return
        try:
            event_id = res.get("gcal_id")
            if not event_id:
                found = await self.google.find_by_marker(res["id"]) or await self.google.find_matching(self._start(res))
                event_id = found["id"] if found else None
            if event_id:
                await self.google.delete_event(event_id)
            res["gcal_id"] = None
        except GoogleError as err:
            _LOGGER.warning("Google kalendář (mazání): %s", err)

    # ------------------------------------------------------------------
    # Toky operací (rezervace / zrušení) se SMS kódem
    # ------------------------------------------------------------------

    @property
    def flow(self) -> dict[str, Any] | None:
        return self.data.get("flow")

    async def start_flow(self, ops: list[dict[str, Any]], title: str, silent: bool = False) -> None:
        """Spustí frontu operací. Nahradí případný rozpracovaný tok."""
        async with self._lock:
            self.data["flow"] = {
                "id": uuid.uuid4().hex[:8],
                "title": title,
                "silent": silent,
                "ops": ops,
                "done": [],
                "awaiting": None,
                "error": None,
                "code_sent_at": None,
                "resend_at": None,
            }
            await self._run_flow()

    async def abort_flow(self) -> None:
        async with self._lock:
            self.data["flow"] = None
            await self._clear_notification(TAG_CODE)
            await self._save()
            self._changed()

    async def submit_code(self, code: str) -> None:
        async with self._lock:
            flow = self.flow
            if not flow or not flow.get("awaiting"):
                raise FnusaError("Žádný krok teď nečeká na SMS kód.")
            code = "".join(ch for ch in code if ch.isalnum())
            awaiting = flow["awaiting"]
            try:
                if awaiting == AWAIT_BOOKING_CODE:
                    nonce, valid_to = await self.fnusa.verify_booking_code(self.person.phone, code)
                    self._nonce = (nonce, valid_to or dt_util.now() + timedelta(minutes=10))
                else:
                    key = flow["awaiting_key"]
                    self._admin[key] = await self.fnusa.admin_login(self.person.phone, key, code)
            except FnusaCodeRejected as err:
                flow["error"] = str(err)
                await self._notify_code_request(error=str(err))
                await self._save()
                self._changed()
                return
            flow["awaiting"] = None
            flow["error"] = None
            await self._clear_notification(TAG_CODE)
            await self._run_flow()

    async def resend_code(self) -> None:
        async with self._lock:
            flow = self.flow
            if not flow or not flow.get("awaiting"):
                return
            await self._request_code(flow, flow["awaiting"], flow.get("awaiting_key"))
            await self._save()
            self._changed()

    def _nonce_valid(self) -> bool:
        return bool(self._nonce and self._nonce[1] - timedelta(seconds=20) > dt_util.utcnow())

    def _admin_valid(self, key: str) -> bool:
        sess = self._admin.get(key)
        return bool(sess and sess.valid_to - timedelta(seconds=20) > dt_util.utcnow())

    async def _request_code(self, flow: dict[str, Any], awaiting: str, key: str | None = None) -> None:
        try:
            if awaiting == AWAIT_BOOKING_CODE:
                info = await self.fnusa.request_booking_code(self.person.phone)
                flow["resend_at"] = info.get("resendAllowedAt")
            else:
                await self.fnusa.request_admin_code(self.person.phone, key)
                flow["resend_at"] = (dt_util.utcnow() + timedelta(seconds=60)).isoformat()
        except FnusaError as err:
            flow["error"] = str(err)
        flow["awaiting"] = awaiting
        flow["awaiting_key"] = key
        flow["code_sent_at"] = dt_util.utcnow().isoformat()
        await self._notify_code_request(error=flow.get("error"))

    async def _run_flow(self) -> None:
        """Zpracuje operace, dokud nějaká nepotřebuje kód. Volat pod zámkem."""
        flow = self.flow
        while flow and flow["ops"]:
            op = flow["ops"][0]
            if op["op"] == "book":
                if not self._nonce_valid():
                    await self._request_code(flow, AWAIT_BOOKING_CODE)
                    break
                ok = await self._do_book(flow, op)
            else:
                res = self._res().get(op["res_id"])
                if not res or res["status"] != RES_RESERVED:
                    ok = True
                elif not res.get("key"):
                    flow["error"] = "U rezervace zatím chybí klíč z potvrzovacího e-mailu – zkus to za pár minut."
                    break
                elif not self._admin_valid(res["key"]):
                    await self._request_code(flow, AWAIT_ADMIN_CODE, res["key"])
                    break
                else:
                    ok = await self._do_cancel(flow, res)
            if ok is None:  # kód je potřeba znovu – smyčka ho vyžádá
                continue
            flow["ops"].pop(0)
            flow["done"].append({**op, "ok": ok})
        if flow and not flow["ops"]:
            await self._finish_flow(flow)
        await self._save()
        self._changed()

    async def _do_book(self, flow: dict[str, Any], op: dict[str, Any]) -> bool | None:
        kind = op["kind"]
        start = dt_util.parse_datetime(op["start"])
        try:
            terms = await self.fnusa.available_terms(kind)
            self.terms[kind] = terms
            if start not in terms.slots:
                raise FnusaSlotTaken("Termín už není volný.")
            fnusa_id = await self.fnusa.reserve(kind, start, self.person, terms.fields, self._nonce[0])
        except FnusaCodeRejected:
            self._nonce = None
            return None
        except FnusaSlotTaken as err:
            op["error"] = str(err)
            await self._notify_result(False, kind, start, str(err))
            return False
        except FnusaError as err:
            op["error"] = str(err)
            await self._notify_result(False, kind, start, str(err))
            return False
        res = self._add_reservation(kind, start, "module", fnusa_id=fnusa_id)
        await self._sync_event(res)
        self._fast_mail_until = dt_util.now() + timedelta(minutes=30)
        op["res_id"] = res["id"]
        # Plán se změnil – nabídka pro tento krok už neplatí
        self.data["notified"] = {}
        if not flow.get("silent"):
            await self._notify_result(True, kind, start)
        return True

    async def _do_cancel(self, flow: dict[str, Any], res: dict[str, Any]) -> bool | None:
        sess = self._admin[res["key"]]
        start = self._start(res)
        try:
            item = await self.fnusa.find_reservation(sess, res["key"], start)
            if item:
                await self.fnusa.delete_reservation(sess, res["key"], item)
        except FnusaCodeRejected:
            self._admin.pop(res["key"], None)
            return None
        except FnusaError as err:
            flow["error"] = str(err)
            return False
        res["status"] = RES_CANCELLED
        await self._remove_event(res)
        self.data["notified"] = {}
        return True

    async def _finish_flow(self, flow: dict[str, Any]) -> None:
        self.data["flow"] = None
        await self._clear_notification(TAG_CODE)
        cancelled = [d for d in flow["done"] if d["op"] == "cancel" and d.get("ok")]
        booked = [d for d in flow["done"] if d["op"] == "book" and d.get("ok")]
        if cancelled and not booked:
            await self._notify(
                "Rezervace zrušena" if len(cancelled) == 1 else f"Zrušeno rezervací: {len(cancelled)}",
                "Události z kalendáře jsem odstranil. Další termín ti nabídnu, jakmile bude volný.",
                tag=TAG_RESULT,
            )
        elif len(booked) > 1 or (booked and cancelled):
            lines = [f"{KIND_LABEL[b['kind']]} · {fmt_slot(dt_util.parse_datetime(b['start']))}" for b in booked]
            await self._notify("Plán upraven", "Nové termíny: " + ", ".join(lines), tag=TAG_RESULT)
        await self._maybe_offer(force=False)

    # ------------------------------------------------------------------
    # Veřejné akce (služby, karta, notifikace)
    # ------------------------------------------------------------------

    async def select_slot(self, kind: str, start: datetime) -> None:
        start = dt_util.as_local(start)
        label = f"{KIND_LABEL[kind]} {fmt_slot(start)}"
        await self.start_flow([{"op": "book", "kind": kind, "start": start.isoformat()}], f"Rezervace: {label}")

    async def cancel(self, res_id: str) -> None:
        res = self._res().get(res_id)
        if not res or res["status"] != RES_RESERVED:
            raise FnusaError("Rezervace nenalezena.")
        label = f"{KIND_LABEL[res['kind']]} {fmt_slot(self._start(res))}"
        await self.start_flow([{"op": "cancel", "res_id": res_id}], f"Zrušení: {label}")

    def postpone_preview(self, days: int) -> dict[str, Any]:
        slots = {k: (self.terms[k].slots if k in self.terms else []) for k in KINDS}
        result = planner.simulate_postpone(
            self._donations(), dt_util.now().date(), days, self.rules, slots, self.preferred_latest
        )
        changes = []
        for ch in result.changes:
            changes.append(
                {
                    "res_id": ch.reservation.ref,
                    "kind": ch.reservation.kind,
                    "start": ch.reservation.start.isoformat(),
                    "action": ch.action,
                    "new_earliest": ch.new_earliest.isoformat() if ch.new_earliest else None,
                    "replacement": ch.replacement.isoformat() if ch.replacement else None,
                }
            )
        return {"days": days, "blocked_until": result.blocked_until.isoformat(), "changes": changes}

    async def postpone_apply(self, days: int, reason: str | None = None) -> dict[str, Any]:
        preview = self.postpone_preview(days)
        self.data["blocked_until"] = preview["blocked_until"]
        self.data["block_reason"] = reason
        self.data["notified"] = {}
        ops: list[dict[str, Any]] = []
        # Nejdřív zrušit to, co v odkladu být nesmí, pak rezervovat náhrady
        for ch in preview["changes"]:
            if ch["action"] in ("move", "cancel"):
                ops.append({"op": "cancel", "res_id": ch["res_id"]})
        for ch in preview["changes"]:
            if ch["action"] == "move" and ch["replacement"]:
                ops.append({"op": "book", "kind": ch["kind"], "start": ch["replacement"]})
        if ops:
            await self.start_flow(ops, f"Odklad o {days} dní", silent=True)
        else:
            await self._save()
            self._changed()
        return preview

    async def clear_deferral(self) -> None:
        self.data["blocked_until"] = None
        self.data["block_reason"] = None
        self.data["notified"] = {}
        await self._maybe_offer()
        await self._save()
        self._changed()

    async def mark_not_done(self, res_id: str) -> None:
        res = self._res().get(res_id)
        if res and res["status"] == RES_DONE:
            res["status"] = RES_SKIPPED
            self.data["notified"] = {}
            await self._save()
            self._changed()

    async def add_history(self, kind: str, when: datetime) -> None:
        self._add_reservation(kind, dt_util.as_local(when), "manual", status=RES_DONE)
        await self._save()
        self._changed()

    async def snooze(self, hours: int = 24) -> None:
        self.data["snoozed_until"] = (dt_util.now() + timedelta(hours=hours)).isoformat()
        await self._save()
        self._changed()

    # ------------------------------------------------------------------
    # Notifikace
    # ------------------------------------------------------------------

    def _notify_service(self) -> str | None:
        svc = (self.opts.get(OPT_NOTIFY_SERVICE) or "").replace("notify.", "")
        if svc and self.hass.services.has_service("notify", svc):
            return svc
        return None

    async def _notify(
        self,
        title: str,
        message: str,
        *,
        tag: str,
        actions: list[dict[str, Any]] | None = None,
        level: str = "active",
        subtitle: str | None = None,
    ) -> None:
        svc = self._notify_service()
        if not svc:
            return
        data: dict[str, Any] = {
            "tag": tag,
            "group": NOTIFY_GROUP,
            "url": self.opts[OPT_DASHBOARD_PATH],
            "push": {"interruption-level": level},
        }
        if subtitle:
            data["subtitle"] = subtitle
        if actions:
            data["actions"] = actions
        try:
            await self.hass.services.async_call(
                "notify", svc, {"title": title, "message": message, "data": data}, blocking=True
            )
        except Exception as err:  # noqa: BLE001 – notifikace nesmí shodit tok
            _LOGGER.warning("Notifikace se nepodařila: %s", err)

    async def _clear_notification(self, tag: str) -> None:
        svc = self._notify_service()
        if svc:
            try:
                await self.hass.services.async_call(
                    "notify", svc, {"message": "clear_notification", "data": {"tag": tag}}, blocking=True
                )
            except Exception:  # noqa: BLE001
                pass

    def _in_quiet_hours(self) -> bool:
        now = dt_util.now().time()
        start = _parse_time(self.opts[OPT_QUIET_START], "21:30")
        end = _parse_time(self.opts[OPT_QUIET_END], "07:00")
        return now >= start or now < end if start > end else start <= now < end

    def _slot_actions(self, kind: str, slots: list[datetime]) -> list[dict[str, Any]]:
        return [
            {"action": f"{ACTION_BOOK}|{kind}|{dt_util.as_local(s).isoformat()}", "title": fmt_slot(s)}
            for s in slots
        ]

    async def _maybe_offer(self, force: bool = False) -> None:
        """Pošle nabídku termínů pro další krok – jen když dává smysl."""
        if self.flow:
            return
        step = self.next_bookable()
        if not step:
            return
        slots = self.slots_for(step.kind, step.earliest)
        if not slots:
            return
        snoozed = self.data.get("snoozed_until")
        if not force and snoozed and dt_util.parse_datetime(snoozed) > dt_util.now():
            return
        if not force and self._in_quiet_hours():
            return
        step_key = f"{step.kind}:{step.earliest.isoformat()}"
        first = slots[0]
        last = self.data["notified"].get(step_key)
        if not force and last:
            prev_first = dt_util.parse_datetime(last["first"])
            # Znovu jen když se uvolnil termín aspoň o den dřív
            if first.date() >= prev_first.date():
                return
        suggestions = planner.suggest_slots(slots, step.earliest, self.preferred_latest, count=3)
        actions = self._slot_actions(step.kind, suggestions)
        actions.append({"action": "URI", "title": "Jiný termín…", "uri": self.opts[OPT_DASHBOARD_PATH]})
        actions.append({"action": ACTION_LATER, "title": "Připomenout zítra"})
        title = f"{KIND_LABEL[step.kind]} – volné termíny"
        if last:
            title = f"{KIND_LABEL[step.kind]} – uvolnil se dřívější termín"
        await self._notify(
            title,
            f"Darovat můžeš od {fmt_day(step.earliest)}. Vyber termín, potvrdíš ho kódem z SMS.",
            tag=TAG_OFFER,
            actions=actions,
        )
        self.data["notified"][step_key] = {"first": first.isoformat(), "at": dt_util.now().isoformat()}

    async def _notify_code_request(self, error: str | None = None) -> None:
        flow = self.flow
        if not flow:
            return
        purpose = "rezervaci" if flow.get("awaiting") == AWAIT_BOOKING_CODE else "správu rezervace"
        message = f"{flow['title']}. FN USA ti poslala SMS s kódem pro {purpose} – zadej ho tady."
        if error:
            message = f"{error} {message}"
        await self._notify(
            "Kód z SMS",
            message,
            tag=TAG_CODE,
            level="time-sensitive",
            actions=[
                {
                    "action": ACTION_OTP,
                    "title": "Zadat kód",
                    "behavior": "textInput",
                    "textInputButtonTitle": "Potvrdit",
                    "textInputPlaceholder": "Kód z SMS",
                }
            ],
        )

    async def _notify_result(self, ok: bool, kind: str, start: datetime, error: str | None = None) -> None:
        if ok:
            message = f"{KIND_LABEL[kind]} · {fmt_slot_long(start)} · FN u sv. Anny. Událost je v kalendáři."
            actions: list[dict[str, Any]] = []
            step = self.next_bookable()
            if step and self._nonce_valid():
                nxt = planner.suggest_slots(
                    self.slots_for(step.kind, step.earliest), step.earliest, self.preferred_latest, count=2
                )
                if nxt:
                    message += f" Rovnou můžeš zamluvit i další ({KIND_LABEL[step.kind].lower()}) bez nového kódu:"
                    actions = self._slot_actions(step.kind, nxt)
            await self._clear_notification(TAG_OFFER)
            await self._notify("Rezervováno", message, tag=TAG_RESULT, actions=actions or None)
        else:
            await self._notify(
                "Rezervace se nepodařila",
                f"{KIND_LABEL[kind]} {fmt_slot(start)}: {error}",
                tag=TAG_RESULT,
                actions=[{"action": "URI", "title": "Vybrat jiný", "uri": self.opts[OPT_DASHBOARD_PATH]}],
            )

    async def _on_notification_action(self, event: Event) -> None:
        action = str(event.data.get("action", ""))
        if not action.startswith("DAROVANI"):
            return
        if not self.is_allowed(event.context.user_id):
            _LOGGER.warning("Akce z notifikace od nepovoleného uživatele %s ignorována", event.context.user_id)
            return
        try:
            if action.startswith(ACTION_BOOK + "|"):
                _, kind, iso = action.split("|", 2)
                await self.select_slot(kind, dt_util.parse_datetime(iso))
            elif action == ACTION_OTP:
                await self.submit_code(str(event.data.get("reply_text", "")))
            elif action == ACTION_LATER:
                await self.snooze(24)
            elif action.startswith(ACTION_UNDO_DONE + "|"):
                await self.mark_not_done(action.split("|", 1)[1])
        except FnusaError as err:
            await self._notify("Darování", str(err), tag=TAG_RESULT)

    # ------------------------------------------------------------------
    # Stav pro entity a kartu
    # ------------------------------------------------------------------

    def status(self) -> tuple[str, str]:
        flow = self.flow
        if flow and flow.get("awaiting"):
            return STATUS_WAITING_CODE, "Čeká na kód z SMS"
        if flow:
            return STATUS_BOOKING, flow["title"]
        if self.last_error and not self.terms:
            return STATUS_ERROR, self.last_error
        step = self.next_bookable()
        if step and step.earliest <= dt_util.now().date() + timedelta(days=int(self.opts[OPT_HORIZON_DAYS])):
            if self.slots_for(step.kind, step.earliest):
                return STATUS_TO_BOOK, f"{KIND_LABEL[step.kind]} od {fmt_day(step.earliest)} – k rezervaci"
            return STATUS_OK, f"{KIND_LABEL[step.kind]} od {fmt_day(step.earliest)} – termíny zatím nevypsané"
        return STATUS_OK, "Vše naplánováno"

    def _res_view(self, res: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": res["id"],
            "kind": res["kind"],
            "start": dt_util.as_local(self._start(res)).isoformat(),
            "end": dt_util.as_local(self._end(res)).isoformat(),
            "status": res["status"],
            "admin_url": res.get("admin_url") or (FNUSA_ADMIN_URL.format(key=res["key"]) if res.get("key") else None),
            "has_key": bool(res.get("key")),
            "in_calendar": bool(res.get("gcal_id")),
        }

    def snapshot(self) -> dict[str, Any]:
        status, message = self.status()
        steps = self.plan()
        upcoming = self.upcoming()
        offer = None
        step = self.next_bookable()
        if step:
            slots = self.slots_for(step.kind, step.earliest)
            days = planner.group_by_day(slots)
            offer = {
                "kind": step.kind,
                "earliest": step.earliest.isoformat(),
                "max_date": (self.terms[step.kind].max_date.isoformat() if step.kind in self.terms and self.terms[step.kind].max_date else None),
                "days": [
                    {"date": d.isoformat(), "slots": [dt_util.as_local(s).isoformat() for s in times]}
                    for d, times in days.items()
                ],
                "suggested": [
                    dt_util.as_local(s).isoformat()
                    for s in planner.suggest_slots(slots, step.earliest, self.preferred_latest, count=3)
                ],
            }
        flow = self.flow
        flow_view = None
        if flow:
            flow_view = {
                "title": flow["title"],
                "awaiting": flow.get("awaiting"),
                "error": flow.get("error"),
                "resend_at": flow.get("resend_at"),
                "remaining": len(flow["ops"]),
                "nonce_valid_to": self._nonce[1].isoformat() if self._nonce_valid() else None,
            }
        return {
            "status": status,
            "message": message,
            "next": self._res_view(upcoming[0]) if upcoming else None,
            "upcoming": [self._res_view(r) for r in upcoming],
            "plan": [
                {
                    "kind": s.kind,
                    "state": s.state,
                    "earliest": s.earliest.isoformat(),
                    "conflict": s.conflict,
                    "reservation": self._res_view(self._res()[s.reservation.ref])
                    if s.reservation and s.reservation.ref in self._res()
                    else None,
                }
                for s in steps
            ],
            "offer": offer,
            "flow": flow_view,
            "history": [self._res_view(r) for r in self.history()[:12]],
            "blocked_until": self.data.get("blocked_until"),
            "block_reason": self.data.get("block_reason"),
            "preferred_latest": self.preferred_latest.strftime("%H:%M"),
            "google": self.google is not None,
            "terms_updated": self.terms_updated.isoformat() if self.terms_updated else None,
            "mail_updated": self.mail_updated.isoformat() if self.mail_updated else None,
            "error": self.last_error,
        }

    # Pomocné pro senzory
    def last_of(self, kind: str) -> datetime | None:
        items = [r for r in self.history() if r["kind"] == kind and r["status"] == RES_DONE]
        return self._start(items[0]) if items else None

    def done_count(self) -> int:
        return sum(1 for r in self._res().values() if r["status"] == RES_DONE)
