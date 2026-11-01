"""Nastavení integrace: Google účet (Kalendář + Gmail), údaje dárce, volby."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .api import normalize_phone
from .const import (
    BLOOD_TYPES,
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
    DOMAIN,
    GOOGLE_SCOPES,
    OPT_ALLOWED_USERS,
    OPT_INVITE_CALENDAR,
    OPT_INVITE_EMAIL,
    OPT_BLOOD_INTERVAL_MONTHS,
    OPT_BLOOD_TO_PLASMA_DAYS,
    OPT_CALENDAR_ID,
    OPT_DASHBOARD_PATH,
    OPT_HORIZON_DAYS,
    OPT_NOTIFY_SERVICE,
    OPT_PLASMA_INTERVAL_DAYS,
    OPT_PLASMA_TO_BLOOD_DAYS,
    OPT_PREFERRED_LATEST,
    OPT_QUIET_END,
    OPT_QUIET_START,
    OPT_SLACK_DAYS,
)

_LOGGER = logging.getLogger(__name__)


def _person_schema(defaults: dict[str, Any]) -> vol.Schema:
    text = selector.TextSelector()
    return vol.Schema(
        {
            vol.Optional(CONF_TITLE_BEFORE, default=defaults.get(CONF_TITLE_BEFORE, "")): text,
            vol.Required(CONF_FIRST_NAME, default=defaults.get(CONF_FIRST_NAME, "")): text,
            vol.Required(CONF_LAST_NAME, default=defaults.get(CONF_LAST_NAME, "")): text,
            vol.Optional(CONF_TITLE_AFTER, default=defaults.get(CONF_TITLE_AFTER, "")): text,
            vol.Required(CONF_BIRTH_DATE, default=defaults.get(CONF_BIRTH_DATE)): selector.DateSelector(),
            vol.Required(CONF_EMAIL, default=defaults.get(CONF_EMAIL, "")): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
            ),
            vol.Required(CONF_PHONE, default=defaults.get(CONF_PHONE, "+420")): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEL)
            ),
            vol.Required(CONF_WEIGHT, default=defaults.get(CONF_WEIGHT, 80)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=50, max=150, step=1, unit_of_measurement="kg", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_BLOOD_TYPE, default=defaults.get(CONF_BLOOD_TYPE, "uncertain")): selector.SelectSelector(
                selector.SelectSelectorConfig(options=BLOOD_TYPES, translation_key="blood_type")
            ),
            vol.Optional(CONF_INSURANCE_NO, default=defaults.get(CONF_INSURANCE_NO, "")): text,
        }
    )


def _clean_person(user_input: dict[str, Any]) -> dict[str, Any]:
    data = {k: (v.strip() if isinstance(v, str) else v) for k, v in user_input.items()}
    data[CONF_PHONE] = normalize_phone(data[CONF_PHONE])
    data[CONF_WEIGHT] = int(data[CONF_WEIGHT])
    return data


class DarovaniFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Google OAuth -> údaje dárce -> hotovo."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._oauth_data: dict[str, Any] = {}

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent"}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return await super().async_step_user(user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        if self.source == "reauth":
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(entry, data={**entry.data, **data})
        self._oauth_data = data
        return await self.async_step_person()

    async def async_step_person(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            person = _clean_person(user_input)
            if len(person[CONF_PHONE]) < 12:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                await self.async_set_unique_id(DOMAIN)
                return self.async_create_entry(
                    title="Darování krve a plazmy", data={**self._oauth_data, **person}, options=dict(DEFAULT_OPTIONS)
                )
        return self.async_show_form(step_id="person", data_schema=_person_schema(user_input or {}), errors=errors)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DarovaniOptionsFlow()


class DarovaniOptionsFlow(OptionsFlow):
    """Notifikace, kalendář, preference časů a pravidla."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["preferences", "rules", "person"])

    async def async_step_preferences(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            return self.async_create_entry(data={**current, **user_input})
        notify_services = sorted(
            f"notify.{svc}" for svc in self.hass.services.async_services_for_domain("notify") if svc.startswith("mobile_app_")
        )
        notify_value = current[OPT_NOTIFY_SERVICE] or (notify_services[0] if notify_services else "")
        users = [
            selector.SelectOptionDict(value=u.id, label=u.name or u.id)
            for u in await self.hass.auth.async_get_users()
            if not u.system_generated and u.is_active
        ]
        manager = getattr(self.config_entry, "runtime_data", None)
        allowed_default = list(current.get(OPT_ALLOWED_USERS) or (manager.allowed_users() if manager else []))
        schema = vol.Schema(
            {
                vol.Required(OPT_ALLOWED_USERS, default=allowed_default): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=users, multiple=True, mode=selector.SelectSelectorMode.LIST)
                ),
                vol.Required(OPT_NOTIFY_SERVICE, default=notify_value): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=notify_services, custom_value=True)
                ),
                vol.Required(OPT_CALENDAR_ID, default=current[OPT_CALENDAR_ID]): selector.TextSelector(),
                vol.Required(OPT_PREFERRED_LATEST, default=current[OPT_PREFERRED_LATEST]): selector.TimeSelector(),
                vol.Required(OPT_HORIZON_DAYS, default=current[OPT_HORIZON_DAYS]): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=21, max=180, step=1, unit_of_measurement="dní", mode=selector.NumberSelectorMode.BOX)
                ),
                vol.Required(OPT_QUIET_START, default=current[OPT_QUIET_START]): selector.TimeSelector(),
                vol.Required(OPT_QUIET_END, default=current[OPT_QUIET_END]): selector.TimeSelector(),
                vol.Required(OPT_DASHBOARD_PATH, default=current[OPT_DASHBOARD_PATH]): selector.TextSelector(),
                vol.Optional(OPT_INVITE_EMAIL, default=current[OPT_INVITE_EMAIL]): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
                ),
                vol.Optional(OPT_INVITE_CALENDAR, default=current[OPT_INVITE_CALENDAR]): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="preferences", data_schema=schema)

    async def async_step_rules(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        if user_input is not None:
            return self.async_create_entry(data={**current, **{k: int(v) for k, v in user_input.items()}})

        def num(lo: int, hi: int, unit: str) -> selector.NumberSelector:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(min=lo, max=hi, step=1, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX)
            )

        schema = vol.Schema(
            {
                vol.Required(OPT_BLOOD_INTERVAL_MONTHS, default=current[OPT_BLOOD_INTERVAL_MONTHS]): num(2, 12, "měs."),
                vol.Required(OPT_BLOOD_TO_PLASMA_DAYS, default=current[OPT_BLOOD_TO_PLASMA_DAYS]): num(7, 90, "dní"),
                vol.Required(OPT_PLASMA_INTERVAL_DAYS, default=current[OPT_PLASMA_INTERVAL_DAYS]): num(14, 90, "dní"),
                vol.Required(OPT_PLASMA_TO_BLOOD_DAYS, default=current[OPT_PLASMA_TO_BLOOD_DAYS]): num(7, 90, "dní"),
                vol.Required(OPT_SLACK_DAYS, default=current[OPT_SLACK_DAYS]): num(0, 30, "dní"),
            }
        )
        return self.async_show_form(step_id="rules", data_schema=schema)

    async def async_step_person(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, **_clean_person(user_input)}
            )
            return self.async_create_entry(data=dict(self.config_entry.options))
        return self.async_show_form(step_id="person", data_schema=_person_schema(dict(self.config_entry.data)))
