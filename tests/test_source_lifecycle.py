"""Offline source lifecycle contracts; HA interfaces are small synthetic doubles.

The fake base camera mirrors Core 2026.10.2 async_create_stream: stream_source
is called only when no Stream exists. No vendor service or socket is used.
The full HA middleware and lifecycle tests remain in test_proxy.py/test_ha.py.
"""
import asyncio
from datetime import datetime, timezone
from enum import IntFlag
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web

COMPONENT = Path(__file__).resolve().parents[1] / 'custom_components/v380_cloud'
PACKAGE = '_v380_source_contract'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
package.V380CloudCoordinator = object
package.V380ConfigEntry = object
sys.modules[PACKAGE] = package
api = importlib.import_module(PACKAGE + '.api')


class BaseCamera:
    def __init__(self):
        self.stream = None

    async def async_create_stream(self):
        if self.stream is None:
            self.stream = make_stream(await self.stream_source())
        return self.stream


class CoordinatorEntity:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, coordinator, **kwargs):
        self.coordinator = coordinator

    async def async_will_remove_from_hass(self):
        pass


class HomeAssistantError(Exception):
    def __init__(self, *, translation_domain, translation_key):
        self.translation_key = translation_key


class Features(IntFlag):
    STREAM = 1


def module(name, **attrs):
    result = types.ModuleType(name)
    result.__dict__.update(attrs)
    return result


stubs = {
    'homeassistant': module('homeassistant'),
    'homeassistant.components': module('homeassistant.components'),
    'homeassistant.components.camera': module('homeassistant.components.camera', Camera=BaseCamera, CameraEntityFeature=Features),
    'homeassistant.components.http': module('homeassistant.components.http', HomeAssistantView=object),
    'homeassistant.components.http.auth': module('homeassistant.components.http.auth', async_sign_path=Mock()),
    'homeassistant.core': module('homeassistant.core', CALLBACK_TYPE=object, HomeAssistant=object, callback=lambda f: f),
    'homeassistant.exceptions': module('homeassistant.exceptions', HomeAssistantError=HomeAssistantError),
    'homeassistant.helpers': module('homeassistant.helpers'),
    'homeassistant.helpers.device_registry': module('homeassistant.helpers.device_registry', DeviceInfo=dict),
    'homeassistant.helpers.entity_platform': module('homeassistant.helpers.entity_platform', AddConfigEntryEntitiesCallback=object),
    'homeassistant.helpers.event': module('homeassistant.helpers.event', async_call_later=Mock()),
    'homeassistant.helpers.update_coordinator': module('homeassistant.helpers.update_coordinator', CoordinatorEntity=CoordinatorEntity),
    'homeassistant.helpers.network': module('homeassistant.helpers.network', get_url=Mock()),
    'homeassistant.helpers.http': module('homeassistant.helpers.http', KEY_HASS='hass'),
}
with patch.dict(sys.modules, stubs):
    camera = importlib.import_module(PACKAGE + '.camera')
    proxy = importlib.import_module(PACKAGE + '.proxy')


def make_stream(source):
    stream = types.SimpleNamespace(source=source, stop=AsyncMock())
    stream.update_source = Mock(side_effect=lambda value: setattr(stream, 'source', value))
    return stream


class SourceLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = types.SimpleNamespace(async_live_source=AsyncMock(side_effect=[
            'https://ha.invalid/live?authSig=first',
            'https://ha.invalid/live?authSig=renewed',
            'https://ha.invalid/live?authSig=third',
        ]))
        self.entry = types.SimpleNamespace(runtime_data=types.SimpleNamespace(
            client=self.client, coordinator=object(), account_id='fixture'), async_start_reauth=Mock())
        self.camera = camera.V380CloudCamera(self.entry, api.CloudDevice('7', 'fixture'))
        self.camera.hass = object()
        self.timer = Mock(side_effect=lambda *args: Mock())
        self.timer_patch = patch.object(camera, 'async_call_later', self.timer)
        self.timer_patch.start()
        self.addCleanup(self.timer_patch.stop)
        self.now = datetime.now(timezone.utc)

    async def test_repeated_source_requests_cannot_postpone_active_url_renewal(self):
        stream = await self.camera.async_create_stream()
        deadline = getattr(self.camera, '_source_refresh_at', None)
        original_timer = self.camera._refresh_source_unsub
        for _ in range(20):
            self.assertEqual(await self.camera.stream_source(), stream.source)
        self.assertEqual(self.camera._source_refresh_at, deadline)
        self.assertIs(self.camera._refresh_source_unsub, original_timer)
        self.assertEqual(self.timer.call_count, 1)
        original_timer.assert_not_called()
        self.client.async_live_source.assert_awaited_once_with('7')

    async def test_timer_replaces_cached_source_then_schedules_next_renewal(self):
        stream = await self.camera.async_create_stream()
        await self.camera._async_refresh_source(self.now)
        self.assertTrue(stream.source.endswith('authSig=renewed'))
        stream.update_source.assert_called_once_with(stream.source)
        self.assertEqual(self.timer.call_count, 2)
        self.assertEqual(self.timer.call_args.args[1], 480)
        self.assertEqual(await self.camera.stream_source(), stream.source)
        self.assertEqual(self.client.async_live_source.await_count, 2)

    async def test_cached_idle_stream_is_refreshed_before_reuse(self):
        stream = await self.camera.async_create_stream()
        self.camera._source_refresh_at = 0  # Renewal deadline passed while idle.
        self.assertIs(await self.camera.async_create_stream(), stream)
        self.assertTrue(stream.source.endswith('authSig=renewed'))
        stream.update_source.assert_called_once_with(stream.source)
        self.assertEqual(self.client.async_live_source.await_count, 2)

    async def test_concurrent_reuse_shares_one_new_source(self):
        stream = await self.camera.async_create_stream()
        self.camera._source_refresh_at = 0
        result = await asyncio.gather(*(self.camera.async_create_stream() for _ in range(12)))
        self.assertTrue(all(item is stream for item in result))
        self.assertEqual(self.client.async_live_source.await_count, 2)
        stream.update_source.assert_called_once()

    async def test_retry_after_cloud_failure_preserves_source_until_replaced(self):
        stream = await self.camera.async_create_stream()
        old_source = stream.source
        self.client.async_live_source.side_effect = api.CloudError('temporary')
        await self.camera._async_refresh_source(self.now)
        self.assertEqual(stream.source, old_source)
        stream.update_source.assert_not_called()
        self.assertEqual(self.timer.call_args.args[1], 60)
        self.client.async_live_source.side_effect = None
        self.client.async_live_source.return_value = 'https://ha.invalid/live?authSig=recovered'
        await self.camera._async_refresh_source(self.now)
        self.assertTrue(stream.source.endswith('authSig=recovered'))
        self.assertEqual(self.timer.call_args.args[1], 480)

    async def test_unload_cancels_timer_and_cannot_resurrect_stream(self):
        stream = await self.camera.async_create_stream()
        timer = self.camera._refresh_source_unsub
        await self.camera.async_will_remove_from_hass()
        timer.assert_called_once_with()
        stream.stop.assert_awaited_once()
        self.assertIsNone(self.camera._source)
        self.assertIsNone(await self.camera.async_create_stream())
        self.assertIsNone(await self.camera.stream_source())
        await self.camera._async_refresh_source(self.now)
        self.assertEqual(self.client.async_live_source.await_count, 1)

    async def test_unload_during_renewal_cannot_apply_a_new_url(self):
        stream = await self.camera.async_create_stream()
        entered, release = asyncio.Event(), asyncio.Event()
        async def source(*args):
            entered.set()
            await release.wait()
            return 'https://ha.invalid/live?authSig=too-late'
        self.client.async_live_source.side_effect = source
        task = asyncio.create_task(self.camera._async_refresh_source(self.now))
        await entered.wait()
        await self.camera.async_will_remove_from_hass()
        release.set()
        await task
        stream.update_source.assert_not_called()
        self.assertIsNone(self.camera._refresh_source_unsub)

    async def test_real_account_rejection_still_starts_reauthentication(self):
        await self.camera.async_create_stream()
        self.client.async_live_source.side_effect = api.AuthError('rejected')
        self.camera._source_refresh_at = 0
        with self.assertRaises(HomeAssistantError) as caught:
            await self.camera.async_create_stream()
        self.assertEqual(caught.exception.translation_key, 'invalid_auth')
        self.entry.async_start_reauth.assert_called_once_with(self.camera.hass)

    async def test_timer_without_a_stream_does_not_open_vendor_video(self):
        await self.camera.stream_source()
        await self.camera._async_refresh_source(self.now)
        self.assertIsNone(self.camera.stream)
        self.assertEqual(self.client.async_live_source.await_count, 1)


class RouteLifecycleTests(unittest.TestCase):
    def hass(self, *, entry=None, runtime=None):
        return types.SimpleNamespace(
            data={'v380_cloud': {'entry': runtime} if runtime else {}},
            config_entries=types.SimpleNamespace(async_get_entry=Mock(return_value=entry)),
        )

    def test_known_entry_during_reload_is_retryable(self):
        hass = self.hass(entry=types.SimpleNamespace(domain='v380_cloud'))
        with self.assertRaises(web.HTTPServiceUnavailable) as caught:
            proxy._client(hass, 'entry', '7')
        self.assertEqual(caught.exception.headers['Retry-After'], '5')

    def test_unknown_or_other_domain_entry_remains_not_found(self):
        for entry in (None, types.SimpleNamespace(domain='other')):
            with self.assertRaises(web.HTTPNotFound):
                proxy._client(self.hass(entry=entry), 'entry', '7')

    def test_failed_empty_snapshot_is_retryable_but_unknown_camera_is_not(self):
        runtime = types.SimpleNamespace(client=object(), coordinator=types.SimpleNamespace(
            data=None, last_update_success=False))
        hass = self.hass(runtime=runtime)
        with self.assertRaises(web.HTTPServiceUnavailable):
            proxy._client(hass, 'entry', '7')
        runtime.coordinator.data = {'8': object()}
        runtime.coordinator.last_update_success = True
        with self.assertRaises(web.HTTPNotFound):
            proxy._client(hass, 'entry', '7')

    def test_valid_camera_survives_a_failed_poll_with_cached_metadata(self):
        runtime = types.SimpleNamespace(client=object(), coordinator=types.SimpleNamespace(
            data={'7': object()}, last_update_success=False))
        self.assertIs(proxy._client(self.hass(runtime=runtime), 'entry', '7'), runtime.client)

    def test_same_route_uses_new_runtime_after_reload(self):
        hass = self.hass(entry=types.SimpleNamespace(domain='v380_cloud'))
        with self.assertRaises(web.HTTPServiceUnavailable):
            proxy._client(hass, 'entry', '7')
        runtime = types.SimpleNamespace(client=object(), coordinator=types.SimpleNamespace(
            data={'7': object()}, last_update_success=True))
        hass.data['v380_cloud']['entry'] = runtime
        self.assertIs(proxy._client(hass, 'entry', '7'), runtime.client)
