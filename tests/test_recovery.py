"""Authentication and live recovery with synthetic services; no vendor traffic."""
import asyncio
import base64
from contextlib import asynccontextmanager
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

COMPONENT = Path(__file__).resolve().parents[1] / 'custom_components/v380_cloud'
PACKAGE = '_v380_recovery_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package
account = importlib.import_module(PACKAGE + '.account')
live = importlib.import_module(PACKAGE + '.live')
api = importlib.import_module(PACKAGE + '.api')


def login(token='new-token', uid=42):
    return {'result': 0, 'error_code': 0, 'data': {'access_token': token, 'user_id': uid}}


def devices(**extra):
    return {'result': 0, 'error_code': 0, 'data': [{'device_id': 7, 'nickname': 'fixture'}], **extra}


EXPIRED = {'result': -1, 'error_code': 21011}


class AccountRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = account.AccountClient(None, 'fixture-account', 'fixture-password')
        self.client.account_id, self.client.user_id, self.client.access_token = '42', 42, 'old-token'
        self.calls = []

    async def asyncTearDown(self):
        await self.client.async_close()

    async def test_expired_token_signs_in_with_saved_credentials_and_retries_once(self):
        async def request(url, payload):
            self.calls.append((url, payload))
            if url.endswith('user/login'):
                return login()
            return EXPIRED if payload['accesstoken'] == 'old-token' else devices()
        with patch.object(self.client, '_request_json', side_effect=request):
            result = await self.client.async_devices()
        self.assertEqual([item.device_id for item in result], ['7'])
        self.assertEqual([url.rsplit('/', 1)[-1] for url, _ in self.calls], ['list', 'login', 'list'])
        self.assertEqual(self.calls[2][1]['accesstoken'], 'new-token')
        self.assertNotIn('fixture-password', str(self.calls))

    async def test_http_401_also_recovers_automatically(self):
        class Reply:
            def __init__(self, body, status): self.body, self.status, self.content = body, status, self
            async def iter_chunked(self, size): yield json.dumps(self.body).encode()
        outer = self
        class Session:
            def request(self, method, url, *, data, **kwargs):
                payload = json.loads(data)
                outer.calls.append((url, payload))
                @asynccontextmanager
                async def response():
                    if url.endswith('user/login'):
                        yield Reply(login(), 200)
                    elif payload['accesstoken'] == 'old-token':
                        yield Reply({}, 401)
                    else:
                        yield Reply(devices(), 200)
                return response()
        self.client.session = Session()
        self.assertEqual(len(await self.client.async_devices()), 1)
        self.assertEqual(len(self.calls), 3)

    async def test_concurrent_expiry_shares_one_login_even_when_token_text_is_reused(self):
        async def request(url, payload):
            self.calls.append((url, payload))
            await asyncio.sleep(0)
            if url.endswith('user/login'):
                return login('old-token')
            return EXPIRED if self.client._auth_generation == 0 else devices()
        with patch.object(self.client, '_request_json', side_effect=request):
            results = await asyncio.gather(*(self.client.async_devices() for _ in range(10)))
        self.assertTrue(all(len(result) == 1 for result in results))
        self.assertEqual(sum(url.endswith('user/login') for url, _ in self.calls), 1)

    async def test_initial_concurrent_requests_also_share_one_login(self):
        self.client.access_token = ''
        async def request(url, payload):
            self.calls.append((url, payload))
            await asyncio.sleep(0)
            return login() if url.endswith('user/login') else devices()
        with patch.object(self.client, '_request_json', side_effect=request):
            await asyncio.gather(*(self.client.async_devices() for _ in range(8)))
        self.assertEqual(sum(url.endswith('user/login') for url, _ in self.calls), 1)

    async def test_invalid_saved_credentials_do_not_create_a_login_storm(self):
        async def request(url, payload):
            self.calls.append((url, payload))
            await asyncio.sleep(0)
            return {'result': -1, 'error_code': 21006} if url.endswith('user/login') else EXPIRED
        with patch.object(self.client, '_request_json', side_effect=request):
            results = await asyncio.gather(*(self.client.async_devices() for _ in range(8)), return_exceptions=True)
        self.assertTrue(all(isinstance(result, account.AuthError) for result in results))
        self.assertEqual(sum(url.endswith('user/login') for url, _ in self.calls), 1)
        self.assertEqual(self.client.access_token, '')

    async def test_identity_change_is_rejected(self):
        with patch.object(self.client, '_request_json', side_effect=[EXPIRED, login(uid=99)]):
            with self.assertRaises(account.AuthError):
                await self.client.async_devices()
        self.assertEqual(self.client.account_id, '42')
        self.assertEqual(self.client.access_token, '')

    async def test_network_timeout_does_not_trigger_account_login(self):
        with patch.object(self.client, '_request_json', side_effect=account.CloudError('Cannot communicate')) as request:
            with self.assertRaises(account.CloudError):
                await self.client.async_devices()
        self.assertEqual(request.await_count, 1)
        self.assertTrue(request.call_args.args[0].endswith('device/list'))

    async def test_rejected_renewal_falls_back_to_saved_credentials(self):
        with patch.object(self.client, '_request_json', side_effect=[devices(renew_token=True), EXPIRED, login()]) as request:
            self.assertEqual(len(await self.client.async_devices()), 1)
        self.assertEqual(self.client.access_token, 'new-token')
        self.assertEqual(request.await_count, 3)

    async def test_concurrent_renewal_hints_share_one_renewal(self):
        async def request(url, payload):
            self.calls.append((url, payload))
            await asyncio.sleep(0)
            if url.endswith('user/get-new-token'):
                return {'result': 0, 'error_code': 0, 'access_token': 'renewed-token'}
            return devices(renew_token=True)
        with patch.object(self.client, '_request_json', side_effect=request):
            await asyncio.gather(*(self.client.async_devices() for _ in range(8)))
        self.assertEqual(sum(url.endswith('user/get-new-token') for url, _ in self.calls), 1)

    async def test_closing_during_login_cannot_restore_tokens_after_unload(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def request(*args):
            entered.set()
            await release.wait()
            return login()
        with patch.object(self.client, '_request_json', side_effect=request):
            task = asyncio.create_task(self.client.async_login())
            await entered.wait()
            await self.client.async_close()
            release.set()
            with self.assertRaises(account.CloudError): await task
        self.assertEqual(self.client.access_token, '')
        self.assertEqual(self.client._password, '')


def frame(sequence, *, keyframe=False, codec='h264'):
    return live.Frame(b'\x00\x00\x00\x01fixture', codec, sequence * 40, keyframe, sequence)


def timeout_error():
    return live.CloudError('Temporary video timeout', stage='media_stream', reason='network_timeout')


class LiveRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def client(self):
        client = live.ForwardedCloudLiveClient({'device_id': 7}, 'fixture')
        client.reconnect_delays = (0, 0, 0)
        return client

    async def test_reconnect_skips_predictive_frames_and_closes_both_sessions(self):
        client, started, closed = self.client(), [], []
        async def session():
            number = len(started)
            started.append(number)
            try:
                if number == 0:
                    yield frame(1, keyframe=True)
                    yield frame(2)
                    raise timeout_error()
                yield frame(3)  # Cannot resume video from this P frame.
                yield frame(4, keyframe=True)
                yield frame(5)
            finally:
                closed.append(number)
        with patch.object(client, '_async_session_frames', side_effect=session):
            frames = client.async_frames()
            received = [await anext(frames) for _ in range(4)]
            await frames.aclose()
        self.assertEqual([item.sequence for item in received], [1, 2, 4, 5])
        self.assertEqual(client.reconnect_count, 1)
        self.assertEqual(closed, [0, 1])

    async def test_consecutive_network_failures_have_a_finite_retry_budget(self):
        client, started = self.client(), []
        async def session():
            started.append(True)
            if False: yield frame(0)
            raise timeout_error()
        with patch.object(client, '_async_session_frames', side_effect=session):
            with self.assertRaises(live.CloudError) as caught:
                await anext(client.async_frames())
        self.assertEqual(len(started), 4)
        self.assertEqual(client.reconnect_count, 3)
        self.assertEqual(caught.exception.diagnostic['detail'], 'network_timeout')

    async def test_codec_change_after_reconnection_stops_the_existing_stream(self):
        client, started = self.client(), []
        async def session():
            number = len(started)
            started.append(number)
            yield frame(number, keyframe=True, codec='h264' if number == 0 else 'hevc')
            raise timeout_error()
        with patch.object(client, '_async_session_frames', side_effect=session):
            frames = client.async_frames()
            self.assertEqual((await anext(frames)).codec, 'h264')
            with self.assertRaises(live.UnsupportedError) as caught:
                await anext(frames)
        self.assertEqual(caught.exception.diagnostic['detail'], 'codec_changed')
        self.assertEqual(len(started), 2)

    async def test_credentials_and_protocol_errors_are_not_network_retries(self):
        for error in (live.AuthError('fixture', stage='device_login_response', reason='login_rejected', code=1012),
                      live.UnsupportedError('fixture'),
                      live.CloudError('fixture', stage='media_stream', reason='video_key_invalid')):
            client = self.client()
            async def session():
                if False: yield frame(0)
                raise error
            with patch.object(client, '_async_session_frames', side_effect=session) as attempts:
                with self.assertRaises(type(error)): await anext(client.async_frames())
            self.assertEqual(attempts.call_count, 1)
            self.assertEqual(client.reconnect_count, 0)

    async def test_close_interrupts_backoff_without_another_login(self):
        client, entered = self.client(), asyncio.Event()
        client.reconnect_delays = (60,)
        async def session():
            entered.set()
            if False: yield frame(0)
            raise timeout_error()
        with patch.object(client, '_async_session_frames', side_effect=session) as attempts:
            frames = client.async_frames()
            task = asyncio.create_task(anext(frames))
            await entered.wait()
            await asyncio.sleep(0)
            await client.async_close()
            with self.assertRaises(StopAsyncIteration):
                await asyncio.wait_for(task, 1)
        self.assertEqual(attempts.call_count, 1)

    async def test_healthy_video_resets_the_consecutive_failure_budget(self):
        client, started = self.client(), []
        client.reconnect_delays, client.retry_reset_after = (0,), 0
        async def session():
            number = len(started)
            started.append(number)
            yield frame(number * 2, keyframe=True)
            yield frame(number * 2 + 1)
            if number < 3: raise timeout_error()
        with patch.object(client, '_async_session_frames', side_effect=session):
            received = [item async for item in client.async_frames()]
        self.assertEqual(len(received), 8)
        self.assertEqual(client.reconnect_count, 3)

    async def test_audio_packets_cannot_hide_a_video_stall(self):
        client = self.client()
        client.reconnect_delays, client.video_timeout = (), 0.01
        async def session():
            yield frame(1, keyframe=True)
            while True:
                await asyncio.sleep(0)
                yield frame(2, codec='aac')
        with patch.object(client, '_async_session_frames', side_effect=session):
            with self.assertRaises(live.CloudError) as caught:
                async for _ in client.async_frames(): pass
        self.assertEqual(caught.exception.diagnostic['detail'], 'network_timeout')


class ApiCredentialRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, rejected_twice=False, account_rejected=False):
        client = api.V380CloudClient(None, 'fixture-account', 'fixture-account-password')
        def device(password):
            return api.CloudDevice('7', 'fixture', metadata={'device_id': 7,
                'device_password': base64.b64encode(password.encode()).decode()})
        client.devices['7'] = device('old-device-password')
        constructed, closed = [], []
        class Transport:
            def __init__(self, metadata, password, **kwargs):
                self.number = len(constructed)
                constructed.append(password)
            async def async_frames(self):
                if self.number == 0 or rejected_twice:
                    raise api.AuthError('Device credentials rejected', stage='device_login_response',
                                        reason='login_rejected', code=1012)
                yield frame(1, keyframe=True)
            async def async_close(self): closed.append(self.number)
        async def refresh():
            if account_rejected: raise api.AuthError('Account credentials rejected')
            client.devices['7'] = device('new-device-password')
            return list(client.devices.values())
        with patch.object(api, 'ForwardedCloudLiveClient', Transport), \
             patch.object(client, 'async_devices', side_effect=refresh) as refreshed:
            frames = client.async_live_frames('7')
            try:
                if rejected_twice:
                    with self.assertRaises(api.CloudError) as caught: await anext(frames)
                    self.assertNotIsInstance(caught.exception, api.AuthError)
                    self.assertEqual(caught.exception.diagnostic['code'], 1012)
                elif account_rejected:
                    with self.assertRaises(api.AuthError): await anext(frames)
                else:
                    self.assertTrue((await anext(frames)).keyframe)
            finally:
                await frames.aclose()
            self.assertEqual(refreshed.await_count, 1)
        self.assertFalse(client._live_sessions)
        self.assertEqual(closed, list(range(len(constructed))))
        self.assertEqual(constructed, ['old-device-password'] + ([] if account_rejected else ['new-device-password']))

    async def test_device_rejection_refreshes_saved_metadata_once(self):
        await self.exercise()

    async def test_persistent_device_rejection_does_not_prompt_for_cloud_password(self):
        await self.exercise(rejected_twice=True)

    async def test_rejected_cloud_credentials_still_request_real_reauthentication(self):
        await self.exercise(account_rejected=True)
