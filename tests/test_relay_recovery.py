"""Relay/session recovery with synthetic binary media and bounded clocks."""
import asyncio
import importlib
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

PACKAGE = '_v380_route_tests'
pkg = types.ModuleType(PACKAGE)
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/v380_cloud')]
sys.modules[PACKAGE] = pkg
api = importlib.import_module(PACKAGE + '.api')
live = importlib.import_module(PACKAGE + '.live')


class Writer:
    def __init__(self): self.closed = False
    def close(self): self.closed = True
    async def wait_closed(self): pass


def packet(*, audio=False):
    body = struct.pack('<IHHQ', 1, 24 if audio else 0, 0, 0) + b'\x00\x00\x00\x01fixture'
    header = bytearray(12)
    header[0] = 0x7f
    struct.pack_into('<HHH', header, 3, 1, 0, len(body))
    return bytes(header) + body


class Transport(live.ForwardedCloudLiveClient):
    def __init__(self, state, *, hosts=None, bad=(), slow=(), audio=(), progress=None):
        super().__init__({'device_id': 7}, 'fixture', relay_state=state, progress=progress)
        self.hosts = hosts if hosts is not None else ['slow1', 'slow2', 'good']
        self.bad, self.slow, self.audio = set(bad), set(slow), set(audio)
        self.order, self.dispatches = [], 0
        self.reconnect_delays = ()

    async def async_relays(self):
        self.dispatches += 1
        return self.hosts

    async def _login(self, host):
        self.order.append(host)
        if host in self.slow: await asyncio.Event().wait()
        if host in self.bad:
            raise live.CloudError('fixture', stage='device_login_response', reason='network_timeout')
        return {'handle': 7, 'session': 9, 'version': 30}

    async def _start_relay(self, host, login):
        reader = asyncio.StreamReader()
        reader.feed_data(packet(audio=host in self.audio))
        self._writer = Writer()
        return reader, 0


class RelayRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def consume(self, client):
        frames = client.async_frames()
        try:
            return await asyncio.wait_for(anext(frames), 0.2)
        finally:
            await frames.aclose()
            await client.async_close()

    async def test_outer_deadline_advances_next_http_attempt_past_slow_relays(self):
        state = live.RelayState()
        for expected in ('slow1', 'slow2'):
            client = Transport(state, slow=('slow1', 'slow2'))
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(anext(client.async_frames()), 0.01)
            self.assertEqual(client.order, [expected])
            self.assertIn(expected, state.failed)
            await client.async_close()
        client = Transport(state, slow=('slow1', 'slow2'))
        self.assertTrue((await self.consume(client)).keyframe)
        self.assertEqual(client.order, ['good'])
        self.assertEqual(client.dispatches, 0)

    async def test_working_relay_is_reused_without_dispatcher_round_trip(self):
        state = live.RelayState()
        state.success('good', asyncio.get_running_loop().time())
        client = Transport(state)
        with patch.object(client, 'async_relays', side_effect=live.CloudError('dispatcher unavailable')) as dispatch:
            self.assertTrue((await self.consume(client)).keyframe)
        dispatch.assert_not_awaited()
        self.assertNotIn('good', state.failed)

    async def test_failed_preferred_relay_moves_to_an_alternative(self):
        state = live.RelayState()
        now = asyncio.get_running_loop().time()
        state.remember(['bad', 'good'], now); state.success('bad', now)
        client = Transport(state, hosts=['bad', 'good'], bad=('bad',))
        self.assertTrue((await self.consume(client)).keyframe)
        self.assertEqual(client.order, ['bad', 'good'])
        self.assertEqual(state.preferred, 'good')
        self.assertIn('bad', state.failed)

    async def test_dispatcher_failure_can_fall_back_to_recent_cached_alternatives(self):
        state = live.RelayState()
        state.remember(['bad', 'good'], asyncio.get_running_loop().time())
        progress = {}
        client = Transport(state, bad=('bad',), progress=progress)
        with patch.object(client, 'async_relays', side_effect=live.CloudError('dispatcher unavailable')):
            self.assertTrue((await self.consume(client)).keyframe)
        self.assertEqual(client.order, ['bad', 'good'])
        self.assertTrue(progress['cached_relay_used'])

    async def test_expired_cache_requires_fresh_discovery(self):
        state = live.RelayState()
        past = asyncio.get_running_loop().time() - 901
        state.remember(['old'], past); state.success('old', past)
        client = Transport(state, hosts=['fresh'])
        await self.consume(client)
        self.assertEqual(client.order, ['fresh'])
        self.assertEqual(client.dispatches, 1)

    async def test_audio_only_cancel_does_not_pin_a_previously_working_relay(self):
        state = live.RelayState()
        now = asyncio.get_running_loop().time()
        state.remember(['audio', 'good'], now); state.success('audio', now)
        client = Transport(state, audio=('audio',))
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(anext(client.async_frames()), 0.01)
        self.assertIn('audio', state.failed)
        await client.async_close()
        replacement = Transport(state)
        self.assertTrue((await self.consume(replacement)).keyframe)
        self.assertEqual(replacement.order, ['good'])

    async def test_actual_media_progress_counts_are_bounded_and_have_no_endpoints(self):
        progress = {}
        client = Transport(live.RelayState(), hosts=['private-relay-fixture'], progress=progress)
        await self.consume(client)
        self.assertEqual(progress['stage'], 'media_stream')
        self.assertEqual(progress['relays_tried'], 1)
        self.assertEqual(progress['keyframes'], 1)
        self.assertEqual(progress['video_frames'], 1)
        self.assertNotIn('private-relay-fixture', str(progress))


class FrameTransport:
    def __init__(self, metadata, password, **kwargs):
        self.state = kwargs['relay_state']
    async def async_frames(self):
        self.state.success('private-relay-fixture', asyncio.get_running_loop().time())
        yield live.Frame(b'\x00\x00\x00\x01fixture', 'h264', 0, True)
    async def async_close(self): pass


class ApiStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = api.V380CloudClient(None, 'fixture', 'fixture')
        self.client.account_id, self.client.access_token = '42', 'fixture'
        self.client.devices['7'] = api.CloudDevice('7', 'fixture', metadata={'device_id': 7})

    async def asyncTearDown(self):
        await self.client.async_close()

    def failed(self):
        return self.client.record_live_failure(api.CloudError(reason='network_timeout'), phase='startup', device_id='7')

    async def frame(self, progress=None):
        with patch.object(api, 'ForwardedCloudLiveClient', FrameTransport):
            frames = self.client.async_live_frames('7', progress=progress)
            try: return await anext(frames)
            finally: await frames.aclose()

    async def test_failed_startup_gets_one_saved_session_and_catalog_refresh(self):
        self.failed()
        progress = {}
        with patch.object(self.client, '_recover_login', AsyncMock()) as login, \
             patch.object(self.client, 'async_devices', AsyncMock(return_value=list(self.client.devices.values()))) as devices:
            self.assertTrue((await self.frame(progress)).keyframe)
        login.assert_awaited_once(); devices.assert_awaited_once()
        self.assertEqual(progress['metadata_refreshes'], 1)
        self.assertEqual(self.client.live_session_recovery_attempts, 1)

    async def test_repeated_failure_is_limited_by_five_minute_recovery_cooldown(self):
        with patch.object(self.client, '_recover_login', AsyncMock()) as login, \
             patch.object(self.client, 'async_devices', AsyncMock()):
            self.failed(); await self.frame()
            self.failed(); await self.frame()
        self.assertEqual(login.await_count, 1)

    async def test_cold_start_does_not_force_account_login(self):
        with patch.object(self.client, '_recover_login', AsyncMock()) as login:
            await self.frame()
        login.assert_not_awaited()

    async def test_recovery_can_run_again_after_cooldown_expires(self):
        with patch.object(self.client, '_recover_login', AsyncMock()) as login, \
             patch.object(self.client, 'async_devices', AsyncMock()):
            self.failed(); await self.frame()
            self.client._live_recovery_after = 0
            self.failed(); await self.frame()
        self.assertEqual(login.await_count, 2)

    async def test_concurrent_retry_requests_share_one_account_recovery(self):
        self.failed()
        async def recover(generation):
            await asyncio.sleep(0)
        with patch.object(self.client, '_recover_login', AsyncMock(side_effect=recover)) as login, \
             patch.object(self.client, 'async_devices', AsyncMock()):
            await asyncio.gather(self.client._recover_live_startup('7', {}),
                                 self.client._recover_live_startup('7', {}))
        login.assert_awaited_once()

    async def test_account_api_outage_keeps_trying_cached_device_credentials(self):
        self.failed(); progress = {}
        with patch.object(self.client, '_recover_login', AsyncMock(side_effect=api.CloudError('temporary'))):
            self.assertTrue((await self.frame(progress)).keyframe)
        self.assertTrue(progress['metadata_refresh_failed'])
        self.assertTrue(self.client.cached_devices_available)

    async def test_genuine_saved_password_rejection_propagates_without_relay_attempt(self):
        self.failed()
        with patch.object(self.client, '_recover_login', AsyncMock(side_effect=api.AuthError('rejected'))), \
             patch.object(api, 'ForwardedCloudLiveClient') as transport:
            with self.assertRaises(api.AuthError):
                await anext(self.client.async_live_frames('7'))
        transport.assert_not_called()
        self.assertFalse(self.client.cached_devices_available)

    async def test_relay_state_survives_http_sessions_and_clears_on_unload(self):
        await self.frame()
        state = self.client._relay_states['7']
        await self.frame()
        self.assertIs(self.client._relay_states['7'], state)
        self.assertEqual(state.preferred, 'private-relay-fixture')
        await self.client.async_close()
        self.assertFalse(self.client._relay_states)

    async def test_timeout_progress_reports_actual_stage_and_drops_private_fields(self):
        result = self.client.record_live_failure(api.CloudError(reason='network_timeout'), phase='startup',
            progress={'stage': 'device_login_response', 'relays_tried': 2, 'keyframes': 0,
                      'cached_relay_used': True, 'private-host': 'do-not-log', 'video_frames': 'secret'}, device_id='7')
        self.assertEqual(result['stage'], 'device_login_response')
        self.assertEqual(result['relays_tried'], 2)
        self.assertEqual(result['keyframes'], 0)
        self.assertNotIn('secret', str(result))
        self.assertNotIn('do-not-log', str(result))
