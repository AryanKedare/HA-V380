"""Exercise the adapter against real Home Assistant classes, without vendor calls.

These tests validate the Home Assistant boundary. They do not prove that V380's
cloud service accepts the recovered protocol or produces playable footage.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import pytest

pytest.importorskip("homeassistant")
from homeassistant import loader
from homeassistant.components.camera import CameraEntityFeature
from homeassistant.components.media_player import BrowseError
from homeassistant.components.media_source import MediaSourceItem, Unresolvable
from homeassistant.config_entries import (
    ConfigEntries,
    ConfigEntry,
    ConfigEntryState,
    SOURCE_REAUTH,
    SOURCE_USER,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import frame

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.v380_cloud import (
    V380CloudCoordinator,
    V380RuntimeData,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.v380_cloud.api import AuthError, CloudDevice, CloudError, CloudRecording, UnsupportedError
from custom_components.v380_cloud.camera import V380CloudCamera
from custom_components.v380_cloud.config_flow import V380CloudConfigFlow
from custom_components.v380_cloud.const import CONF_ALLOW_INSECURE_HTTP, DOMAIN
from custom_components.v380_cloud.diagnostics import async_get_config_entry_diagnostics
from custom_components.v380_cloud.media_source import V380MediaSource

CREDS = {"username": "test@example.invalid", "password": "fixture-password"}


@asynccontextmanager
async def _hass(tmp_path: Path):
    """Create a real isolated HA core and config-entry manager."""
    (tmp_path / "custom_components").symlink_to(ROOT / "custom_components", target_is_directory=True)
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    hass.data["fixture_entries"] = []
    async with aiohttp.ClientSession() as session:
        # Avoid initializing HA's multicast DNS/network discovery in unit tests.
        # This remains a real aiohttp session; the vendor client is mocked below.
        with (
            patch("custom_components.v380_cloud.async_get_clientsession", return_value=session),
            patch("custom_components.v380_cloud.config_flow.async_get_clientsession", return_value=session),
        ):
            try:
                yield hass
            finally:
                # These tests call the integration's entry functions directly.
                # Mirror ConfigEntry.async_unload's callback phase as well, so
                # coordinators and entry-owned tasks are fully released.
                for entry in hass.data["fixture_entries"]:
                    await entry._async_process_on_unload(hass)
                await hass.async_stop(force=True)


def _client() -> SimpleNamespace:
    """Mock only the external cloud boundary."""
    return SimpleNamespace(
        bind_home_assistant=Mock(),
        async_login=AsyncMock(return_value="fixture-account"),
        async_devices=AsyncMock(return_value=[CloudDevice("camera/one", "Front door")]),
        async_live_source=AsyncMock(return_value="https://media.example.invalid/live.m3u8"),
        async_recording_dates=AsyncMock(return_value=["2026-10-16", "2026-10-17"]),
        async_recordings=AsyncMock(return_value=[CloudRecording("clip/one", "12:30 motion", "video/mp4")]),
        async_recording_source=AsyncMock(return_value="https://media.example.invalid/clip.mp4?fixture_signature=temporary"),
        async_close=AsyncMock(),
    )


def _entry(hass: HomeAssistant, *, state=ConfigEntryState.NOT_LOADED, data=None) -> ConfigEntry:
    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Fixture account",
        data=data or CREDS,
        source=SOURCE_USER,
        options={},
        unique_id="fixture-account",
        state=state,
        discovery_keys=MappingProxyType({}),
        subentries_data=(),
    )
    hass.data["fixture_entries"].append(entry)
    return entry


async def _add_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    # The entry registry is real; avoid loading unrelated platforms in unit tests.
    with patch.object(hass.config_entries, "async_setup", AsyncMock(return_value=True)):
        await hass.config_entries.async_add(entry)


def _flow(hass: HomeAssistant, *, source=SOURCE_USER, entry=None) -> V380CloudConfigFlow:
    flow = V380CloudConfigFlow()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "fixture-flow"
    flow.context = {"source": source}
    if entry is not None:
        flow.context["entry_id"] = entry.entry_id
    return flow


@contextmanager
def _observe_reauth():
    """Observe scheduling without replacing a HA class method with MagicMock."""
    calls = []

    def record(entry, hass):
        calls.append((entry, hass))

    with patch.object(ConfigEntry, "async_start_reauth", new=record):
        yield calls


def test_manifest_and_user_flow(tmp_path):
    async def run():
        async with _hass(tmp_path) as hass:
            integration = await loader.async_get_integration(hass, DOMAIN)
            assert integration.manifest["config_flow"] is True
            assert integration.manifest["version"] == "0.1.0"
            assert set(integration.dependencies) == {"http", "media_source", "stream"}
            flow = _flow(hass)
            result = await flow.async_step_user()
            assert result["type"] is FlowResultType.FORM
            submitted = result["data_schema"](CREDS)
            assert submitted[CONF_ALLOW_INSECURE_HTTP] is False
            client = _client()
            with patch("custom_components.v380_cloud.config_flow.V380CloudClient", return_value=client) as constructor:
                result = await flow.async_step_user(submitted)
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert flow.unique_id == "fixture-account"
            assert result["data"] == {**CREDS, CONF_ALLOW_INSECURE_HTTP: False}
            assert constructor.call_args.kwargs[CONF_ALLOW_INSECURE_HTTP] is False
            client.async_devices.assert_awaited_once()
            client.async_close.assert_awaited_once()
    asyncio.run(run())


@pytest.mark.parametrize("error,key", [(AuthError("fixture"), "invalid_auth"), (CloudError("fixture"), "cannot_connect"), (UnsupportedError("fixture"), "unsupported_protocol")])
@pytest.mark.parametrize("stage", ["async_login", "async_devices"])
def test_flow_error_recovery(tmp_path, error, key, stage):
    async def run():
        async with _hass(tmp_path) as hass:
            flow = _flow(hass)
            client = _client()
            operation = getattr(client, stage)
            operation.side_effect = [error, operation.return_value]
            with patch("custom_components.v380_cloud.config_flow.V380CloudClient", return_value=client):
                result = await flow.async_step_user(CREDS)
                assert result["errors"] == {"base": key}
                assert result["type"] is FlowResultType.FORM
                result = await flow.async_step_user(CREDS)
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert client.async_close.await_count == 2
    asyncio.run(run())


def test_duplicate_account_and_reauthentication(tmp_path):
    async def run():
        async with _hass(tmp_path) as hass:
            entry = _entry(hass, data={**CREDS, "preserved_setting": "keep"})
            await _add_entry(hass, entry)
            client = _client()
            with patch("custom_components.v380_cloud.config_flow.V380CloudClient", return_value=client):
                with pytest.raises(AbortFlow) as duplicate:
                    await _flow(hass).async_step_user(CREDS)
                assert duplicate.value.reason == "already_configured"
                flow = _flow(hass, source=SOURCE_REAUTH, entry=entry)
                result = await flow.async_step_reauth(entry.data)
                assert result["step_id"] == "reauth_confirm"
                client.async_login.return_value = "another-account"
                with pytest.raises(AbortFlow) as mismatch:
                    await flow.async_step_reauth_confirm(CREDS)
                assert mismatch.value.reason == "wrong_account"
                assert entry.data["password"] == CREDS["password"]
                client.async_login.return_value = "fixture-account"
                with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
                    result = await flow.async_step_reauth_confirm({**CREDS, "password": "updated", CONF_ALLOW_INSECURE_HTTP: True})
                assert result["type"] is FlowResultType.ABORT
                assert result["reason"] == "reauth_successful"
                assert entry.data["password"] == "updated"
                assert entry.data[CONF_ALLOW_INSECURE_HTTP] is True
                assert entry.data["preserved_setting"] == "keep"
                reload_entry.assert_called_once_with(entry.entry_id)
    asyncio.run(run())


@pytest.mark.parametrize("error,expected", [(AuthError("fixture"), ConfigEntryAuthFailed), (CloudError("fixture"), ConfigEntryNotReady)])
@pytest.mark.parametrize("stage", ["async_login", "async_devices"])
def test_setup_failure_closes_client(tmp_path, error, expected, stage):
    async def run():
        async with _hass(tmp_path) as hass:
            client = _client()
            getattr(client, stage).side_effect = type(error)
            entry = _entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
            with patch("custom_components.v380_cloud.V380CloudClient", return_value=client):
                with pytest.raises(expected):
                    await async_setup_entry(hass, entry)
            client.async_close.assert_awaited_once()
            assert entry.entry_id not in hass.data.get(DOMAIN, {})
    asyncio.run(run())


def test_camera_lifecycle_and_unknown_online_status(tmp_path):
    async def run():
        async with _hass(tmp_path) as hass:
            client = _client()
            entry = _entry(hass, state=ConfigEntryState.SETUP_IN_PROGRESS)
            with (
                patch("custom_components.v380_cloud.V380CloudClient", return_value=client),
                patch("custom_components.v380_cloud.proxy.register_views") as register_views,
                patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
            ):
                assert await async_setup_entry(hass, entry)
            register_views.assert_called_once_with(hass)
            client.bind_home_assistant.assert_called_once_with(hass, entry.entry_id)
            assert entry.runtime_data is hass.data[DOMAIN][entry.entry_id]
            camera = V380CloudCamera(entry, client.async_devices.return_value[0])
            camera.hass = hass
            assert camera.available  # API does not establish an online state.
            assert camera.use_stream_for_stills
            assert camera.supported_features & CameraEntityFeature.STREAM
            first = await camera.stream_source()
            client.async_live_source.return_value = "https://media.example.invalid/renewed.m3u8"
            assert await camera.stream_source() != first
            client.async_live_source.side_effect = UnsupportedError
            with pytest.raises(HomeAssistantError) as unsupported:
                await camera.stream_source()
            assert unsupported.value.translation_key == "unsupported_live"
            client.async_live_source.side_effect = AuthError
            with _observe_reauth() as reauth:
                with pytest.raises(HomeAssistantError) as auth:
                    await camera.stream_source()
            assert auth.value.translation_key == "invalid_auth"
            assert reauth == [(entry, hass)]
            entry.runtime_data.coordinator.async_set_updated_data({"camera/one": CloudDevice("camera/one", "Front door", False)})
            assert not camera.available
            diagnostics = await async_get_config_entry_diagnostics(hass, entry)
            assert diagnostics["offline_camera_count"] == 1
            assert "fixture-password" not in json.dumps(diagnostics)
            with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)):
                assert await async_unload_entry(hass, entry)
            client.async_close.assert_awaited_once()
            assert entry.entry_id not in hass.data[DOMAIN]
    asyncio.run(run())


def test_media_browse_and_fresh_recording_resolution(tmp_path):
    async def run():
        async with _hass(tmp_path) as hass:
            entry = _entry(hass)
            await _add_entry(hass, entry)
            client = _client()
            coordinator = V380CloudCoordinator(hass, entry, client)
            coordinator.async_set_updated_data({"camera/one": client.async_devices.return_value[0]})
            runtime = V380RuntimeData(client, coordinator, "fixture-account")
            entry.runtime_data = runtime
            hass.data[DOMAIN] = {entry.entry_id: runtime}
            source = V380MediaSource(hass)

            def item(identifier=""):
                return MediaSourceItem(hass, DOMAIN, identifier, None)

            root = await source.async_browse_media(item())
            account = await source.async_browse_media(item(root.children[0].identifier))
            camera = await source.async_browse_media(item(account.children[0].identifier))
            assert [child.title for child in camera.children] == ["2026-10-17", "2026-10-16"]
            day = await source.async_browse_media(item(camera.children[0].identifier))
            clip = day.children[0]
            assert clip.can_play and not clip.can_expand
            assert "fixture_signature" not in clip.media_content_id
            resolved = await source.async_resolve_media(item(clip.identifier))
            assert resolved.mime_type == "video/mp4"
            client.async_recording_source.assert_awaited_once_with("camera/one", "clip/one")
            assert resolved.url.endswith("fixture_signature=temporary")
            client.async_recordings.return_value = []
            with pytest.raises(Unresolvable) as missing:
                await source.async_resolve_media(item(clip.identifier))
            assert missing.value.translation_key == "recording_missing"
            with pytest.raises(BrowseError):
                await source.async_browse_media(item("account/unknown"))
            with pytest.raises(Unresolvable):
                await source.async_resolve_media(item("../../unknown"))
            client.async_recording_dates.side_effect = AuthError
            with _observe_reauth() as reauth:
                with pytest.raises(BrowseError) as expired:
                    await source.async_browse_media(item(account.children[0].identifier))
            assert expired.value.translation_key == "invalid_auth"
            assert reauth == [(entry, hass)]
    asyncio.run(run())


def test_camera_renews_signed_source_and_cancels_on_unload(tmp_path):
    async def run():
        async with _hass(tmp_path) as hass:
            entry = _entry(hass)
            client = _client()
            coordinator = V380CloudCoordinator(hass, entry, client)
            device = client.async_devices.return_value[0]
            coordinator.async_set_updated_data({device.device_id: device})
            entry.runtime_data = V380RuntimeData(client, coordinator, "fixture-account")
            camera = V380CloudCamera(entry, device)
            camera.hass = hass
            stream = SimpleNamespace(update_source=Mock(), stop=AsyncMock(), available=True)
            camera.stream = stream
            first_timer, renewed_timer = Mock(), Mock()
            client.async_live_source.side_effect = [
                "https://ha.example.invalid/live?authSig=first",
                "https://ha.example.invalid/live?authSig=renewed",
            ]
            with patch("custom_components.v380_cloud.camera.async_call_later", side_effect=[first_timer, renewed_timer]) as timer:
                await camera.stream_source()
                assert timer.call_args.args[1] == 8 * 60
                callback = timer.call_args.args[2]
                await callback(datetime.now(timezone.utc))
                stream.update_source.assert_called_once_with("https://ha.example.invalid/live?authSig=renewed")
                assert timer.call_count == 2
                await camera.async_will_remove_from_hass()
                renewed_timer.assert_called_once_with()
                stream.stop.assert_awaited_once()
                assert camera.stream is None
                await callback(datetime.now(timezone.utc))
                assert client.async_live_source.await_count == 2
    asyncio.run(run())
