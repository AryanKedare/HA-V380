"""Configure a V380 cloud account through Home Assistant's UI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AuthError, CloudError, UnsupportedError, V380CloudClient
from .const import CONF_ALLOW_INSECURE_HTTP, DOMAIN, NAME


def _credentials_schema(
    username: str = "", allow_insecure_http: bool = False
) -> probatio.Schema:
    return probatio.Schema(
        {
            probatio.Required(CONF_USERNAME, default=username): TextSelector(),
            probatio.Required(CONF_PASSWORD): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            ),
            probatio.Optional(
                CONF_ALLOW_INSECURE_HTTP, default=allow_insecure_http
            ): bool,
        }
    )


class V380CloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Authenticate an account and check camera-list access before saving it."""

    VERSION = 1

    async def _async_validate(self, user_input: dict[str, Any]) -> str:
        client = V380CloudClient(
            async_get_clientsession(self.hass),
            user_input[CONF_USERNAME],
            user_input[CONF_PASSWORD],
            allow_insecure_http=user_input.get(CONF_ALLOW_INSECURE_HTTP, False),
        )
        try:
            account_id = await client.async_login()
            await client.async_devices()
            return account_id
        finally:
            await client.async_close()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle initial account setup."""
        return await self._async_credentials(user_input, mode="user")

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let a user update credentials or the legacy HTTP preference."""
        return await self._async_credentials(user_input, mode="reconfigure")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication linked to the existing account entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Replace invalid credentials after validating the same account."""
        return await self._async_credentials(user_input, mode="reauth_confirm")

    async def _async_credentials(
        self, user_input: dict[str, Any] | None, *, mode: str
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = None
        if mode == "reauth_confirm":
            entry = self._get_reauth_entry()
        elif mode == "reconfigure":
            entry = self._get_reconfigure_entry()
        username = entry.data[CONF_USERNAME] if entry else ""
        allow_insecure_http = (
            entry.data.get(CONF_ALLOW_INSECURE_HTTP, False) if entry else False
        )
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            credentials = {
                CONF_USERNAME: username,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_ALLOW_INSECURE_HTTP: user_input.get(
                    CONF_ALLOW_INSECURE_HTTP, allow_insecure_http
                ),
            }
            try:
                if not username or not credentials[CONF_PASSWORD]:
                    raise AuthError("Empty credentials")
                account_id = await self._async_validate(credentials)
            except AuthError:
                errors["base"] = "invalid_auth"
            except UnsupportedError:
                errors["base"] = "unsupported_protocol"
            except CloudError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(account_id)
                if entry is not None:
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                    return self.async_update_reload_and_abort(
                        entry, data_updates=credentials
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=credentials)

        return self.async_show_form(
            step_id=mode,
            data_schema=_credentials_schema(username, allow_insecure_http),
            errors=errors,
        )

