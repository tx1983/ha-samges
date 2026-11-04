"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SamGesAuthError, SamGesClient, SamGesError
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN

_LOGGER = logging.getLogger(__name__)


class SamGesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetches the account summary, counters and accruals pages."""

    def __init__(
        self, hass: HomeAssistant, entry: SamGesConfigEntry, client: SamGesClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                minutes=entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
                )
            ),
        )
        self.client = client
        # Values typed into the reading NumberEntities, waiting for the
        # submit button: {counter_id: {tariff_zone: value}}.
        self.pending: dict[str, dict[str, float]] = {}

    def set_pending(self, counter_id: str, zone: str, value: float) -> None:
        """Store a typed-in reading and refresh the entity states."""
        self.pending.setdefault(counter_id, {})[zone] = value
        self.async_update_listeners()

    def pending_for(self, counter_id: str) -> dict[str, float]:
        """Pending readings of one meter ({} while nothing was typed)."""
        return self.pending.get(counter_id) or {}

    async def async_submit_readings(
        self, readings: dict[str, dict[str, float]]
    ) -> None:
        """POST readings to the cabinet and refresh the data."""
        try:
            await self.client.async_submit_readings(readings)
        except SamGesAuthError as err:
            raise ConfigEntryAuthFailed(
                f"SamGES credentials rejected: {err}"
            ) from err
        except SamGesError as err:
            raise HomeAssistantError(str(err)) from err
        # Drop the pending slots that were just accepted.
        for counter_id, zones in readings.items():
            stored = self.pending.get(counter_id)
            if not stored:
                continue
            for zone, value in zones.items():
                if stored.get(zone) == value:
                    stored.pop(zone)
            if not stored:
                self.pending.pop(counter_id, None)
        self.async_update_listeners()  # grey out the button right away
        await self.async_request_refresh()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.async_get_data()
        except SamGesAuthError as err:
            raise ConfigEntryAuthFailed(
                f"SamGES credentials rejected: {err}"
            ) from err
        except SamGesError as err:
            raise UpdateFailed(str(err)) from err
        _LOGGER.debug(
            "Обновлено: ЛС %s, счётчиков %d, начислений %d",
            data.get("account_id"),
            len(data.get("counters") or []),
            len(data.get("accruals") or []),
        )
        return data


type SamGesConfigEntry = ConfigEntry[SamGesCoordinator]
