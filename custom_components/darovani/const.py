"""Konstanty integrace Darování krve a plazmy (FNUSA Brno)."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "darovani"
VERSION: Final = "1.0.1"

# --- Druhy odběru --------------------------------------------------------
KIND_BLOOD: Final = "krev"
KIND_PLASMA: Final = "plazma"
KINDS: Final = (KIND_BLOOD, KIND_PLASMA)

# Kalendáře v rezervačním systému NIS FNUSA (IdPrac / Kalnum)
FNUSA_BASE_URL: Final = "https://rezervace.fnusa.cz"
FNUSA_CALENDARS: Final = {
    KIND_BLOOD: (162, 3),   # "Dárci krve"
    KIND_PLASMA: (162, 2),  # "Plazmaferézy - Objednávky"
}
# Název kalendáře, jak ho uvádí potvrzovací e-mail -> druh odběru
FNUSA_MAIL_CALENDAR_NAMES: Final = {
    "Dárci krve": KIND_BLOOD,
    "Plazmaferézy - Objednávky": KIND_PLASMA,
}
FNUSA_MAIL_SENDER: Final = "nis-noreply@fnusa.cz"
FNUSA_ADMIN_URL: Final = FNUSA_BASE_URL + "/Administration?ReservationKey={key}"

# Popisky pro UI a kalendář
KIND_LABEL: Final = {KIND_BLOOD: "Krev", KIND_PLASMA: "Plazma"}
KIND_EVENT_TITLE: Final = {
    KIND_BLOOD: "🩸 Darování krve",
    KIND_PLASMA: "🩸 Darování plazmy",
}
# Délka odběru v minutách (stejně jako tvoje dosavadní události)
KIND_DURATION_MIN: Final = {KIND_BLOOD: 60, KIND_PLASMA: 90}

EVENT_LOCATION: Final = "Nemocnice u sv. Anny\nPekařská, 602 00 Brno, Česká republika"
EVENT_PLACE_NOTE: Final = (
    "Transfuzní oddělení FN u sv. Anny – odběrové centrum v 1. PP budovy B1.\n"
    "Parkování pro dárce před budovou B1 – na vrátnici nahlásit darování krve."
)

# --- Konfigurace (config entry data) ------------------------------------
CONF_FIRST_NAME: Final = "first_name"
CONF_LAST_NAME: Final = "last_name"
CONF_TITLE_BEFORE: Final = "title_before"
CONF_TITLE_AFTER: Final = "title_after"
CONF_BIRTH_DATE: Final = "birth_date"
CONF_INSURANCE_NO: Final = "insurance_no"
CONF_EMAIL: Final = "email"
CONF_PHONE: Final = "phone"
CONF_WEIGHT: Final = "weight"
CONF_BLOOD_TYPE: Final = "blood_type"

BLOOD_TYPES: Final = ["0-", "0+", "A-", "A+", "B-", "B+", "AB-", "AB+", "uncertain"]

# --- Volby (options) ----------------------------------------------------
OPT_NOTIFY_SERVICE: Final = "notify_service"
OPT_CALENDAR_ID: Final = "calendar_id"
OPT_PREFERRED_LATEST: Final = "preferred_latest_start"
OPT_HORIZON_DAYS: Final = "horizon_days"
OPT_QUIET_START: Final = "quiet_start"
OPT_QUIET_END: Final = "quiet_end"
OPT_BLOOD_INTERVAL_MONTHS: Final = "blood_interval_months"
OPT_BLOOD_TO_PLASMA_DAYS: Final = "blood_to_plasma_days"
OPT_PLASMA_INTERVAL_DAYS: Final = "plasma_interval_days"
OPT_PLASMA_TO_BLOOD_DAYS: Final = "plasma_to_blood_days"
OPT_SLACK_DAYS: Final = "slack_days"
OPT_DASHBOARD_PATH: Final = "dashboard_path"
# Uživatelé HA, kteří smějí modul používat (id). Prázdné = vlastník
# mobilní aplikace, na kterou chodí notifikace.
OPT_ALLOWED_USERS: Final = "allowed_users"

DEFAULT_OPTIONS: Final = {
    OPT_ALLOWED_USERS: [],
    OPT_NOTIFY_SERVICE: "",
    OPT_CALENDAR_ID: "primary",
    OPT_PREFERRED_LATEST: "10:00",
    OPT_HORIZON_DAYS: 90,
    OPT_QUIET_START: "21:30",
    OPT_QUIET_END: "07:00",
    OPT_BLOOD_INTERVAL_MONTHS: 3,
    OPT_BLOOD_TO_PLASMA_DAYS: 30,
    OPT_PLASMA_INTERVAL_DAYS: 21,
    OPT_PLASMA_TO_BLOOD_DAYS: 21,
    OPT_SLACK_DAYS: 7,
    OPT_DASHBOARD_PATH: "/dashboard-darovani/prehled",
}

# --- Google OAuth --------------------------------------------------------
GOOGLE_AUTHORIZE_URL: Final = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL: Final = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES: Final = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

# --- Intervaly dotazování -----------------------------------------------
TERMS_POLL_MINUTES: Final = 20
MAIL_POLL_MINUTES: Final = 30
MAIL_POLL_FAST_MINUTES: Final = 2  # po rezervaci, dokud nedorazí potvrzení

# --- Notifikace ----------------------------------------------------------
NOTIFY_GROUP: Final = "darovani"
ACTION_PREFIX: Final = "DAROVANI"
ACTION_BOOK: Final = f"{ACTION_PREFIX}_BOOK"      # DAROVANI_BOOK|krev|2026-12-01T06:15
ACTION_OTP: Final = f"{ACTION_PREFIX}_OTP"        # textInput s kódem z SMS
ACTION_LATER: Final = f"{ACTION_PREFIX}_LATER"    # odložit nabídku o 24 h
ACTION_UNDO_DONE: Final = f"{ACTION_PREFIX}_NOT_DONE"

# --- Služby --------------------------------------------------------------
SERVICE_REFRESH: Final = "refresh"
SERVICE_SELECT_SLOT: Final = "select_slot"
SERVICE_SUBMIT_CODE: Final = "submit_code"
SERVICE_RESEND_CODE: Final = "resend_code"
SERVICE_ABORT: Final = "abort"
SERVICE_CANCEL: Final = "cancel_reservation"
SERVICE_POSTPONE_PREVIEW: Final = "postpone_preview"
SERVICE_POSTPONE_APPLY: Final = "postpone_apply"
SERVICE_CLEAR_DEFERRAL: Final = "clear_deferral"
SERVICE_MARK_NOT_DONE: Final = "mark_not_done"
SERVICE_ADD_HISTORY: Final = "add_history"

# --- Stavy ---------------------------------------------------------------
RES_RESERVED: Final = "reserved"
RES_CANCELLED: Final = "cancelled"
RES_DONE: Final = "done"
RES_SKIPPED: Final = "skipped"   # rezervováno, ale odběr neproběhl

STATUS_OK: Final = "ok"
STATUS_TO_BOOK: Final = "to_book"
STATUS_WAITING_CODE: Final = "waiting_code"
STATUS_BOOKING: Final = "booking"
STATUS_ERROR: Final = "error"
STATUS_SETUP: Final = "setup"

SIGNAL_UPDATE: Final = f"{DOMAIN}_update"
STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.state"
