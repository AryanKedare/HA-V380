"""Cloud account, live relay and recording access used by Home Assistant."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, TYPE_CHECKING

import aiohttp

from .account import AccountClient, CloudDevice, CloudRecording, device_password
from .errors import AuthError, CloudError, UnsupportedError
from .live import ForwardedCloudLiveClient, Frame, RelayState

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class CameraUnavailableError(CloudError):
    """The camera is absent from the current account device list."""


class PlaybackNotReadyError(CloudError):
    """The local Home Assistant media proxy has not been bound yet."""


class V380CloudClient(AccountClient):
    def __init__(
        self, session: aiohttp.ClientSession, username: str, password: str,
        *, allow_insecure_http: bool = False,
    ) -> None:
        super().__init__(session, username, password)
        self.allow_insecure_http = allow_insecure_http
        self._hass: HomeAssistant | None = None
        self._entry_id = ""
        self._live_sessions: set[ForwardedCloudLiveClient] = set()
        self._media_tasks: set[asyncio.Task[Any]] = set()
        self._recordings_client: Any = None
        self._account_auth_failed = False
        self.live_stream_failures = 0
        self.last_live_failure: dict[str, Any] = {}
        self._relay_states: dict[str, RelayState] = {}
        self._live_startup_failures: dict[str, int] = {}
        self._live_recovery_lock = asyncio.Lock()
        self._live_recovery_after = 0.0
        self.live_session_recovery_attempts = 0

    @property
    def cached_devices_available(self) -> bool:
        """Allow known devices through transient metadata/session refresh errors."""
        return bool(self.account_id) and not (
            self._closed or self._credentials_rejected or self._account_auth_failed
        )

    async def async_devices(self) -> list[CloudDevice]:
        try:
            devices = await super().async_devices()
        except AuthError:
            self._account_auth_failed = True
            raise
        self._account_auth_failed = False
        self._relay_states = {key: value for key, value in self._relay_states.items() if key in self.devices}
        return devices

    def record_live_failure(self, error: CloudError, *, phase: str,
                            progress: dict[str, Any] | None = None,
                            device_id: str | None = None) -> dict[str, Any]:
        """Store only revalidated enums/numbers, never exception text or URLs."""
        fields = {key: error.diagnostic.get(key) for key in (
            "stage", "code", "error_id", "protocol_version", "packet_type",
            "relay_command", "relay_status",
        )}
        fields["reason"] = error.diagnostic.get("detail")
        progress = progress or {}
        if not fields['stage'] and isinstance(progress.get('stage'), str):
            fields['stage'] = progress['stage']
        safe = dict(CloudError(**fields).diagnostic)
        safe["phase"] = phase if phase in ("startup", "stream") else "unknown"
        for name in ('relays_tried', 'media_headers', 'video_frames', 'keyframes',
                     'transport_reconnects', 'metadata_refreshes'):
            value = progress.get(name)
            if type(value) is int and 0 <= value <= 65535:
                safe[name] = value
        for name in ('cached_relay_used', 'metadata_refresh_failed'):
            if type(progress.get(name)) is bool:
                safe[name] = progress[name]
        if (phase == 'startup' and safe.get('detail') == 'network_timeout'
                and device_id in self.devices):
            self._live_startup_failures[device_id] = min(self._live_startup_failures.get(device_id, 0) + 1, 65535)
        self.live_stream_failures = min(self.live_stream_failures + 1, 65535)
        self.last_live_failure = safe
        return dict(safe)

    def bind_home_assistant(self, hass: HomeAssistant, entry_id: str) -> None:
        """Keep HA-specific URL signing outside the protocol implementation."""
        self._hass, self._entry_id = hass, entry_id

    async def _device(self, device_id: str) -> CloudDevice:
        if device_id not in self.devices:
            await self.async_devices()
        try:
            return self.devices[device_id]
        except KeyError:
            raise CameraUnavailableError("The camera is no longer available to this account") from None

    def _source(self, kind: str, device_id: str, recording_id: str = "") -> str:
        if self._hass is None:
            raise PlaybackNotReadyError("The Home Assistant media proxy has not been initialized")
        from .proxy import signed_media_url
        return signed_media_url(self._hass, self._entry_id, kind, device_id, recording_id)

    async def async_live_source(self, device_id: str) -> str:
        device = await self._device(device_id)
        # Fail during camera source resolution, with a meaningful HA error.
        if int(device.metadata.get("type", 0)) >= 2:
            raise UnsupportedError("This camera uses the newer cloud key-exchange video protocol")
        device_password(device.metadata)
        return self._source("live", device_id)

    async def _recover_live_startup(self, device_id: str, progress: dict[str, Any]) -> None:
        """Perform the session reset provided by reload, at most every 5 minutes."""
        if not self._live_startup_failures.get(device_id):
            return
        loop = asyncio.get_running_loop()
        async with self._live_recovery_lock:
            if (self._closed or not self._live_startup_failures.get(device_id)
                    or loop.time() < self._live_recovery_after):
                return
            self._live_recovery_after = loop.time() + 300
            self.live_session_recovery_attempts = min(self.live_session_recovery_attempts + 1, 65535)
            progress.update(stage='account_refresh', metadata_refreshes=1)
            try:
                await self._recover_login(self._auth_generation)
                await self.async_devices()
            except AuthError:
                self._account_auth_failed = True
                raise
            except CloudError:
                # Cached device credentials can still stream during a separate
                # account API outage. Keep trying relays within the same budget.
                progress['metadata_refresh_failed'] = True
            else:
                self._live_startup_failures.clear()

    async def async_live_frames(self, device_id: str, *,
                                progress: dict[str, Any] | None = None) -> AsyncIterator[Frame]:
        progress = progress if progress is not None else {}
        progress['stage'] = 'device_credentials'
        await self._recover_live_startup(device_id, progress)
        device = await self._device(device_id)
        for credential_attempt in range(2):
            state = self._relay_states.setdefault(device_id, RelayState())
            live = ForwardedCloudLiveClient(device.metadata, device_password(device.metadata),
                                            audio=False, relay_state=state, progress=progress)
            self._live_sessions.add(live)
            frames = live.async_frames()
            try:
                async for frame in frames:
                    if frame.keyframe and frame.codec in ('h264', 'hevc', 'h265'):
                        self._live_startup_failures.pop(device_id, None)
                    yield frame
                return
            except AuthError as err:
                if err.diagnostic.get("stage") != "device_login_response":
                    raise
                if credential_attempt:
                    # Camera credentials are separate from the cloud account.
                    # Refresh once, then report a device error without starting
                    # an unrelated account-password reauthentication flow.
                    raise CloudError("The camera rejected its refreshed device credentials",
                        stage="device_login_response", reason="login_rejected",
                        code=err.diagnostic.get("code"), error_id=err.diagnostic.get("error_id")) from err
            finally:
                await frames.aclose()
                await live.async_close()
                self._live_sessions.discard(live)
            await self.async_devices()
            device = await self._device(device_id)

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
        self._relay_states.clear()
        self._live_startup_failures.clear()
        if self._recordings_client is not None:
            self._recordings_client.clear()
        await super().async_close()
