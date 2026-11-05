"""Camera entities backed by V380's cloud transport."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from copy import copy
from datetime import datetime
import logging
from typing import TYPE_CHECKING

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import V380CloudCoordinator, V380ConfigEntry
from .api import (
    AuthError,
    CameraUnavailableError,
    CloudDevice,
    CloudError,
    PlaybackNotReadyError,
    UnsupportedError,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.components.stream import Stream

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
        self._stream_lifecycle_lock = asyncio.Lock()
        self._retiring_stream: Stream | None = None
        self._source: str | None = None
        self._source_refresh_at = 0.0
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
        """Keep cached video available through transient metadata poll failures."""
        client = self._entry.runtime_data.client
        device = client.devices.get(self._device_id)
        return (
            not self._removing and client.cached_devices_available
            and device is not None and device.online is not False
            and (self.stream is None or self.stream.available)
        )

    @property
    def use_stream_for_stills(self) -> bool:
        """Let Home Assistant derive thumbnails from its shared stream."""
        return True

    async def _async_get_supported_webrtc_provider[_T](
        self, fn: Callable[[HomeAssistant, Camera], Coroutine[None, None, _T | None]]
    ) -> _T | None:
        """Keep this authenticated MPEG-TS proxy on HA's shared HLS pipeline.

        Automatic HTTP-scheme matching does not establish that a provider can
        manage this source's signature renewal or reuse HA's existing stream.
        V380 has no native WebRTC implementation; Core still advertises HLS.
        """
        return None

    async def stream_source(self) -> str | None:
        """Return the active source without postponing its renewal deadline."""
        return await self._async_source()

    async def async_create_stream(self) -> Stream | None:
        """Reuse active workers; retire idle ones before adding new HLS outputs."""
        async with self._stream_lifecycle_lock:
            if self._removing:
                return None
            if stream := self.stream:
                outputs = stream.outputs()
                if not outputs or (
                    not stream.dynamic_stream_settings.preload_stream
                    and all(output.idle for output in outputs.values())
                ):
                    # An old worker's queued cleanup must not remove a new
                    # viewer's HLS output from this same Stream object.
                    self.stream = None
                    self._retiring_stream = stream
                    try:
                        await self._stop_stream(stream)
                    finally:
                        self._retiring_stream = None
                else:
                    await self.stream_source()
            if self._removing:
                return None
            return await super().async_create_stream()

    @staticmethod
    async def _stop_stream(stream: Stream) -> None:
        """Join a worker even when the camera used preloading."""
        settings = copy(stream.dynamic_stream_settings)
        settings.preload_stream = False
        stream.dynamic_stream_settings = settings
        await stream.stop()

    async def _async_source(self, *, force: bool = False) -> str | None:
        """Issue and apply a source under one lock, with one fixed deadline."""
        async with self._source_lock:
            if self._removing:
                return None
            loop = asyncio.get_running_loop()
            now = loop.time()
            if not force and self._source is not None and now < self._source_refresh_at:
                if self._refresh_source_unsub is None:
                    self._schedule_source_refresh(self._source_refresh_at - now)
                return self._source
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
            except CameraUnavailableError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="camera_unavailable"
                ) from err
            except PlaybackNotReadyError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="playback_not_ready"
                ) from err
            except CloudError as err:
                # Diagnostics contain fixed enums and bounded numbers only.
                # Do not log exception text, request URLs or response bodies.
                _LOGGER.debug("V380 live source preparation failed: %s", err.diagnostic)
                key = (
                    "camera_credentials_unavailable"
                    if err.diagnostic.get("stage") == "device_credentials"
                    else "cloud_unavailable"
                )
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key=key
                ) from err
            if self._removing:
                return None
            self._source = source
            self._source_refresh_at = loop.time() + _SOURCE_REFRESH_SECONDS
            if self.stream is not None:
                # Authentication is checked when opening the HTTP request.
                # Renew the public reconnect source without interrupting the
                # current connection; update_source() stops HA's worker.
                self.stream.source = source
            self._schedule_source_refresh(_SOURCE_REFRESH_SECONDS)
            return source

    @callback
    def _schedule_source_refresh(self, delay: float) -> None:
        """Schedule renewal only when a source changes or a retry is needed."""
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
            await self._async_source(force=True)
        except HomeAssistantError as err:
            if err.translation_key in {
                "cloud_unavailable", "camera_unavailable", "playback_not_ready"
            }:
                _LOGGER.debug("Unable to renew the V380 cloud camera source; retrying")
                self._schedule_source_refresh(_SOURCE_RETRY_SECONDS)
            return

    async def async_will_remove_from_hass(self) -> None:
        """Cancel source renewal and stop this entity's stream on unload."""
        self._removing = True
        self._source = None
        if self._refresh_source_unsub is not None:
            self._refresh_source_unsub()
            self._refresh_source_unsub = None
        streams = [stream for stream in (self.stream, self._retiring_stream) if stream is not None]
        self.stream = None
        for stream in streams:
            await self._stop_stream(stream)
        await super().async_will_remove_from_hass()

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Images are produced by the stream integration, when supported."""
        return None

