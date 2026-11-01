"""Kalendář v HA: proběhlé a rezervované odběry + výhled plánu."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import planner
from .const import EVENT_LOCATION, KIND_EVENT_TITLE, KIND_LABEL, RES_DONE, RES_RESERVED
from .entity import DarovaniEntity


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([DonationCalendar(entry.runtime_data)])


class DonationCalendar(DarovaniEntity, CalendarEntity):
    _attr_icon = "mdi:calendar-heart"

    def __init__(self, manager) -> None:
        super().__init__(manager, "calendar")

    def _events(self) -> list[CalendarEvent]:
        m = self.manager
        events: list[CalendarEvent] = []
        for res in m._res().values():  # noqa: SLF001
            if res["status"] not in (RES_RESERVED, RES_DONE):
                continue
            view = m._res_view(res)  # noqa: SLF001
            # Odkaz pro správu rezervace se sem záměrně nedává – stavy entit
            # vidí všichni uživatelé HA; odkaz je jen na kartě a v Google kalendáři.
            desc = "Proběhlo" if res["status"] == RES_DONE else "Rezervováno"
            events.append(
                CalendarEvent(
                    start=dt_util.parse_datetime(view["start"]),
                    end=dt_util.parse_datetime(view["end"]),
                    summary=KIND_EVENT_TITLE[res["kind"]],
                    location=EVENT_LOCATION.replace("\n", ", "),
                    description=desc,
                    uid=res["id"],
                )
            )
        for step in m.plan():
            if step.state == planner.STEP_RESERVED:
                continue
            events.append(
                CalendarEvent(
                    start=step.earliest,
                    end=step.earliest + timedelta(days=1),
                    summary=f"{KIND_LABEL[step.kind]} – možné od",
                    description="Návrh plánu – zatím nerezervováno.",
                    uid=f"plan-{step.kind}-{step.earliest.isoformat()}",
                )
            )
        return sorted(events, key=lambda e: e.start_datetime_local)

    @property
    def event(self) -> CalendarEvent | None:
        now = dt_util.now()
        for ev in self._events():
            if ev.end_datetime_local >= now:
                return ev
        return None

    async def async_get_events(self, hass: HomeAssistant, start_date: datetime, end_date: datetime) -> list[CalendarEvent]:
        return [
            ev for ev in self._events() if ev.end_datetime_local > start_date and ev.start_datetime_local < end_date
        ]
