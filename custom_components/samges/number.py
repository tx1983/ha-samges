"""Number entities for entering new meter readings.

One box per tariff zone of every meter. Typing a value does not send it
immediately — it is kept as «pending» and is POSTed together with the
other zones of the same meter when its «Передать показания» button is
pressed, mirroring the cabinet's single submission form.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SamGesConfigEntry, SamGesCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

KWH = UnitOfEnergy.KILO_WATT_HOUR


def _float(value: Any) -> float | None:
    """Convert stringy numbers ("12799", "12799.5") to float."""
    try:
        return float(str(value).replace(",", ".").replace(" ", "").strip())
    except (TypeError, ValueError):
        return None


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value) if value else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamGesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание полей ввода показаний по каждой зоне счётчика."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    numbers = [
        SamGesReadingNumber(coordinator, account_id, counter, zone, index)
        for counter in data.get("counters") or []
        for index, zone in enumerate(counter.get("zones") or ["0"])
    ]
    async_add_entities(numbers)
    _LOGGER.debug("Создано %d полей ввода показаний SamGES", len(numbers))


class SamGesReadingNumber(CoordinatorEntity[SamGesCoordinator], NumberEntity):
    """Input box for a new reading of one tariff zone of a meter."""

    _attr_has_entity_name = True
    _attr_device_class = NumberDeviceClass.ENERGY
    _attr_native_unit_of_measurement = KWH
    _attr_native_min_value = 0
    _attr_native_max_value = 1_000_000
    _attr_native_step = 1
    _attr_mode = "box"
    _attr_icon = "mdi:meter-electric-outline"

    def __init__(
        self,
        coordinator: SamGesCoordinator,
        account_id: str,
        counter: dict[str, Any],
        zone: str,
        zone_index: int,
    ):
        super().__init__(coordinator)
        self.account_id = account_id
        self._counter_id = str(
            counter.get("id") or counter.get("serial") or "meter"
        )
        self._zone = str(zone)
        multi = len(counter.get("zones") or []) > 1
        self._attr_name = "Новое показание" + (
            f" Т{zone_index + 1}" if multi else ""
        )
        self._attr_unique_id = (
            f"{DOMAIN}_{self._counter_id}_new_reading_{self._zone}"
        )
        self._attr_device_info = {
            "identifiers": {(DOMAIN, f"counter_{self._counter_id}")},
            "via_device": (DOMAIN, self.account_id),
            "name": counter.get("name")
            or f"Счётчик {counter.get('serial') or ''}".strip(),
            "manufacturer": "СамГЭС",
            "model": counter.get("model") or counter.get("serial"),
            "serial_number": counter.get("serial"),
        }

    def _counter(self) -> dict[str, Any]:
        for counter in (self.coordinator.data or {}).get("counters") or []:
            if str(counter.get("id")) == self._counter_id:
                return counter
        return {}

    @property
    def native_value(self) -> float | None:
        """Typed-in value if pending, else the last reading (single-zone).

        On multi-zone meters there is no per-zone «last reading» in the
        scraped data, so the box stays empty until a value is typed.
        """
        pending = self.coordinator.pending_for(self._counter_id)
        if self._zone in pending:
            return pending[self._zone]
        counter = self._counter()
        if len(counter.get("zones") or []) <= 1:
            return _float(counter.get("reading"))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        counter = self._counter()
        return {
            "zone": self._zone,
            "pending": self.coordinator.pending_for(self._counter_id).get(
                self._zone
            ),
            "serial": counter.get("serial"),
            "last_reading": _float(counter.get("reading")),
            "last_reading_date": _iso(counter.get("reading_date")),
        }

    async def async_set_native_value(self, value: float) -> None:
        """Keep the value as pending — the button submits it."""
        self.coordinator.set_pending(self._counter_id, self._zone, value)
