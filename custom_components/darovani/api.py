"""Klient rezervačního systému NIS FNUSA (rezervace.fnusa.cz).

Postup rezervace (stejný jako webový formulář):
  1. GET  /api/AvailableTerms/NIS/{idPrac}/{kalnum}  -> volné termíny + pole formuláře
  2. POST /api/PreReservationOtp/RequestNIS          -> SMS kód na telefon
  3. POST /api/PreReservationOtp/VerifyNIS           -> verificationNonce (platí pár minut)
  4. POST /api/ReserveTerm/NIS/{idPrac}/{kalnum}     -> {id, fileUploadKey}
  5. POST /api/UploadFiles/NIS (prázdný)             -> dokončení rezervace, odchází potvrzení

Postup zrušení (stránka Administrace z odkazu v e-mailu):
  1. POST /api/OtpRequest/NIS                        -> SMS kód k danému ReservationKey
  2. POST /api/Authenticate/NIS/Guest                -> token (platí cca 20 minut)
  3. GET  /api/ReservedTerms/NIS?den/měsíc/rok        -> ReservationId + časové razítko
  4. POST /api/DeleteReservation/NIS/{key}           -> smazání, odchází storno e-mail

Zjednodušeně: nemocnice chce u každé rezervace i zrušení ověřit telefon
SMS kódem. Klient umí všechno ostatní; kód mu předá uživatel.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from .const import FNUSA_BASE_URL, FNUSA_CALENDARS

_LOGGER = logging.getLogger(__name__)
PRAGUE = ZoneInfo("Europe/Prague")
TIMEOUT = aiohttp.ClientTimeout(total=25)
USER_AGENT = "HomeAssistant-Darovani/1.0 (+https://github.com/kautskyjakub/ha-darovani)"

H_LANG = "FNUSA-Covid19Reservations-Language"
H_OTP_PHONE = "FNUSA-Covid19Reservations-PreSubmitOtp-Phone"
H_OTP_CODE = "FNUSA-Covid19Reservations-PreSubmitOtp-Code"
H_OTP_NONCE = "FNUSA-Covid19Reservations-PreSubmitOtp-Verification-Nonce"
H_AUTH_TOKEN = "FNUSA-Covid19Reservations-Authentication-Token"
H_AUTH_PRINCIPAL = "FNUSA-Covid19Reservations-Authentication-Principal"


class FnusaError(Exception):
    """Chyba komunikace s rezervačním systémem."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class FnusaCodeRejected(FnusaError):
    """SMS kód nebo jeho ověření (nonce/token) systém odmítl nebo vypršelo."""


class FnusaSlotTaken(FnusaError):
    """Vybraný termín mezitím obsadil někdo jiný."""


@dataclass
class Person:
    """Údaje dárce pro formulář (zadávají se v nastavení integrace)."""

    first_name: str
    last_name: str
    birth_date: str          # YYYY-MM-DD
    email: str
    phone: str               # E.164, např. +420601234567
    weight: int
    blood_type: str          # 0-, 0+, A-, ..., uncertain
    title_before: str = ""
    title_after: str = ""
    insurance_no: str = ""


@dataclass
class Terms:
    """Volné termíny jednoho kalendáře."""

    slots: list[datetime]
    fields: list[dict[str, Any]]
    min_date: date | None
    max_date: date | None


@dataclass
class AdminSession:
    """Přihlášení ke správě rezervace (po SMS kódu)."""

    token: str
    principal: str
    valid_to: datetime


def normalize_phone(phone: str) -> str:
    """'+420 601 234 567' -> '+420601234567', '601234567' -> '+420601234567'."""
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if not digits.startswith("+"):
        digits = "+420" + digits.lstrip("0") if len(digits) == 9 else "+" + digits
    return digits


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(PRAGUE)


def _parse_min_max(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y/%m/%d").date()
    except ValueError:
        return None


class FnusaClient:
    """Asynchronní klient – jeden na integraci."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        data: Any = None,
        params: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> tuple[int, Any]:
        hdrs = {"User-Agent": USER_AGENT, H_LANG: "cs", "Accept": "application/json"}
        if headers:
            hdrs.update(headers)
        try:
            async with self._session.request(
                method,
                FNUSA_BASE_URL + path,
                headers=hdrs,
                json=json_body,
                data=data,
                params=params,
                timeout=TIMEOUT,
            ) as resp:
                text = await resp.text()
                payload: Any = text
                if expect_json and text:
                    try:
                        payload = await resp.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError):
                        payload = text
                return resp.status, payload
        except (aiohttp.ClientError, TimeoutError) as err:
            raise FnusaError(f"Rezervační systém FNUSA neodpovídá ({err})") from err

    # --- Volné termíny ----------------------------------------------------

    async def available_terms(self, kind: str) -> Terms:
        id_prac, kalnum = FNUSA_CALENDARS[kind]
        status, data = await self._request("GET", f"/api/AvailableTerms/NIS/{id_prac}/{kalnum}")
        if status != 200 or not isinstance(data, dict):
            raise FnusaError(f"Nepodařilo se načíst volné termíny ({status})", status)
        slots: list[datetime] = []
        for day in data.get("Days") or []:
            if not day.get("DayAvailable", True):
                continue
            slots.extend(_parse_dt(t) for t in day.get("AvailableDayTerms") or [])
        return Terms(
            slots=sorted(slots),
            fields=data.get("TargetReservationCalendarSettings") or [],
            min_date=_parse_min_max(data.get("MinDateString")),
            max_date=_parse_min_max(data.get("MaxDateString")),
        )

    # --- Rezervace --------------------------------------------------------

    async def request_booking_code(self, phone: str) -> dict[str, Any]:
        """Pošle SMS kód pro novou rezervaci. Vrací validTo/resendAllowedAt."""
        status, data = await self._request(
            "POST", "/api/PreReservationOtp/RequestNIS", headers={H_OTP_PHONE: normalize_phone(phone)}
        )
        if status == 200 and isinstance(data, dict) and data.get("validTo"):
            return data
        if status == 429:
            raise FnusaError("Kód byl vyžádán příliš brzy, zkus to za chvíli znovu.", status)
        raise FnusaError(f"Nepodařilo se odeslat SMS kód ({status})", status)

    async def verify_booking_code(self, phone: str, code: str) -> tuple[str, datetime | None]:
        """Ověří SMS kód -> (nonce, platnost)."""
        status, data = await self._request(
            "POST",
            "/api/PreReservationOtp/VerifyNIS",
            headers={H_OTP_PHONE: normalize_phone(phone), H_OTP_CODE: code.replace("-", "").strip()},
        )
        if status == 200 and isinstance(data, dict) and data.get("verificationNonce"):
            valid_to = data.get("validTo")
            return data["verificationNonce"], (_parse_dt(valid_to) if valid_to else None)
        raise FnusaCodeRejected("SMS kód nebyl přijat – zkontroluj ho, případně si nech poslat nový.", status)

    @staticmethod
    def build_fields(fields: list[dict[str, Any]], person: Person) -> list[dict[str, Any]]:
        """Vyplní dynamická pole formuláře podle jejich typu."""
        values = {
            "TitlesBeforeFullName": person.title_before,
            "FirstName": person.first_name,
            "LastName": person.last_name,
            "TitlesAfterFullName": person.title_after,
            "HealthInsuranceNoOrBirthNo": person.insurance_no,
            "DateOfBirth": person.birth_date,
            "EmailAddressOnlyNormalized": person.email,
            "MobilePhoneNoE164": normalize_phone(person.phone),
            "Weight": str(int(person.weight)),
            "BloodType": person.blood_type,
            "ReservationNote": "",
        }
        out: list[dict[str, Any]] = []
        for field in sorted(fields, key=lambda f: f.get("HtmlElementUIOrder", 0)):
            if field.get("HtmlElementOutputOnly") or field.get("HtmlElementUIDeactivated"):
                continue
            ui_type = field.get("DynamicFieldUITypeAsString")
            field_id = field.get("NisKalendareReservationSystemHtmlElementRowId")
            if ui_type not in values:
                if field.get("DotNetTypeRequired"):
                    label = field.get("LabelElementInnerText") or ui_type
                    raise FnusaError(f"Formulář nemocnice vyžaduje nové pole „{label}“ – integraci je potřeba aktualizovat.")
                continue
            out.append({"fieldId": field_id, "fieldInvariantValues": [values[ui_type] or ""]})
        return out

    async def reserve(
        self, kind: str, start: datetime, person: Person, fields: list[dict[str, Any]], nonce: str
    ) -> int:
        """Vytvoří rezervaci. Vrací interní id rezervace u nemocnice."""
        id_prac, kalnum = FNUSA_CALENDARS[kind]
        body = {
            "chosenReservationTermInCsCzString": start.astimezone(PRAGUE).strftime("%d.%m.%Y %H:%M"),
            "gdprConsent": True,
            "dynamicFieldsIdsAndValues": self.build_fields(fields, person),
        }
        status, data = await self._request(
            "POST",
            f"/api/ReserveTerm/NIS/{id_prac}/{kalnum}",
            headers={"Content-Type": "application/json;charset=UTF-8", H_OTP_NONCE: nonce},
            json_body=body,
        )
        if status == 401:
            raise FnusaCodeRejected("Ověření telefonu vypršelo – je potřeba nový SMS kód.", status)
        if status != 200 or not isinstance(data, dict) or not data.get("id"):
            message = data if isinstance(data, str) and data else f"chyba {status}"
            if status in (400, 409) and isinstance(message, str) and "termín" in message.lower():
                raise FnusaSlotTaken("Termín už mezitím někdo obsadil.", status)
            raise FnusaError(f"Rezervace se nezdařila: {str(message)[:200]}", status)

        # Dokončení – web vždy volá upload (i bez příloh)
        form = aiohttp.FormData()
        up_status, _ = await self._request(
            "POST",
            "/api/UploadFiles/NIS",
            headers={"nisRowCounterId": str(data["id"]), "nisRowFileUploadKey": str(data.get("fileUploadKey", ""))},
            data=form,
            expect_json=False,
        )
        if up_status != 200:
            _LOGGER.warning("Dokončení rezervace vrátilo %s, rezervace %s ale vznikla", up_status, data["id"])
        return int(data["id"])

    # --- Správa / zrušení -------------------------------------------------

    async def request_admin_code(self, phone: str, key: str) -> None:
        status, _ = await self._request(
            "POST",
            "/api/OtpRequest/NIS",
            headers={
                "otpRequestValue": normalize_phone(phone),
                "otpRequestValueType": "sms",
                "otpRequestReservationKey": key,
            },
            expect_json=False,
        )
        if status != 200:
            raise FnusaError(f"Nepodařilo se odeslat SMS kód pro správu rezervace ({status})", status)

    async def admin_login(self, phone: str, key: str, code: str) -> AdminSession:
        status, data = await self._request(
            "POST",
            "/api/Authenticate/NIS/Guest",
            headers={
                "otpRequestValue": normalize_phone(phone),
                "otpRequestValueType": "sms",
                "otpRequestReservationKey": key,
                "OtpCode": code.replace("-", "").strip(),
            },
        )
        if status == 200 and isinstance(data, dict) and data.get("urlEncodedBase64EncodedAuthenticationToken"):
            return AdminSession(
                token=data["urlEncodedBase64EncodedAuthenticationToken"],
                principal=data.get("principalType", "Guest"),
                valid_to=_parse_dt(data["tokenValidToUtcDateTime"].replace("Z", "+00:00")),
            )
        raise FnusaCodeRejected("SMS kód pro správu rezervace nebyl přijat.", status)

    def _auth_headers(self, sess: AdminSession) -> dict[str, str]:
        return {H_AUTH_TOKEN: sess.token, H_AUTH_PRINCIPAL: sess.principal}

    async def find_reservation(self, sess: AdminSession, key: str, start: datetime) -> dict[str, Any] | None:
        """Najde rezervaci daného klíče v daný den; None = už neexistuje."""
        local = start.astimezone(PRAGUE)
        status, data = await self._request(
            "GET",
            "/api/ReservedTerms/NIS",
            params={
                "pragueCetCestDay": str(local.day),
                "pragueCetCestMonth": str(local.month),
                "pragueCetCestYear": str(local.year),
            },
            headers={**self._auth_headers(sess), "reservationKey": key},
        )
        if status == 404:
            return None
        if status == 401:
            raise FnusaCodeRejected("Přihlášení ke správě rezervace vypršelo.", status)
        if status != 200 or not isinstance(data, dict):
            raise FnusaError(f"Nepodařilo se načíst rezervaci ({status})", status)
        want = f"{local.hour}:{local.minute:02d}"
        items = data.get("reservationsForPragueCetCestDayAsViewModel") or []
        for item in items:
            if item.get("ReservationTimeOnlyCetCestFormattedString") == want:
                return item
        return items[0] if len(items) == 1 else None

    async def delete_reservation(self, sess: AdminSession, key: str, item: dict[str, Any]) -> None:
        status, data = await self._request(
            "POST",
            f"/api/DeleteReservation/NIS/{key}",
            headers={
                **self._auth_headers(sess),
                "reservationIdForDeletion": str(item["ReservationId"]),
                "reservationRowLastUpdatedCetCestTicks": str(item["ReservationRowLastUpdatedCetCestTicks"]),
            },
            expect_json=False,
        )
        if status == 401:
            raise FnusaCodeRejected("Přihlášení ke správě rezervace vypršelo.", status)
        if status not in (200, 204):
            raise FnusaError(f"Zrušení se nezdařilo ({status}): {str(data)[:150]}", status)
