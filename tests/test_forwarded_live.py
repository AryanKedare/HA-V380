"""Offline binary cloud forwarding: independent wire checks and real decoding."""
import asyncio
import base64
import importlib.util
import io
from pathlib import Path
import struct
import sys
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SOURCE = Path(__file__).resolve().parents[1] / 'tools/full_test_core.py'
spec = importlib.util.spec_from_file_location('v380_forwarded_probe_test', SOURCE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def aes(data, key, *, decrypt=False):
    cipher = Cipher(algorithms.AES(key), modes.ECB())
    worker = cipher.decryptor() if decrypt else cipher.encryptor()
    return worker.update(data) + worker.finalize()


def video_packets(p):
    from fractions import Fraction
    codec = p.av.CodecContext.create('libx264', 'w')
    codec.width = codec.height = 64
    codec.pix_fmt = 'yuv420p'
    codec.time_base = Fraction(1, 25)
    codec.framerate = Fraction(25, 1)
    codec.options = {'preset': 'ultrafast', 'tune': 'zerolatency'}
    codec.open()
    packets = []
    for i in range(4):
        frame = p.av.VideoFrame(64, 64, 'yuv420p')
        frame.pts, frame.time_base = i, Fraction(1, 25)
        for plane in frame.planes:
            plane.update(bytes([i * 30]) * plane.buffer_size)
        packets.extend((bytes(packet), packet.is_keyframe) for packet in codec.encode(frame))
    packets.extend((bytes(packet), packet.is_keyframe) for packet in codec.encode(None))
    return packets


class ForwardedLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_homeassistant_api_streams_forwarded_video_and_releases_session(self):
        await self.exercise(through_api=True)

    async def test_forwarded_reply_lengths_and_real_encrypted_video(self):
        for length, pre2k in ((26, False), (256, False), (412, True)):
            with self.subTest(reply_length=length, pre2k=pre2k):
                await self.exercise(reply_length=length, pre2k=pre2k)

    async def test_corrupt_media_cannot_pass_after_positive_handshake(self):
        await self.exercise(corrupt=True)

    async def test_forwarded_rejections_never_send_start_or_leak_private_fields(self):
        for code in (-11, -12, 1002):
            with self.subTest(code=code):
                await self.exercise(result_code=code)

    async def test_fragment_limit_remains_bounded(self):
        p = probe.load_protocol()
        payload = b'\x00\x00\x00\x01' + bytes(9000)
        frame = struct.pack('<IHHQ', 0, 0, 0, 0) + payload
        header = bytearray(12)
        header[0] = 0x7F
        struct.pack_into('<HHH', header, 3, 1, 0, len(frame))
        self.assertEqual(p.live.FrameDecoder(7, 30, 0, fragment_limit=20000).feed(header, frame).payload, payload)
        with self.assertRaises(p.errors.CloudError):
            p.live.FrameDecoder(7, 30, 0).feed(header, frame)
        struct.pack_into('<H', header, 7, 20001)
        with self.assertRaises(p.errors.CloudError):
            p.live.FrameDecoder(7, 30, 0, fragment_limit=20000).feed(header, bytes(20001))

    async def test_authentication_fallback_is_driven_by_specific_errors(self):
        p = probe.load_protocol()
        client = p.live.ForwardedCloudLiveClient({'device_id': 42, 'device_account': 'admin'}, 'fixture')
        failures = [p.errors.AuthError('private', stage='device_login_response', reason='login_rejected', code=1011),
                    p.errors.AuthError('private', stage='device_login_response', reason='login_rejected', code=1012),
                    {'handle': 7, 'session': 9, 'version': 31}]
        with patch.object(client, '_binary_login', side_effect=failures) as login:
            result = await client._login('fixture-relay')
        self.assertEqual(result['version'], 31)
        self.assertEqual([(call.args[1], call.kwargs['padded']) for call in login.call_args_list],
                         [('admin', False), ('42', False), ('42', True)])

    async def exercise(self, *, reply_length=256, pre2k=False, corrupt=False, result_code=100,
                       through_api=False):
        p = probe.load_protocol()
        handle, session = 0x12345678, 19
        # Independently construct the native media key instead of calling the implementation.
        key = struct.pack('<I', handle) + bytes.fromhex('5c79142c46238161f00d8082')
        packets = video_packets(p)
        if corrupt:
            packets = [(b'\x00\x00\x00\x01invalid-video', i == 0) for i in range(4)]
        tasks, errors, commands = [], [], []
        writers = []

        class Writer:
            def __init__(self, peer):
                self.peer, self.closed = peer, False
            def write(self, data): self.peer.feed_data(bytes(data))
            async def drain(self): await asyncio.sleep(0)
            def close(self):
                if not self.closed:
                    self.closed = True
                    self.peer.feed_eof()
            async def wait_closed(self): await asyncio.sleep(0)

        async def serve(reader, writer):
            try:
                command = struct.unpack('<I', await reader.readexactly(4))[0]
                commands.append(command)
                if command == 1167:
                    request = struct.pack('<I', command) + await reader.readexactly(516)
                    self.assertEqual(struct.unpack_from('<II', request), (1167, 1022))
                    self.assertEqual(request[8], 31)
                    self.assertEqual(struct.unpack_from('<II', request, 9), (1, 42))
                    self.assertEqual(request[17:67].rstrip(b'\x00'), b'42.nvdvr.net')
                    self.assertEqual(struct.unpack_from('<I', request, 67)[0], 8800)
                    self.assertEqual(request[71:103].rstrip(b'\x00'), b'42')
                    random_key = request[103:119]
                    password = aes(aes(request[119:167], random_key, decrypt=True), b'macrovideo+*#!^@', decrypt=True)
                    self.assertEqual(password.rstrip(b'\x00'), b'fixture-password')
                    reply = bytearray(256)
                    struct.pack_into('<III', reply, 0, 1168, 1001, 4)
                    reply[12] = 31
                    struct.pack_into('<II', reply, 13, handle, session)
                    writer.write(reply[:9])
                    await writer.drain()
                    writer.write(reply[9:])
                    await writer.drain()
                    return
                self.assertEqual(command, 301)
                request = struct.pack('<I', command) + await reader.readexactly(252)
                self.assertEqual(struct.unpack_from('<I', request, 4)[0], 1022)
                self.assertEqual(request[8:58].rstrip(b'\x00'), b'42.nvdvr.net')
                self.assertEqual(struct.unpack_from('<IIII', request, 58), (8800, 42, handle, session))
                self.assertEqual(request[78], 20)
                self.assertEqual(struct.unpack_from('<I', request, 79)[0], 1)
                self.assertEqual(request[83:], bytes(173))
                reply = bytearray(reply_length)
                struct.pack_into('<IiI', reply, 0, 401, result_code, 1)
                reply[12] = 13  # Opaque forwarding reply; never call this a media version.
                writer.write(reply[:5])
                await writer.drain()
                writer.write(reply[5:])
                await writer.drain()
                if result_code not in (100, 402, 1001):
                    self.assertEqual(await reader.read(), b'')
                    return
                start = await reader.readexactly(256)
                self.assertEqual(struct.unpack_from('<II', start), (303, 0x3001))
                commands.append(303)
                stream = bytearray()
                if pre2k:
                    stream.extend(b'\x2f\x15' + bytes(10))
                for index, (payload, keyframe) in enumerate(packets):
                    encrypted = bytearray(payload)
                    if pre2k:
                        size = min(len(payload) // 16 * 16, 2048)
                        encrypted[:size] = aes(payload[:size], key)
                    else:
                        for offset in range(0, len(payload) - 63, 80):
                            encrypted[offset:offset + 64] = aes(payload[offset:offset + 64], key)
                    frame = struct.pack('<IHHQ', index, 0 if keyframe else 1, 0, index * 40) + encrypted
                    # Exercise reassembly and coalesced reads as well as binary login.
                    parts = (frame[:23], frame[23:])
                    for current, part in enumerate(parts):
                        header = bytearray(12)
                        header[0] = 0x7F
                        struct.pack_into('<HHH', header, 3, 2, current, len(part))
                        stream.extend(header + part)
                writer.write(stream)
                await writer.drain()
                self.assertEqual(await reader.read(), b'')
            except Exception as err:
                errors.append(err)
            finally:
                writer.close()
                await writer.wait_closed()

        async def connect(host, port):
            self.assertEqual((host, port), ('fixture-relay', 8800))
            client_reader, server_reader = asyncio.StreamReader(), asyncio.StreamReader()
            client_writer, server_writer = Writer(server_reader), Writer(client_reader)
            writers.append(client_writer)
            tasks.append(asyncio.create_task(serve(server_reader, server_writer)))
            return client_reader, client_writer

        args = probe.parser().parse_args(['--live-only', '--live-transport', 'forwarded'])
        args.live_seconds, args.max_bytes = 1e-9, 1024 * 1024
        device = type('Device', (), {'metadata': {'device_id': 42, 'device_account': '42'}})()
        if through_api:
            name = p.live.__package__ + '.api'
            api_spec = importlib.util.spec_from_file_location(name, SOURCE.parents[1] / 'custom_components/v380_cloud/api.py')
            api = importlib.util.module_from_spec(api_spec)
            sys.modules[name] = api
            api_spec.loader.exec_module(api)
            api_client = api.V380CloudClient(None, 'fixture-account', 'fixture-account-password')
            api_client.devices['42'] = p.account.CloudDevice('42', 'fixture-camera', metadata={
                'device_id': 42, 'device_account': '42',
                'device_password': base64.b64encode(b'fixture-password').decode()})
        with patch.object(p.live.ForwardedCloudLiveClient, 'async_relays', return_value=['fixture-relay']), \
             patch.object(p.live.ForwardedCloudLiveClient, '_open', side_effect=connect), \
             patch.object(p.live.ForwardedCloudLiveClient, 'reply_idle_timeout', 0.005), \
             patch.object(p.account, 'device_password', return_value='fixture-password'):
            if through_api:
                frames = api_client.async_live_frames('42')
                mux, data = p.mpegts.MpegTsMuxer('h264'), bytearray()
                try:
                    for index in range(2):
                        frame = await anext(frames)
                        data.extend(mux.push(frame.payload, index / 25, keyframe=frame.keyframe))
                finally:
                    await frames.aclose()
                decoded = probe.decode_video(bytes(data), format='mpegts')
                self.assertGreaterEqual(decoded['decoded_frames'], 2)
                self.assertFalse(api_client._live_sessions)
                await api_client.async_close()
            elif result_code not in (100, 402, 1001):
                with self.assertRaises(p.errors.CloudError) as caught:
                    await probe.live_check(None, device, args, 1)
                result = probe.failure(caught.exception)
                self.assertEqual(result['code'], result_code)
                self.assertEqual(result['stage'], 'relay_handshake')
                self.assertNotIn('fixture-relay', str(result))
                self.assertNotIn('fixture-password', str(result))
            elif corrupt:
                with self.assertRaises(probe.CheckError):
                    await probe.live_check(None, device, args, 1)
            else:
                result = await probe.live_check(None, device, args, 1)
                self.assertEqual(result['status'], 'pass')
                self.assertGreaterEqual(result['decoded_frames'], 2)
                self.assertEqual(result['transport'], 'remote_cloud_relay_forwarded')
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
        if errors:
            raise errors[0]
        self.assertTrue(all(writer.closed for writer in writers))
        self.assertEqual(commands, [1167, 301] + ([303] if result_code in (100, 402, 1001) else []))
