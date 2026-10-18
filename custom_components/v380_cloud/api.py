"""Cloud account, live relay and recording access used by Home Assistant."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

import aiohttp

from .account import AccountClient, CloudDevice, CloudRecording, device_password
from .errors import AuthError, CloudError, UnsupportedError
from .live import CloudLiveClient, Frame

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class V380CloudClient(AccountClient):
    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str,
        *, allow_insecure_http: bool = False,
    ) -> None:
        super().__init__(session, username, password)
        self.allow_insecure_http = allow_insecure_http
        self._hass: HomeAssistant | None = None
        self._entry_id = ""
        self._live_sessions: set[CloudLiveClient] = set()
        self._media_tasks: set[asyncio.Task[Any]] = set()
        self._recordings_client: Any = None

    def bind_home_assistant(self, hass: HomeAssistant, entry_id: str) -> None:
        """Keep HA-specific URL signing outside the protocol implementation."""
        self._hass, self._entry_id = hass, entry_id

    async def _device(self, device_id: str) -> CloudDevice:
        if device_id not in self.devices:
            await self.async_devices()
        try:
            return self.devices[device_id]
        except KeyError:
            raise CloudError("The camera is no longer available to this account") from None

    def _source(self, kind: str, device_id: str, recording_id: str = "") -> str:
        if self._hass is None:
            raise CloudError("The Home Assistant media proxy has not been initialized")
        from .proxy import signed_media_url
        return signed_media_url(self._hass, self._entry_id, kind, device_id, recording_id)

    async def async_live_source(self, device_id: str) -> str:
        device = await self._device(device_id)
        # Fail during camera source resolution, with a meaningful HA error.
        if int(device.metadata.get("type", 0)) >= 2:
            raise UnsupportedError("This camera uses the newer cloud key-exchange video protocol")
        device_password(device.metadata)
        return self._source("live", device_id)

    async def async_live_frames(self, device_id: str) -> AsyncIterator[Frame]:
        device = await self._device(device_id)
        live = CloudLiveClient(device.metadata, device_password(device.metadata), audio=False)
        self._live_sessions.add(live)
        try:
            async for frame in live.async_frames():
                yield frame
        finally:
            await live.async_close()
            self._live_sessions.discard(live)

    def _recordings(self) -> Any:
        if self._recordings_client is None:
            from .recordings import CloudRecordings
            self._recordings_client = CloudRecordings(
                self, allow_insecure_http=self.allow_insecure_http,
            )
        return self._recordings_client

    async def async_recording_dates(self, device_id: str) -> list[str]:
        await self._device(device_id)
        return await self._recordings().async_dates(device_id)

    async def async_recordings(self, device_id: str, date: str) -> list[CloudRecording]:
        await self._device(device_id)
        return await self._recordings().async_recordings(device_id, date)

    async def async_recording_source(self, device_id: str, recording_id: str) -> str:
        await self._device(device_id)
        # Root proxy resolves and refreshes vendor credentials on each request.
        # Validate ownership before signing, so identifiers cannot cross cameras.
        self._recordings().validate_recording(device_id, recording_id)
        return self._source("recording", device_id, recording_id)

    async def async_recording_download(self, device_id: str, recording_id: str) -> Any:
        await self._device(device_id)
        self._recordings().validate_recording(device_id, recording_id)
        return await self._recordings().async_source(device_id, recording_id)

    def register_media_task(self) -> None:
        if task := asyncio.current_task():
            self._media_tasks.add(task)

    def unregister_media_task(self) -> None:
        if task := asyncio.current_task():
            self._media_tasks.discard(task)

    async def async_close(self) -> None:
        tasks = [task for task in self._media_tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(live.async_close() for live in self._live_sessions), return_exceptions=True)
        self._live_sessions.clear()
        self._media_tasks.clear()
        if self._recordings_client is not None:
            self._recordings_client.clear()
        await super().async_close()
