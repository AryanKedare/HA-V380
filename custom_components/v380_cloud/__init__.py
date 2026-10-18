"""Connect V380 cloud accounts to Home Assistant.

Cloud protocol support is experimental. This module never substitutes local
camera URLs for a cloud operation that the protocol client cannot perform.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, CloudDevice, CloudError, V380CloudClient
from .const import CONF_ALLOW_INSECURE_HTTP, DEFAULT_UPDATE_INTERVAL_SECONDS, DOMAIN, NAME

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.CAMERA]


class V380CloudCoordinator(DataUpdateCoordinator[dict[str, CloudDevice]]):
    """Refresh account camera metadata without opening live streams."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: V380CloudClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=NAME,
            update_interval=timedelta(seconds=DEFAULT_UPDATE_INTERVAL_SECONDS),
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> dict[str, CloudDevice]:
        try:
            devices = await self.client.async_devices()
        except AuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except CloudError as err:
            # Exception text may contain vendor URLs or authentication material.
            raise UpdateFailed("Unable to refresh V380 cloud cameras") from err
        return {device.device_id: device for device in devices}


@dataclass(slots=True)
class V380RuntimeData:
    """Authenticated client and camera metadata for one configured account."""

    client: V380CloudClient
    coordinator: V380CloudCoordinator
    account_id: str


type V380ConfigEntry = ConfigEntry[V380RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: V380ConfigEntry) -> bool:
    """Authenticate and load an account's camera platform."""
    client = V380CloudClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        allow_insecure_http=entry.data.get(CONF_ALLOW_INSECURE_HTTP, False),
    )
    try:
        account_id = await client.async_login()
        if entry.unique_id is not None and account_id != entry.unique_id:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="account_mismatch"
            )
        coordinator = V380CloudCoordinator(hass, entry, client)
        await coordinator.async_config_entry_first_refresh()
    except AuthError as err:
        await client.async_close()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_auth"
        ) from err
    except CloudError as err:
        await client.async_close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cloud_unavailable"
        ) from err
    except BaseException:
        await client.async_close()
        raise

    from .proxy import register_views

    register_views(hass)
    client.bind_home_assistant(hass, entry.entry_id)
    runtime = V380RuntimeData(client, coordinator, account_id)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await client.async_close()
        raise

    async def async_stop(event: Event) -> None:
        """Release cloud transport resources when Home Assistant stops."""
        await client.async_close()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: V380ConfigEntry) -> bool:
    """Unload entities before releasing their cloud sessions."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    await entry.runtime_data.client.async_close()
    return True

