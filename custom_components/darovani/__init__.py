"""Darování krve a plazmy – FN u sv. Anny v Brně.

Integrace hlídá volné termíny, plánuje odběry podle pravidel, rezervuje
a ruší termíny (potvrzení SMS kódem), drží Google kalendář v souladu
a ukládá odkazy pro správu rezervace z potvrzovacích e-mailů.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, Unauthorized
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import FnusaClient, FnusaError
from .const import (
    DEFAULT_OPTIONS,
    DOMAIN,
    KINDS,
    OPT_CALENDAR_ID,
    SERVICE_ABORT,
    SERVICE_ADD_HISTORY,
    SERVICE_CANCEL,
    SERVICE_CLEAR_DEFERRAL,
    SERVICE_MARK_NOT_DONE,
    SERVICE_POSTPONE_APPLY,
    SERVICE_POSTPONE_PREVIEW,
    SERVICE_REFRESH,
    SERVICE_RESEND_CODE,
    SERVICE_SELECT_SLOT,
    SERVICE_SUBMIT_CODE,
    VERSION,
)
from .google import GoogleClient
from .manager import DonationManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.CALENDAR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
FRONTEND_URL = "/darovani_static"

type DarovaniConfigEntry = ConfigEntry[DonationManager]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Karta na dashboard, služby a websocket – jednou pro celou integraci."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(FRONTEND_URL, str(Path(__file__).parent / "frontend"), False)]
    )
    add_extra_js_url(hass, f"{FRONTEND_URL}/darovani-card.js?v={VERSION}")
    _register_services(hass)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_postpone_preview)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DarovaniConfigEntry) -> bool:
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(hass, entry)
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    calendar_id = {**DEFAULT_OPTIONS, **entry.options}[OPT_CALENDAR_ID]
    google = GoogleClient(oauth, calendar_id)
    fnusa = FnusaClient(async_get_clientsession(hass))

    manager = DonationManager(hass, entry, fnusa, google)
    await manager.async_setup()
    entry.runtime_data = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: DarovaniConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DarovaniConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_unload()
    return ok


def _manager(hass: HomeAssistant) -> DonationManager:
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        return entry.runtime_data
    raise HomeAssistantError("Integrace Darování není nastavená.")


def _owner_only(hass: HomeAssistant, handler: Any) -> Any:
    """Službu smí volat jen vlastník modulu (ne ostatní uživatelé ani automatizace)."""

    async def guarded(call: ServiceCall) -> Any:
        if not _manager(hass).is_allowed(call.context.user_id):
            raise Unauthorized(context=call.context)
        return await handler(call)

    return guarded


def _register_services(hass: HomeAssistant) -> None:
    async def wrap(coro: Any) -> Any:
        try:
            return await coro
        except FnusaError as err:
            raise HomeAssistantError(str(err)) from err

    async def refresh(call: ServiceCall) -> None:
        await wrap(_manager(hass).async_refresh())

    async def select_slot(call: ServiceCall) -> None:
        await wrap(_manager(hass).select_slot(call.data["kind"], call.data["start"]))

    async def submit_code(call: ServiceCall) -> None:
        await wrap(_manager(hass).submit_code(str(call.data["code"])))

    async def resend_code(call: ServiceCall) -> None:
        await wrap(_manager(hass).resend_code())

    async def abort(call: ServiceCall) -> None:
        await wrap(_manager(hass).abort_flow())

    async def cancel(call: ServiceCall) -> None:
        await wrap(_manager(hass).cancel(call.data["reservation_id"]))

    async def postpone_preview(call: ServiceCall) -> ServiceResponse:
        return _manager(hass).postpone_preview(int(call.data["days"]))

    async def postpone_apply(call: ServiceCall) -> ServiceResponse:
        return await wrap(_manager(hass).postpone_apply(int(call.data["days"]), call.data.get("reason")))

    async def clear_deferral(call: ServiceCall) -> None:
        await _manager(hass).clear_deferral()

    async def mark_not_done(call: ServiceCall) -> None:
        await _manager(hass).mark_not_done(call.data["reservation_id"])

    async def add_history(call: ServiceCall) -> None:
        await _manager(hass).add_history(call.data["kind"], call.data["start"])

    kind = vol.In(list(KINDS))
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _owner_only(hass, refresh))
    hass.services.async_register(DOMAIN, SERVICE_SELECT_SLOT, _owner_only(hass, select_slot), vol.Schema({vol.Required("kind"): kind, vol.Required("start"): cv.datetime})
    )
    hass.services.async_register(DOMAIN, SERVICE_SUBMIT_CODE, _owner_only(hass, submit_code), vol.Schema({vol.Required("code"): cv.string}))
    hass.services.async_register(DOMAIN, SERVICE_RESEND_CODE, _owner_only(hass, resend_code))
    hass.services.async_register(DOMAIN, SERVICE_ABORT, _owner_only(hass, abort))
    hass.services.async_register(DOMAIN, SERVICE_CANCEL, _owner_only(hass, cancel), vol.Schema({vol.Required("reservation_id"): cv.string})
    )
    days = vol.All(vol.Coerce(int), vol.Range(min=1, max=120))
    hass.services.async_register(DOMAIN, SERVICE_POSTPONE_PREVIEW, _owner_only(hass, postpone_preview),
        vol.Schema({vol.Required("days"): days}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_POSTPONE_APPLY, _owner_only(hass, postpone_apply),
        vol.Schema({vol.Required("days"): days, vol.Optional("reason"): cv.string}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_CLEAR_DEFERRAL, _owner_only(hass, clear_deferral))
    hass.services.async_register(DOMAIN, SERVICE_MARK_NOT_DONE, _owner_only(hass, mark_not_done), vol.Schema({vol.Required("reservation_id"): cv.string})
    )
    hass.services.async_register(DOMAIN, SERVICE_ADD_HISTORY, _owner_only(hass, add_history),
        vol.Schema({vol.Required("kind"): kind, vol.Required("start"): cv.datetime}),
    )


# --- Websocket pro kartu ---------------------------------------------------


@websocket_api.websocket_command({vol.Required("type"): "darovani/subscribe"})
@websocket_api.async_response
async def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_result(msg["id"])
        connection.send_message(websocket_api.event_message(msg["id"], {"setup": False}))
        return
    manager: DonationManager = entries[0].runtime_data
    if not manager.is_allowed(connection.user.id):
        connection.send_result(msg["id"])
        connection.send_message(websocket_api.event_message(msg["id"], {"setup": True, "forbidden": True}))
        return

    @callback
    def push() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], manager.snapshot()))

    connection.subscriptions[msg["id"]] = manager.add_listener(push)
    connection.send_result(msg["id"])
    push()


@websocket_api.websocket_command(
    {vol.Required("type"): "darovani/postpone_preview", vol.Required("days"): vol.All(int, vol.Range(min=1, max=120))}
)
@callback
def ws_postpone_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    try:
        manager = _manager(hass)
        if not manager.is_allowed(connection.user.id):
            connection.send_error(msg["id"], "unauthorized", "Modul Darování je jen pro jeho vlastníka.")
            return
        connection.send_result(msg["id"], manager.postpone_preview(msg["days"]))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "not_setup", str(err))
