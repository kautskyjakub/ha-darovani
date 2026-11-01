"""Senzory: další odběr, stav, další krok, poslední krev/plazma, počet odběrů."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    KIND_BLOOD,
    KIND_LABEL,
    KIND_PLASMA,
    STATUS_BOOKING,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_TO_BOOK,
    STATUS_WAITING_CODE,
)
from .entity import DarovaniEntity


async def async_setup_entry(hass: HomeAssistant, entry: Any, async_add_entities: AddEntitiesCallback) -> None:
    m = entry.runtime_data
    async_add_entities(
        [
            NextDonationSensor(m),
            StatusSensor(m),
            NextStepSensor(m),
            LastSensor(m, KIND_BLOOD, "last_blood"),
            LastSensor(m, KIND_PLASMA, "last_plasma"),
            CountSensor(m),
        ]
    )


class NextDonationSensor(DarovaniEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:water"

    def __init__(self, manager) -> None:
        super().__init__(manager, "next_donation")

    @property
    def native_value(self) -> datetime | None:
        up = self.manager.upcoming()
        return dt_util.parse_datetime(up[0]["start"]) if up else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        up = self.manager.upcoming()
        if not up:
            return {}
        view = self.manager._res_view(up[0])  # noqa: SLF001
        return {
            "druh": KIND_LABEL[view["kind"]],
            "kind": view["kind"],
            "rezervace_id": view["id"],
            "sprava_rezervace": view["admin_url"],
        }


class StatusSensor(DarovaniEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_OK, STATUS_TO_BOOK, STATUS_WAITING_CODE, STATUS_BOOKING, STATUS_ERROR]
    _attr_icon = "mdi:clipboard-check-outline"

    def __init__(self, manager) -> None:
        super().__init__(manager, "status")

    @property
    def native_value(self) -> str:
        return self.manager.status()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snap_status, message = self.manager.status()
        return {"zprava": message, "odklad_do": self.manager.data.get("blocked_until")}


class NextStepSensor(DarovaniEntity, SensorEntity):
    _attr_icon = "mdi:calendar-arrow-right"

    def __init__(self, manager) -> None:
        super().__init__(manager, "next_step")

    @property
    def native_value(self) -> str | None:
        step = self.manager.next_bookable()
        if not step:
            return None
        return f"{KIND_LABEL[step.kind]} od {step.earliest.day}. {step.earliest.month}. {step.earliest.year}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        step = self.manager.next_bookable()
        if not step:
            return {}
        slots = self.manager.slots_for(step.kind, step.earliest)
        return {
            "kind": step.kind,
            "nejdrive": step.earliest.isoformat(),
            "volnych_terminu": len(slots),
            "prvni_volny": slots[0].isoformat() if slots else None,
        }


class LastSensor(DarovaniEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, manager, kind: str, key: str) -> None:
        super().__init__(manager, key)
        self._kind = kind
        self._attr_icon = "mdi:water-check" if kind == KIND_BLOOD else "mdi:water-plus"

    @property
    def native_value(self) -> datetime | None:
        return self.manager.last_of(self._kind)


class CountSensor(DarovaniEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:counter"
    _attr_native_unit_of_measurement = "odběrů"

    def __init__(self, manager) -> None:
        super().__init__(manager, "donations")

    @property
    def native_value(self) -> int:
        return self.manager.done_count()
