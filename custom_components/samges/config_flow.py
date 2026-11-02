"""Config flow: login + password for the lk.samges.ru personal account."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SamGesAuthError, SamGesClient, SamGesError
from .const import (
    CONF_LOGIN,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_LOGIN): str,
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

_SCAN_INTERVAL_SELECTOR = NumberSelector(
    NumberSelectorConfig(
        min=MIN_SCAN_INTERVAL_MINUTES,
        max=1440,
        step=1,
        unit_of_measurement="min",
        mode=NumberSelectorMode.BOX,
    )
)


class SamGesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config Flow для интеграции SamGES."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SamGesOptionsFlow:
        return SamGesOptionsFlow(config_entry)

    async def _validate(
        self, login: str, password: str
    ) -> tuple[str | None, str | None]:
        """Sign in; return (account_id, error_key)."""
        # Own session + cookie jar; closed right after validation.
        session = async_create_clientsession(self.hass, auto_cleanup=False)
        try:
            client = SamGesClient(session, login, password)
            account_id = await client.async_sign_in()
        except SamGesAuthError:
            return None, "invalid_auth"
        except SamGesError as err:
            _LOGGER.debug("Validation failed: %s", err)
            return None, "cannot_connect"
        finally:
            await session.close()
        return account_id, None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Шаг ввода логина и пароля личного кабинета."""
        errors: dict[str, str] = {}
        if user_input is not None:
            login = user_input[CONF_LOGIN].strip()
            account_id, error = await self._validate(
                login, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(account_id or login.lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"СамГЭС {account_id or login}",
                    data={
                        CONF_LOGIN: login,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-enter the password — the stored one no longer signs in."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            login = user_input[CONF_LOGIN].strip()
            _account_id, error = await self._validate(
                login, user_input[CONF_PASSWORD]
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_LOGIN: login,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_LOGIN: entry.data.get(CONF_LOGIN, "")},
            ),
            errors=errors,
        )


class SamGesOptionsFlow(OptionsFlow):
    """Scan interval option."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL_MINUTES
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=current
                    ): _SCAN_INTERVAL_SELECTOR
                }
            ),
        )
