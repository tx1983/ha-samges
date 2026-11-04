"""Button entity submitting pending meter readings."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SamGesConfigEntry, SamGesCoordinator
from .parser import cell_number

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SamGesConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создание кнопки «Передать показания» на каждый счётчик."""
    coordinator = entry.runtime_data
    data = coordinator.data or {}
    account_id = str(data.get("account_id") or entry.unique_id or "account")

    buttons = [
        SamGesSubmitButton(coordinator, account_id, counter)
        for counter in data.get("counters") or []
    ]
    async_add_entities(buttons)


class SamGesSubmitButton(CoordinatorEntity[SamGesCoordinator], ButtonEntity):
    """«Передать показания» — POSTs all pending values of the meter.

    Unavailable (greyed out) while no new values were typed into the
    meter's number inputs.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:send-check"

    def __init__(
        self,
        coordinator: SamGesCoordinator,
        account_id: str,
        counter: dict[str, Any],
    ):
        super().__init__(coordinator)
        self.account_id = account_id
        self._counter_id = str(
            counter.get("id") or counter.get("serial") or "meter"
        )
        self._attr_name = "Передать показания"
        self._attr_unique_id = f"{DOMAIN}_{self._counter_id}_submit"
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

    def _unchanged(self, pending: dict[str, float]) -> bool:
        """Pending value identical to the last reading — nothing to send.

        Only single-zone meters can be compared: the scraped data keeps
        one «last reading» per meter, not per tariff zone. A value lower
        than the last one still enables the button (meter replacement /
        correction are legitimate edge cases).
        """
        counter = self._counter()
        zones = counter.get("zones") or ["0"]
        last = cell_number(str(counter.get("reading")))
        if len(zones) != 1 or last is None:
            return False
        return pending.get(zones[0]) == last

    @property
    def available(self) -> bool:
        pending = self.coordinator.pending_for(self._counter_id)
        return (
            super().available
            and bool(pending)
            and not self._unchanged(pending)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"pending": self.coordinator.pending_for(self._counter_id)}

    async def async_press(self) -> None:
        """POST every pending zone of this meter in one form submit."""
        pending = self.coordinator.pending_for(self._counter_id)
        if not pending:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_pending_readings",
            )
        await self.coordinator.async_submit_readings(
            {self._counter_id: dict(pending)}
        )
