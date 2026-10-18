"""Standalone diagnostics: media decode, bounded requests, statuses and privacy."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'tools/full_test_core.py'
spec = importlib.util.spec_from_file_location('v380_probe_core_test', SOURCE)
probe = importlib.util.module_from_spec(spec); spec.loader.exec_module(probe)


def sample_ts():
    p = probe.load_protocol()
    from fractions import Fraction
    encoder = p.av.CodecContext.create('libx264', 'w')
    encoder.width = encoder.height = 64; encoder.pix_fmt = 'yuv420p'
    encoder.time_base = Fraction(1, 25); encoder.framerate = Fraction(25, 1)
    encoder.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}; encoder.open()
    mux = p.mpegts.MpegTsMuxer('h264'); output = bytearray()
    for i in range(12):
        frame = p.av.VideoFrame(64, 64, 'yuv420p'); frame.pts = i; frame.time_base = Fraction(1, 25)
        for plane in frame.planes: plane.update(bytes([i * 15]) * plane.buffer_size)
        for packet in encoder.encode(frame): output.extend(mux.push(bytes(packet), i / 25, keyframe=packet.is_keyframe))
    for packet in encoder.encode(None): output.extend(mux.push(bytes(packet)))
    return bytes(output)


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self.body, self.status = body, status
        self.headers = headers or {}
        self.content_length = len(body)
        self.content = self

    async def read(self, limit): return self.body[:limit]

    async def iter_chunked(self, size):
        for pos in range(0, len(self.body), size): yield self.body[pos:pos+size]


class FakeSession:
    def __init__(self, responses): self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url not in self.responses: raise AssertionError('Unexpected request')
        @asynccontextmanager
        async def response(): yield self.responses[url]
        return response()


def test_actual_decode_and_invalid_media_rejection():
    result = probe.decode_video(sample_ts(), format='mpegts')
    assert result['decoded_frames'] == 12 and result['codec'] == 'h264'
    with pytest.raises(probe.CheckError, match='video_decode_failed'):
        probe.decode_video(b'not video')


def test_same_origin_resolves_hls_relative_paths_and_blocks_token_leaks():
    assert probe.same_origin('http://ha.example:8123/api/hls/master.m3u8', 'segment.ts') == 'http://ha.example:8123/api/hls/segment.ts'
    for target in ['https://external.example/segment', '//external.example/segment', 'http://:password@ha.example:8123/private']:
        with pytest.raises(probe.CheckError): probe.same_origin('http://ha.example:8123', target)


def test_download_bounds_status_and_error_document():
    async def run():
        session = FakeSession({'https://cloud.example/private?token=secret': FakeResponse(b'{"token":"secret"}'),
                               'https://cloud.example/large': FakeResponse(b'x'*20),
                               'https://cloud.example/denied': FakeResponse(b'no', 403)})
        for url, limit, reason in [('https://cloud.example/private?token=secret', 200, 'error_document_instead_of_video'),
                                   ('https://cloud.example/large', 10, 'download_limit'),
                                   ('https://cloud.example/denied', 10, 'media_http_error')]:
            with pytest.raises(probe.CheckError, match=reason) as failure:
                await probe.download(session, url, limit)
            assert 'secret' not in str(failure.value)
        assert all(call[1]['allow_redirects'] is False for call in session.calls)
    asyncio.run(run())


def test_ha_hls_master_playlist_and_relative_segment_auth():
    async def run():
        ts = sample_ts()
        session = FakeSession({
            'https://ha.example/api/master.m3u8': FakeResponse(b'#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild/index.m3u8\n'),
            'https://ha.example/api/child/index.m3u8': FakeResponse(b'#EXTM3U\n#EXTINF:1,\none.ts\n#EXT-X-ENDLIST\n'),
            'https://ha.example/api/child/one.ts': FakeResponse(ts),
        })
        connection = SimpleNamespace(base='https://ha.example', headers={'Authorization':'Bearer fixture-ha-token'}, session=session)
        result = await probe.ha_hls(connection, '/api/master.m3u8', SimpleNamespace(max_bytes=1000000))
        assert result['decoded_frames'] == 12
        assert all(call[1]['headers'] == connection.headers for call in session.calls)
        session.responses['https://ha.example/api/child/index.m3u8'] = FakeResponse(b'#EXTM3U\n#EXTINF:1,\nhttps://external.example/file\n')
        with pytest.raises(probe.CheckError, match='external_ha_media_url'):
            await probe.ha_hls(connection, '/api/master.m3u8', SimpleNamespace(max_bytes=1000000))
        assert not any('external.example' in call[0] for call in session.calls)
    asyncio.run(run())


def test_ha_low_latency_parts_can_be_decoded():
    async def run():
        session = FakeSession({
            'https://ha.example/playlist.m3u8': FakeResponse(b'#EXTM3U\n#EXT-X-PART:DURATION=1,URI="part.ts"\n'),
            'https://ha.example/part.ts': FakeResponse(sample_ts()),
        })
        connection = SimpleNamespace(base='https://ha.example', headers={}, session=session)
        result = await probe.ha_hls(connection, '/playlist.m3u8', SimpleNamespace(max_bytes=1000000))
        assert result['status'] == 'pass'
    asyncio.run(run())


def test_cloud_report_does_not_leak_account_camera_or_tokens(tmp_path):
    async def run():
        p = probe.load_protocol(); closed = []
        class Account:
            def __init__(self, *args): pass
            async def async_login(self): return 'private-account-id'
            async def async_devices(self):
                return [p.account.CloudDevice('987654321', 'Private bedroom', metadata={'type':2, 'access_token':'private-vendor-token'})]
            async def async_close(self): closed.append(True)
        class Catalog:
            def __init__(self, *args, **kwargs): pass
            async def async_dates(self, device): return []
        args = probe.parser().parse_args(['--report', str(tmp_path/'result.json')]);args.max_bytes = 32*1024*1024
        with patch.object(p.account, 'AccountClient', Account), patch.object(p.recordings, 'CloudRecordings', Catalog), \
             patch('builtins.input', return_value='private-email@example.invalid'), patch.object(probe.getpass, 'getpass', return_value='private-password'):
            code = await probe.run(args)
        report = json.loads(args.report.read_text())
        assert code == 2 and report['overall'] == 'incomplete'
        assert report['cameras'][0]['live']['status'] == 'unsupported'
        assert report['cameras'][0]['recordings']['reason'] == 'no_cloud_recording_dates'
        for secret in ['987654321','Private bedroom','private-email','private-password','private-vendor-token','private-account-id']:
            assert secret not in args.report.read_text()
        assert closed == [True]
    asyncio.run(run())


def test_no_false_pass_when_checks_missing_or_blocked():
    assert probe.combine([]) == 'incomplete'
    assert probe.combine([{'status':'pass'}, {'status':'unsupported'}]) == 'incomplete'
    assert probe.combine([{'status':'pass'}, {'status':'fail'}]) == 'fail'
    assert probe.combine([{'status':'pass'}]) == 'pass'
    result = probe.failure(probe.load_protocol().errors.CloudError('secret URL https://cloud.example/?token=secret result 123'))
    assert 'secret' not in json.dumps(result) and result['vendor_codes'] == [123]


def test_optional_home_assistant_api_and_media_workflow():
    async def run():
        ts = sample_ts()
        class WebSocket:
            def __init__(self): self.queue=[{'type':'auth_required'}];self.sent=[];self.closed=False;self._response=SimpleNamespace(url='https://ha.example/api/websocket')
            async def receive_json(self): return self.queue.pop(0)
            async def send_json(self, message):
                self.sent.append(message)
                kind=message['type']
                if kind=='auth':
                    self.queue.append({'type':'auth_ok'});return
                if kind=='config/entity_registry/list':
                    value=[{'entity_id':'camera.private_name','platform':'v380_cloud','disabled_by':None}]
                elif kind=='camera/stream': value={'url':'/stream.m3u8'}
                elif kind=='media_source/browse_media':
                    level=message['media_content_id'].count('/')-2
                    if level>=3: value={'children':[{'can_play':True,'media_content_id':'private-clip-id'}]}
                    else:value={'children':[{'can_play':False,'media_content_id':message['media_content_id']+'/next'}]}
                elif kind=='media_source/resolve_media':value={'url':'/private-recording.ts?authSig=fixture-signature','mime_type':'video/mp2t'}
                else:raise AssertionError(kind)
                self.queue.append({'id':message['id'],'type':'result','success':True,'result':value})
            async def close(self): self.closed=True
        ws=WebSocket()
        class Session(FakeSession):
            async def ws_connect(self, url, **kwargs):
                assert url=='https://ha.example/api/websocket'
                return ws
        session=Session({
            'https://ha.example/api/config':FakeResponse(b'{"version":"2026.10.2"}'),
            'https://ha.example/api/states/camera.private_name':FakeResponse(b'{"state":"idle"}'),
            'https://ha.example/stream.m3u8':FakeResponse(b'#EXTM3U\n#EXTINF:1,\nstream.ts\n'),
            'https://ha.example/stream.ts':FakeResponse(ts),
            'https://ha.example/private-recording.ts?authSig=fixture-signature':FakeResponse(ts),
        })
        args=SimpleNamespace(ha_url='https://ha.example',max_bytes=1000000)
        with patch.object(probe.getpass,'getpass',return_value='private-ha-token'):
            result=await probe.homeassistant_check(session,args)
        assert result['status']=='pass' and result['camera_count']==1
        assert result['recording']['decoded_frames']==12
        assert ws.closed
        for private in ('private-ha-token','fixture-signature','private_name','private-clip-id'):
            assert private not in json.dumps(result)
    asyncio.run(run())


def test_websocket_redirect_guard_runs_before_token_auth():
    async def run():
        class WS:
            _response=SimpleNamespace(url='https://external.example/api/websocket')
            sent=[]
            async def send_json(self, message): self.sent.append(message)
        ws=WS()
        class Session:
            async def ws_connect(self, *args, **kwargs): return ws
        client=probe.HAConnection(Session(),'https://ha.example','private-ha-token')
        with pytest.raises(probe.CheckError, match='external_ha_media_url'):
            await client.connect()
        assert not ws.sent
    asyncio.run(run())
