"""Camera entities backed by V380's cloud transport."""

from __future__ import annotations

import asyncio
from datetime import datetime
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import V380CloudCoordinator, V380ConfigEntry
from .api import AuthError, CloudDevice, CloudError, UnsupportedError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
# HA proxy source signatures last ten minutes. Renew before a worker reconnects
# with an expired source; this does not start a new vendor video session itself.
_SOURCE_REFRESH_SECONDS = 8 * 60
_SOURCE_RETRY_SECONDS = 60


async def async_setup_entry(
    hass: HomeAssistant,
    entry: V380ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add existing cameras and cameras subsequently shared with this account."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def async_add_cameras() -> None:
        new_devices = [
            device
            for device_id, device in coordinator.data.items()
            if device_id not in known
        ]
        known.update(device.device_id for device in new_devices)
        async_add_entities(V380CloudCamera(entry, device) for device in new_devices)

    async_add_cameras()
    entry.async_on_unload(coordinator.async_add_listener(async_add_cameras))


class V380CloudCamera(CoordinatorEntity[V380CloudCoordinator], Camera):
    """Present an authenticated cloud stream as a Home Assistant camera."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM
    _attr_brand = "V380"

    def __init__(self, entry: V380ConfigEntry, device: CloudDevice) -> None:
        super().__init__(entry.runtime_data.coordinator, context=device.device_id)
        Camera.__init__(self)
        self._entry = entry
        self._device_id = device.device_id
        self._source_lock = asyncio.Lock()
        self._refresh_source_unsub: CALLBACK_TYPE | None = None
        self._removing = False
        self._attr_unique_id = f"{entry.runtime_data.account_id}_{device.device_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            manufacturer="V380",
            name=device.name,
        )

    @property
    def available(self) -> bool:
        """Report unavailable when the cloud reports a camera offline."""
        device = self.coordinator.data.get(self._device_id)
        return super().available and device is not None and device.online is not False

    @property
    def use_stream_for_stills(self) -> bool:
        """Let Home Assistant derive thumbnails from its shared stream."""
        return True

    async def stream_source(self) -> str | None:
        """Request a fresh cloud source when Home Assistant creates a stream."""
        async with self._source_lock:
            try:
                source = await self._entry.runtime_data.client.async_live_source(
                    self._device_id
                )
            except AuthError as err:
                self._entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="invalid_auth"
                ) from err
            except UnsupportedError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="unsupported_live"
                ) from err
            except CloudError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="cloud_unavailable"
                ) from err
            self._schedule_source_refresh(_SOURCE_REFRESH_SECONDS)
            return source

    @callback
    def _schedule_source_refresh(self, delay: float) -> None:
        """Keep one refresh timer, including when stills request the source."""
        if self._refresh_source_unsub is not None:
            self._refresh_source_unsub()
            self._refresh_source_unsub = None
        if not self._removing:
            self._refresh_source_unsub = async_call_later(
                self.hass, delay, self._async_refresh_source
            )

    async def _async_refresh_source(self, _now: datetime) -> None:
        """Replace the source of an existing HA stream before its URL expires."""
        self._refresh_source_unsub = None
        if self._removing or self.stream is None:
            return
        try:
            source = await self.stream_source()
        except HomeAssistantError as err:
            if err.translation_key == "cloud_unavailable":
                _LOGGER.debug("Unable to renew the V380 cloud camera source; retrying")
                self._schedule_source_refresh(_SOURCE_RETRY_SECONDS)
            return
        if not self._removing and self.stream is not None and source is not None:
            self.stream.update_source(source)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel source renewal and stop this entity's stream on unload."""
        self._removing = True
        if self._refresh_source_unsub is not None:
            self._refresh_source_unsub()
            self._refresh_source_unsub = None
        if self.stream is not None:
            await self.stream.stop()
            self.stream = None
        await super().async_will_remove_from_hass()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Images are produced by the stream integration, when supported."""
        return None

