"""Data update coordinator."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
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
