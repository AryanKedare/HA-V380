"""Real local HTTP, HA auth/worker/HLS and PyAV; synthetic camera frames only."""
import asyncio
from contextlib import asynccontextmanager
from copy import copy
from datetime import datetime, timezone
from fractions import Fraction
import io
import logging
import random
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import av
import pytest

pytest.importorskip('homeassistant')
from homeassistant import auth, loader
from homeassistant.components.camera.const import DATA_CAMERA_PREFS
from homeassistant.components.camera.helper import async_stream_endpoint_url
from homeassistant.components.camera.prefs import CameraPreferences
from homeassistant.components.http.auth import async_setup_auth
from homeassistant.components import stream as ha_stream
from homeassistant.components.stream.const import ATTR_ENDPOINTS, ATTR_SETTINGS, ATTR_STREAMS
from homeassistant.components.stream.core import STREAM_SETTINGS_NON_LL_HLS
from homeassistant.components.stream.exceptions import StreamWorkerError
from homeassistant.components.stream import worker as ha_worker
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry, frame
from homeassistant.helpers.http import KEY_HASS, request_handler_factory
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from custom_components.v380_cloud.api import CloudDevice, CloudError, V380CloudClient
from custom_components.v380_cloud.camera import V380CloudCamera
from custom_components.v380_cloud.live import Frame
from custom_components.v380_cloud import proxy


def video_frames():
    encoder = av.CodecContext.create('libx264', 'w')
    encoder.width = encoder.height = 64
    encoder.pix_fmt = 'yuv420p'
    encoder.time_base = Fraction(1, 20)
    encoder.framerate = Fraction(20, 1)
    encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency', 'g': '10', 'keyint_min': '10', 'sc_threshold': '0'}
    encoder.open()
    result = []
    for index in range(40):
        frame = av.VideoFrame(64, 64, 'yuv420p')
        frame.pts, frame.time_base = index, Fraction(1, 20)
        randomizer = random.Random(index)
        for plane in frame.planes:
            plane.update(randomizer.randbytes(plane.buffer_size))
        for packet in encoder.encode(frame):
            result.append(Frame(bytes(packet), 'h264', index * 50, packet.is_keyframe, index))
    return result


async def until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


@asynccontextmanager
async def environment(tmp_path, frame_source):
    hass = HomeAssistant(str(tmp_path))
    frame.async_setup(hass)
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    hass.auth = await auth.auth_manager_from_config(hass, [], [])
    prefs = CameraPreferences(hass)
    await prefs.async_load()
    hass.data[DATA_CAMERA_PREFS] = prefs
    settings = copy(STREAM_SETTINGS_NON_LL_HLS)
    settings.min_segment_duration = 0.45
    hass.data['stream'] = {ATTR_SETTINGS: settings, ATTR_STREAMS: [],
                          ATTR_ENDPOINTS: {'hls': '/api/hls/{}/playlist.m3u8'}}
    hass.config.components.add('stream')
    app = web.Application()
    app[KEY_HASS] = hass
    await async_setup_auth(hass, app)
    client = V380CloudClient(None, 'fixture-account', 'fixture-password')
    client.account_id, client.access_token = '42', 'fixture-token'
    device = CloudDevice('7', 'Fixture camera', metadata={'device_id': 7})
    client.devices = {'7': device}
    client.device_list_complete = True
    coordinator = DataUpdateCoordinator(hass, logging.getLogger(__name__), name='fixture', config_entry=None)
    coordinator.async_set_updated_data(dict(client.devices))
    entry = SimpleNamespace(runtime_data=SimpleNamespace(client=client, coordinator=coordinator, account_id='42'))
    hass.data['v380_cloud'] = {'fixture': entry.runtime_data}
    client.bind_home_assistant(hass, 'fixture')
    camera = V380CloudCamera(entry, device)
    camera.hass, camera.entity_id = hass, 'camera.fixture'
    camera.async_write_ha_state = Mock()
    view = proxy.V380LiveView()
    app.router.add_get(view.url, request_handler_factory(hass, view, view.get))
    server = TestServer(app)
    await server.start_server()
    hass.config.internal_url = str(server.make_url('')).rstrip('/')
    with patch.object(client, 'async_live_frames', side_effect=frame_source):
        try:
            yield SimpleNamespace(hass=hass, client=client, camera=camera,
                                  coordinator=coordinator, url=await client.async_live_source('7'))
        finally:
            await camera.async_will_remove_from_hass()
            await client.async_close()
            await server.close()
            await hass.async_stop(force=True)


def test_real_http_worker_produces_decodable_hls_and_renews_without_reconnect(tmp_path):
    async def run():
        frames = video_frames()
        opened, closed = [], []
        async def source(device_id, **kwargs):
            opened.append(True)
            try:
                index = 0
                while True:
                    yield frames[index % len(frames)]
                    index += 1
                    await asyncio.sleep(0.05)
            finally:
                closed.append(True)
        async with environment(tmp_path, source) as env:
            endpoint = await async_stream_endpoint_url(env.hass, env.camera, 'hls')
            stream = env.camera.stream
            output = stream.outputs()['hls']
            await until(lambda: len(output.sequences) >= 3, timeout=12)
            segment = output.get_segment(output.sequences[-2])
            media = segment.init + b''.join(part.data for part in segment.parts)
            with av.open(io.BytesIO(media), format='mp4') as container:
                assert sum(1 for _ in container.decode(video=0)) > 0
            worker = stream._thread
            original_source = stream.source
            env.coordinator.async_set_update_error(UpdateFailed('synthetic metadata outage'))
            assert env.camera.available
            await asyncio.sleep(1.05)  # Ensure a new signature issuance timestamp.
            await env.camera._async_refresh_source(datetime.now(timezone.utc))
            assert stream.source != original_source
            assert stream._thread is worker and worker.is_alive()
            assert not stream._thread_quit.is_set()
            assert stream.endpoint_url('hls') == endpoint
            sequence = output.sequences[-1]
            await until(lambda: output.sequences[-1] > sequence)
            assert len(opened) == 1 and not closed
        assert len(closed) == 1
    asyncio.run(run())


def test_renewal_during_core_retry_keeps_hls_output(tmp_path):
    async def source(device_id, **kwargs):
        if False: yield None
    async def run():
        attempts = []
        def worker(source_url, options, settings, state, converter, quit_event):
            attempts.append(source_url)
            if len(attempts) == 1:
                raise StreamWorkerError('synthetic retry')
            quit_event.wait(5)
        async with environment(tmp_path, source) as env:
            with patch.object(ha_worker, 'stream_worker', side_effect=worker), \
                 patch.object(ha_stream, 'STREAM_RESTART_INCREMENT', 0.2):
                endpoint = await async_stream_endpoint_url(env.hass, env.camera, 'hls')
                stream = env.camera.stream
                await until(lambda: not stream.available)
                await env.camera._async_refresh_source(datetime.now(timezone.utc))
                await until(lambda: len(attempts) == 2)
                assert stream.endpoint_url('hls') == endpoint
                assert 'hls' in stream.outputs()
                await stream.stop()
    asyncio.run(run())


def test_idle_core_stream_is_replaced_before_new_endpoint(tmp_path):
    frames = video_frames()
    async def source(device_id, **kwargs):
        while True:
            yield frames[0]
            await asyncio.sleep(0.05)
    async def run():
        async with environment(tmp_path, source) as env:
            old = await env.camera.async_create_stream()
            assert not old.outputs()
            endpoint = await async_stream_endpoint_url(env.hass, env.camera, 'hls')
            assert env.camera.stream is not old
            assert env.camera.stream.endpoint_url('hls') == endpoint
            assert 'hls' in env.camera.stream.outputs()
    asyncio.run(run())


def test_slow_start_returns_gateway_error_before_reader_timeout(tmp_path):
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source(device_id, **kwargs):
            kwargs['progress'].update(stage='relay_handshake', relays_tried=2, keyframes=0)
            entered.set()
            try:
                await asyncio.Event().wait()
                if False: yield None
            finally:
                closed.set()
        async with environment(tmp_path, source) as env:
            with patch.object(proxy, 'LIVE_STARTUP_TIMEOUT', 0.1):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
                    async with session.get(env.url) as response:
                        assert response.status == 502
            assert entered.is_set() and closed.is_set()
            assert not env.client._media_tasks
            assert env.client.last_live_failure['phase'] == 'startup'
            assert env.client.last_live_failure['detail'] == 'network_timeout'
            assert env.client.last_live_failure['stage'] == 'relay_handshake'
            assert env.client.last_live_failure['relays_tried'] == 2
            assert env.client._live_startup_failures['7'] == 1
    asyncio.run(run())


def test_client_disconnect_cancels_session_before_first_frame(tmp_path):
    async def run():
        entered, closed = asyncio.Event(), asyncio.Event()
        async def source(device_id, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
                if False: yield None
            finally:
                closed.set()
        async with environment(tmp_path, source) as env:
            with patch.object(proxy, 'DISCONNECT_CHECK_INTERVAL', 0.01):
                async with aiohttp.ClientSession() as session:
                    request = asyncio.create_task(session.get(env.url))
                    await asyncio.wait_for(entered.wait(), 2)
                    request.cancel()
                    with pytest.raises(asyncio.CancelledError): await request
                    await asyncio.wait_for(closed.wait(), 1)
                    await until(lambda: not env.client._media_tasks)
            assert env.client.live_stream_failures == 0
    asyncio.run(run())


def test_relay_failure_after_headers_records_only_sanitized_reason(tmp_path, caplog):
    frames = video_frames()
    async def source(device_id, **kwargs):
        yield frames[0]
        raise CloudError('private-url-and-password', stage='media_stream', reason='connection_failed', code=100)
    async def run():
        async with environment(tmp_path, source) as env:
            async with aiohttp.ClientSession() as session:
                async with session.get(env.url) as response:
                    assert response.status == 200
                    assert await response.read()
            assert env.client.last_live_failure == {'stage': 'media_stream', 'detail': 'connection_failed', 'code': 100, 'phase': 'stream'}
            assert env.client.live_stream_failures == 1
    asyncio.run(run())
    assert 'private-url-and-password' not in caplog.text


def test_viewer_close_during_video_is_not_recorded_as_relay_failure(tmp_path, caplog):
    frames = video_frames()
    async def run():
        closed = asyncio.Event()
        async def source(device_id, **kwargs):
            try:
                while True:
                    yield frames[0]
                    await asyncio.sleep(0.01)
            finally:
                closed.set()
        async with environment(tmp_path, source) as env:
            async with aiohttp.ClientSession() as session:
                response = await session.get(env.url)
                assert await response.content.read(188)
                response.close()
                await asyncio.wait_for(closed.wait(), 1)
                await until(lambda: not env.client._media_tasks)
            assert env.client.live_stream_failures == 0
    asyncio.run(run())
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
