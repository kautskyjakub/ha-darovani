"""Celý tok v běžícím Home Assistantu s napodobenou nemocnicí a Googlem."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.setup import async_setup_component

from custom_components.darovani.api import AdminSession, Terms
from custom_components.darovani.const import DOMAIN
from custom_components.darovani.mail_parser import parse_mail

TZ = ZoneInfo("Europe/Prague")


def dt(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=TZ)


def mail(kind_name: str, when: str, key: str | None, cancel: bool = False):
    if cancel:
        subject = f"Storno rezervace v systému NIS: {kind_name} (TO)"
        body = f"Vaše rezervace byla zrušena.\n\nTermín: pondělí {when}.\nNázev kalendáře: {kind_name}.\n"
    else:
        subject = f"Rezervace nového termínu v systému NIS: {kind_name} (TO)"
        body = (
            f"Byl pro Vás rezervován nový termín.\n\nTermín: pondělí {when}.\nNázev kalendáře: {kind_name}.\n"
            f"odkazu: https://rezervace.fnusa.cz/Administration?ReservationKey={key}\n"
        )
    return subject, body


MAILS = {
    "m1": (mail("Dárci krve", "31.8.2026 6:19", "KEYBLOODAUG"), dt(2026, 8, 12, 9)),
    "m2": (mail("Plazmaferézy - Objednávky", "12.10.2026 9:00", "KEYPLASMAOCT"), dt(2026, 8, 12, 9, 5)),
    "m3": (mail("Dárci krve", "30.11.2026 6:15", "KEYBLOODNOV"), dt(2026, 8, 12, 9, 10)),
}

PLASMA_SLOTS = [dt(2026, 12, 30, 11, 0), dt(2026, 12, 31, 7, 30), dt(2027, 1, 4, 9, 45), dt(2027, 1, 5, 7, 15)]
BLOOD_SLOTS = [dt(2026, 11, 30, 6, 15), dt(2027, 3, 3, 6, 30)]
FIELDS = [
    {"NisKalendareReservationSystemHtmlElementRowId": 14, "DynamicFieldUITypeAsString": "FirstName", "HtmlElementUIOrder": 1},
    {"NisKalendareReservationSystemHtmlElementRowId": 15, "DynamicFieldUITypeAsString": "LastName", "HtmlElementUIOrder": 2},
    {"NisKalendareReservationSystemHtmlElementRowId": 20, "DynamicFieldUITypeAsString": "MobilePhoneNoE164", "HtmlElementUIOrder": 3},
    {"NisKalendareReservationSystemHtmlElementRowId": 22, "DynamicFieldUITypeAsString": "BloodType", "HtmlElementUIOrder": 4},
]


OWNER = "owner-user-id"
OWNER_CTX = Context(user_id=OWNER)


@pytest.fixture
async def setup(hass: HomeAssistant, freezer: FrozenDateTimeFactory, hass_admin_user):
    freezer.move_to("2026-11-01T10:00:00+01:00")
    await hass.config.async_set_time_zone("Europe/Prague")
    notify_calls = async_mock_service(hass, "notify", "mobile_app_test_phone")

    fnusa = MagicMock()
    fnusa.available_terms = AsyncMock(
        side_effect=lambda kind: Terms(PLASMA_SLOTS if kind == "plazma" else BLOOD_SLOTS, FIELDS, None, None)
    )
    fnusa.request_booking_code = AsyncMock(return_value={"validTo": "x", "resendAllowedAt": "y"})
    fnusa.verify_booking_code = AsyncMock(return_value=("NONCE", dt(2026, 11, 1, 10, 10)))
    fnusa.reserve = AsyncMock(return_value=12345)
    fnusa.request_admin_code = AsyncMock()
    fnusa.admin_login = AsyncMock(return_value=AdminSession("TOKEN", "Guest", dt(2026, 11, 1, 10, 20)))
    fnusa.find_reservation = AsyncMock(return_value={"ReservationId": 1, "ReservationRowLastUpdatedCetCestTicks": "1"})
    fnusa.delete_reservation = AsyncMock()

    google = MagicMock()
    google.list_fnusa_messages = AsyncMock(return_value=list(MAILS))
    google.get_message = AsyncMock(side_effect=lambda mid: (*MAILS[mid][0], MAILS[mid][1]))
    google.find_by_marker = AsyncMock(return_value=None)
    google.find_matching = AsyncMock(
        side_effect=lambda start, *a: {"id": "existing-nov"} if start == dt(2026, 11, 30, 6, 15) else None
    )
    google.create_event = AsyncMock(return_value="new-event")
    google.patch_event = AsyncMock()
    google.delete_event = AsyncMock()
    google.create_invite = AsyncMock(return_value="invite-1")
    google.delete_invite = AsyncMock()
    google.my_email = AsyncMock(return_value="me@gmail.com")
    google.send_raw = AsyncMock()

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={
            "auth_implementation": "darovani",
            "token": {"access_token": "a", "refresh_token": "r", "expires_at": 9e9},
            "first_name": "Jakub",
            "last_name": "Test",
            "birth_date": "2000-01-01",
            "email": "test@example.com",
            "phone": "+420601234567",
            "weight": 80,
            "blood_type": "A+",
        },
        options={"notify_service": "notify.mobile_app_test_phone", "allowed_users": [OWNER, hass_admin_user.id],
                 "work_invite_email": "work@example.com"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.darovani.config_entry_oauth2_flow.async_get_config_entry_implementation", AsyncMock()),
        patch("custom_components.darovani.config_entry_oauth2_flow.OAuth2Session"),
        patch("custom_components.darovani.FnusaClient", return_value=fnusa),
        patch("custom_components.darovani.GoogleClient", return_value=google),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry.runtime_data, fnusa, google, notify_calls


async def test_import_plan_and_offer(hass: HomeAssistant, setup) -> None:
    manager, fnusa, google, notify_calls = setup
    snap = manager.snapshot()
    # Krev 30. 11. převzata z e-mailu, adoptována existující událost a doplněn odkaz
    assert snap["next"]["kind"] == "krev"
    assert snap["next"]["admin_url"].endswith("KEYBLOODNOV")
    google.patch_event.assert_awaited()
    assert "KEYBLOODNOV" in google.patch_event.await_args.args[1]["description"]
    # Další krok: plazma od 30. 12., navržené ranní termíny
    assert snap["offer"]["kind"] == "plazma"
    assert snap["offer"]["earliest"] == "2026-12-30"
    assert snap["offer"]["suggested"][0].startswith("2026-12-31T07:30")
    assert snap["status"] == "to_book"
    # Jedna notifikace s nabídkou
    offers = [c for c in notify_calls if c.data.get("data", {}).get("tag") == "darovani-nabidka"]
    assert len(offers) == 1
    actions = offers[0].data["data"]["actions"]
    assert actions[0]["action"].startswith("DAROVANI_BOOK|plazma|2026-12-31T07:30")
    # Senzory
    assert hass.states.get("sensor.darovani_krve_a_plazmy_status").state == "to_book"
    assert hass.states.get("sensor.darovani_krve_a_plazmy_donations").state == "2"


async def test_booking_with_sms_code_from_notification(hass: HomeAssistant, setup) -> None:
    manager, fnusa, google, notify_calls = setup
    hass.bus.async_fire("mobile_app_notification_action", {"action": "DAROVANI_BOOK|plazma|2026-12-31T07:30:00+01:00"}, context=OWNER_CTX)
    await hass.async_block_till_done()
    fnusa.request_booking_code.assert_awaited_once_with("+420601234567")
    assert manager.status()[0] == "waiting_code"
    code_notes = [c for c in notify_calls if c.data.get("data", {}).get("tag") == "darovani-kod"]
    assert code_notes[-1].data["data"]["actions"][0]["behavior"] == "textInput"

    hass.bus.async_fire("mobile_app_notification_action", {"action": "DAROVANI_OTP", "reply_text": "073-07"}, context=OWNER_CTX)
    await hass.async_block_till_done()
    fnusa.verify_booking_code.assert_awaited_once_with("+420601234567", "07307")
    kind, start, person, fields, nonce = fnusa.reserve.await_args.args
    assert (kind, start, nonce) == ("plazma", dt(2026, 12, 31, 7, 30), "NONCE")
    google.create_event.assert_awaited()
    body = google.create_event.await_args.args[0]
    assert body["summary"] == "🩸 Darování plazmy"
    assert body["end"]["dateTime"].startswith("2026-12-31T09:00")
    assert body["reminders"]["overrides"][0]["minutes"] == 1440
    assert manager.flow is None
    results = [c for c in notify_calls if c.data.get("data", {}).get("tag") == "darovani-vysledek"]
    assert results[-1].data["title"] == "Rezervováno"
    # Další krok se posunul na plazmu po 31. 12.
    assert manager.next_bookable().earliest.isoformat() == "2027-01-21"


async def test_cancel_with_admin_code(hass: HomeAssistant, setup) -> None:
    manager, fnusa, google, notify_calls = setup
    res_id = manager.snapshot()["next"]["id"]
    await hass.services.async_call(DOMAIN, "cancel_reservation", {"reservation_id": res_id}, blocking=True, context=OWNER_CTX)
    fnusa.request_admin_code.assert_awaited_once_with("+420601234567", "KEYBLOODNOV")
    await hass.services.async_call(DOMAIN, "submit_code", {"code": "0730773537"}, blocking=True, context=OWNER_CTX)
    fnusa.admin_login.assert_awaited_once_with("+420601234567", "KEYBLOODNOV", "0730773537")
    fnusa.delete_reservation.assert_awaited_once()
    google.delete_event.assert_awaited_with("existing-nov")
    assert manager.snapshot()["next"] is None
    # Bez krve 30. 11. jde podle pravidel nejdřív další plazma (3 týdny po 12. 10.)
    step = manager.next_bookable()
    assert (step.kind, step.earliest.isoformat()) == ("plazma", "2026-11-02")


async def test_postpone_preview_and_card(hass: HomeAssistant, setup, hass_ws_client, hass_client) -> None:
    manager, *_ = setup
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "darovani/postpone_preview", "days": 35})
    msg = await ws.receive_json()
    assert msg["success"]
    change = msg["result"]["changes"][0]
    # krev 30. 11. je v odkladu do 6. 12. -> přesun na nejbližší volný termín krve
    assert change["action"] == "move"
    assert change["replacement"].startswith("2027-03-03T06:30")
    await ws.send_json({"id": 2, "type": "darovani/subscribe"})
    assert (await ws.receive_json())["success"]
    event = await ws.receive_json()
    assert event["event"]["offer"]["kind"] == "plazma"

    from homeassistant.components.lovelace.const import LOVELACE_DATA

    urls = [r["url"] for r in hass.data[LOVELACE_DATA].resources.async_items()]
    assert any(u.startswith("/darovani_static/darovani-card.js?v=") for u in urls)

    client = await hass_client()
    resp = await client.get("/darovani_static/darovani-card.js")
    assert resp.status == 200
    assert "darovani-card" in await resp.text()


async def test_options_flow(hass: HomeAssistant, setup) -> None:
    manager, *_ = setup
    entry = manager.entry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "menu"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "rules"})
    assert result["step_id"] == "rules"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "blood_interval_months": 3,
            "blood_to_plasma_days": 28,
            "plasma_interval_days": 21,
            "plasma_to_blood_days": 21,
            "slack_days": 7,
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert hass.config_entries.async_get_entry(entry.entry_id).options["blood_to_plasma_days"] == 28
    assert hass.config_entries.async_get_entry(entry.entry_id).options["notify_service"] == "notify.mobile_app_test_phone"


async def test_only_owner_can_use_module(hass: HomeAssistant, setup, hass_ws_client) -> None:
    manager, fnusa, google, notify_calls = setup
    stranger = Context(user_id="janca-user-id")
    # Služba od jiného uživatele i bez uživatele (automatizace) je zamítnutá
    for ctx in (stranger, None):
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN, "select_slot", {"kind": "plazma", "start": "2026-12-31T07:30:00+01:00"}, blocking=True, context=ctx
            )
    # Akce z cizího telefonu se ignoruje
    hass.bus.async_fire("mobile_app_notification_action", {"action": "DAROVANI_BOOK|plazma|2026-12-31T07:30:00+01:00"}, context=stranger)
    await hass.async_block_till_done()
    fnusa.request_booking_code.assert_not_awaited()
    assert manager.flow is None
    # Karta pro jiného uživatele nic neukáže
    manager.allowed_users = lambda: {OWNER}  # bez admina, kterého používá testovací websocket
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "darovani/subscribe"})
    assert (await ws.receive_json())["success"]
    assert (await ws.receive_json())["event"] == {"setup": True, "forbidden": True}
    await ws.send_json({"id": 2, "type": "darovani/postpone_preview", "days": 14})
    assert not (await ws.receive_json())["success"]


async def test_owner_defaults_to_notification_phone_user(hass: HomeAssistant, setup) -> None:
    manager, *_ = setup
    MockConfigEntry(domain="mobile_app", data={"device_name": "Test Phone", "user_id": "phone-owner"}).add_to_hass(hass)
    hass.config_entries.async_update_entry(manager.entry, options={**manager.entry.options, "allowed_users": []})
    assert manager.allowed_users() == {"phone-owner"}
    assert not manager.is_allowed(None)


async def test_forget_and_no_resurrection_from_old_mail(hass: HomeAssistant, setup) -> None:
    manager, fnusa, google, notify_calls = setup
    res_id = manager.snapshot()["next"]["id"]
    await hass.services.async_call(DOMAIN, "forget_reservation", {"reservation_id": res_id}, blocking=True, context=OWNER_CTX)
    assert manager.snapshot()["next"] is None
    google.delete_event.assert_awaited_with("existing-nov")
    fnusa.request_admin_code.assert_not_awaited()
    # Stejný potvrzovací e-mail znovu (např. nové načtení pošty) rezervaci nevzkřísí
    await manager._apply_mail(parse_mail(*MAILS["m3"][0]))
    assert manager.snapshot()["next"] is None


async def test_work_invite_only_for_new_bookings(hass: HomeAssistant, setup) -> None:
    import base64
    import email

    manager, fnusa, google, notify_calls = setup

    def last_mail():
        raw = google.send_raw.await_args.args[0]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw))

    def ics_of(msg) -> str:
        return next(p for p in msg.walk() if p.get_content_type() == "text/calendar").get_payload(decode=True).decode()

    # Starší rezervace krve 30. 11. (mail z léta) pozvánku nedostala
    google.send_raw.assert_not_awaited()
    # Nová rezervace přes modul -> celodenní PLASMA, Mimo kancelář, připomenutí den předem
    await hass.services.async_call(
        DOMAIN, "select_slot", {"kind": "plazma", "start": "2026-12-31T07:30:00+01:00"}, blocking=True, context=OWNER_CTX
    )
    await hass.services.async_call(DOMAIN, "submit_code", {"code": "12345"}, blocking=True, context=OWNER_CTX)
    msg = last_mail()
    assert msg["To"] == "work@example.com"
    assert "Darování" in str(email.header.make_header(email.header.decode_header(msg["From"])))
    cal = ics_of(msg)
    assert "METHOD:REQUEST" in cal and "SUMMARY:PLASMA" in cal
    assert "DTSTART;VALUE=DATE:20261231" in cal and "DTEND;VALUE=DATE:20270101" in cal
    assert "X-MICROSOFT-CDO-BUSYSTATUS:OOF" in cal and "TRIGGER:-P1D" in cal
    assert 'ORGANIZER;CN="Darování – Jakub Test":mailto:me@gmail.com' in cal
    # Ruční odeslání pro starší rezervaci a její zrušení (METHOD:CANCEL, vyšší SEQUENCE)
    old = next(r for r in manager.upcoming() if r["kind"] == "krev")
    await hass.services.async_call(DOMAIN, "send_work_invite", {"reservation_id": old["id"]}, blocking=True, context=OWNER_CTX)
    assert "SUMMARY:KREV" in ics_of(last_mail())
    await hass.services.async_call(DOMAIN, "forget_reservation", {"reservation_id": old["id"]}, blocking=True, context=OWNER_CTX)
    cal = ics_of(last_mail())
    assert "METHOD:CANCEL" in cal and "SEQUENCE:1" in cal and "SUMMARY:KREV" in cal


async def test_cancel_and_rebook_same_slot(hass: HomeAssistant, setup, freezer) -> None:
    """Zrušení a nové zarezervování téhož termínu (FNUSA použije stejný klíč)."""
    manager, fnusa, google, notify_calls = setup
    slot = {"kind": "plazma", "start": "2027-01-05T09:45:00+01:00"}
    plazma = "Plazmaferézy - Objednávky"

    async def book() -> None:
        await hass.services.async_call(DOMAIN, "select_slot", slot, blocking=True, context=OWNER_CTX)
        if manager.flow and manager.flow.get("awaiting"):
            await hass.services.async_call(DOMAIN, "submit_code", {"code": "1"}, blocking=True, context=OWNER_CTX)

    global PLASMA_SLOTS
    PLASMA_SLOTS.append(dt(2027, 1, 5, 9, 45))
    try:
        await book()
        await manager._apply_mail(parse_mail(*mail(plazma, "5.1.2027 9:45", "KEYJAN")), datetime(2026, 11, 1, 10, 0, 30, tzinfo=TZ))
        first = next(r for r in manager.upcoming() if r["kind"] == "plazma")
        assert first["key"] == "KEYJAN"

        freezer.move_to("2026-11-01T10:06:00+01:00")
        await hass.services.async_call(DOMAIN, "cancel_reservation", {"reservation_id": first["id"]}, blocking=True, context=OWNER_CTX)
        await hass.services.async_call(DOMAIN, "submit_code", {"code": "2"}, blocking=True, context=OWNER_CTX)
        freezer.move_to("2026-11-01T10:07:00+01:00")
        await book()

        # Pošta dorazí až teď: storno (10:06:05) a nové potvrzení se stejným klíčem (10:07:05)
        await manager._apply_mail(parse_mail(*mail(plazma, "5.1.2027 9:45", None, cancel=True)), datetime(2026, 11, 1, 10, 6, 5, tzinfo=TZ))
        await manager._apply_mail(parse_mail(*mail(plazma, "5.1.2027 9:45", "KEYJAN")), datetime(2026, 11, 1, 10, 7, 5, tzinfo=TZ))
        upcoming = [r for r in manager.upcoming() if r["kind"] == "plazma"]
        assert len(upcoming) == 1
        assert upcoming[0]["key"] == "KEYJAN"
    finally:
        PLASMA_SLOTS.remove(dt(2027, 1, 5, 9, 45))
