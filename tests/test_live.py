"""Offline protocol tests. Fake relays only; never connect to vendor services."""
from __future__ import annotations

import asyncio
import base64
from contextlib import nullcontext
import importlib.util
import json
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

# The wire protocol is independent of Home Assistant and can be tested without
# installing Home Assistant or running its integration package initializer.
_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "v380_cloud"
_PACKAGE = "_v380_live_test_protocol"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_COMPONENT)]
    sys.modules[_PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{_PACKAGE}.live", _COMPONENT / "live.py")
live = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = live
spec.loader.exec_module(live)


def aes_encrypt(data: bytes, key: bytes) -> bytes:
    context = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return context.update(data) + context.finalize()


def decrypt_padded(data: bytes, key: bytes) -> bytes:
    context = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    data = context.update(data) + context.finalize()
    unpadder = PKCS7(128).unpadder()
    return unpadder.update(data) + unpadder.finalize()


def fragment(payload: bytes, count: int, index: int) -> bytes:
    header = bytearray(12)
    header[:2] = b"\x7f\x28"
    struct.pack_into("<HHH", header, 3, count, index, len(payload))
    return bytes(header) + payload


class CodecTests(unittest.TestCase):
    def test_native_key_layout_and_selective_decryption(self):
        key = live.media_key(0x12345678)
        self.assertEqual(key.hex(), "785634125c79142c46238161f00d8082")
        plain = bytes(range(200))
        encrypted = bytearray(plain)
        encrypted[:64] = aes_encrypt(plain[:64], key)
        encrypted[80:144] = aes_encrypt(plain[80:144], key)
        self.assertEqual(live.decrypt_media(encrypted, key, audio=False), plain)
        self.assertEqual(live.decrypt_media(b"short tail", key, audio=False), b"short tail")

    def test_first_2k_encryption_boundary(self):
        key = live.media_key(7)
        plain = b"camera frame data" * 140
        encrypted = aes_encrypt(plain[:2048], key) + plain[2048:]
        self.assertEqual(live.decrypt_media(encrypted, key, audio=False, first_2k=True), plain)

    def test_fragments_emit_once_and_drop_missing_fragment(self):
        plain = b"\x00\x00\x00\x01\x67" + b"x" * 30
        frame = struct.pack("<IHHQ", 14, 0, 0, 123456) + plain
        decoder = live.FrameDecoder(9, 30, 22)
        self.assertIsNone(decoder.feed(fragment(frame[:20], 2, 0)[:12], frame[:20]))
        decoded = decoder.feed(fragment(frame[20:], 2, 1)[:12], frame[20:])
        self.assertEqual((decoded.payload, decoded.codec, decoded.timestamp, decoded.keyframe), (plain, "h264", 123456, True))
        self.assertIsNone(decoder.feed(fragment(frame[:20], 3, 0)[:12], frame[:20]))
        self.assertIsNone(decoder.feed(fragment(frame[20:], 3, 2)[:12], frame[20:]))

    def test_bounds_and_new_protocol_rejected(self):
        with self.assertRaises(live.UnsupportedError):
            live.CloudLiveClient({"device_id": 42, "type": 2})
        with self.assertRaises(live.CloudError):
            live.FrameDecoder(0, 31, 22).feed(b"\x7f" + bytes(11), b"")
        with self.assertRaises(live.CloudError):
            live.parse_dispatch(struct.pack("<H", 200) + bytes(62))


class FakeCloudTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_login_relay_and_fragmented_video_in_memory(self):
        await self._exercise_cloud(use_tcp=False)

    async def test_dispatch_login_relay_and_fragmented_video_loopback(self):
        await self._exercise_cloud(use_tcp=True)

    async def test_fixed_parameter_header_updates_encryption_mode(self):
        await self._exercise_cloud(use_tcp=False, parameter_marker=0x1F)

    async def _exercise_cloud(self, *, use_tcp, parameter_marker=0x2F):
        device_id, handle, session = 42, 0x12345678, 19
        camera_password = "synthetic-test-password"
        plain = b"\x00\x00\x00\x01\x67" + bytes(range(120))
        encrypted = bytearray(plain)
        encrypted[:64] = aes_encrypt(plain[:64], live.media_key(handle))
        frame = struct.pack("<IHHQ", 7, 0, 0, 1234000) + encrypted
        errors = []
        tasks = set()
        completed = asyncio.Event()

        def guarded(fn):
            async def callback(reader, writer):
                task = asyncio.current_task()
                tasks.add(task)
                try:
                    await fn(reader, writer)
                except Exception as err:
                    errors.append(err)
                finally:
                    writer.close()
                    await writer.wait_closed()
                    tasks.discard(task)
            return callback

        async def dispatch(reader, writer):
            packet = await reader.readexactly(144)
            command, zone_length = struct.unpack_from("<HH", packet)
            device_length = struct.unpack_from("<H", packet, 4 + zone_length)[0]
            offset = 6 + zone_length
            random_key = packet[offset + device_length:offset + device_length + 16]
            zone = decrypt_padded(decrypt_padded(packet[4:4 + zone_length], random_key), live.STATIC_KEY)
            device = decrypt_padded(decrypt_padded(packet[offset:offset + device_length], random_key), live.STATIC_KEY)
            self.assertEqual((command, zone, struct.unpack_from("<I", device)[0]), (100, b"", device_id))
            self.assertEqual(device[4:], random_key)
            response = bytearray(64)
            struct.pack_into("<H", response, 0, 200)
            response[6] = 1
            response[8:12] = b"\x7f\x00\x00\x01"
            writer.write(response[:11])
            await writer.drain()
            await asyncio.sleep(0)
            writer.write(response[11:])
            await writer.drain()

        async def login(reader, writer):
            magic, length = struct.unpack("<II", await reader.readexactly(8))
            self.assertEqual(magic, 0xFE000300)
            request = json.loads(await reader.readexactly(length))
            params = request["params"]
            random_key = params["randomKey"].encode()
            password = decrypt_padded(decrypt_padded(base64.b64decode(params["password"]), random_key), live.STATIC_KEY)
            self.assertEqual(password.decode(), camera_password)
            self.assertEqual((params["deviceId"], params["domain"], params["username"]), (42, "42.nvdvr.net", "admin"))
            response = json.dumps({"id": request["id"], "result": {"code": 1001, "errorId": 4}, "v380": {"handle": handle, "session": session, "version": 31}}).encode()
            writer.write(struct.pack("<II", magic, len(response)) + response)
            await writer.drain()

        async def relay(reader, writer):
            packet = await reader.readexactly(256)
            self.assertEqual(struct.unpack_from("<II", packet), (301, 1002))
            self.assertEqual(packet[8:20], b"42.nvdvr.net")
            self.assertEqual(struct.unpack_from("<IIII", packet, 58), (8800, device_id, handle, session))
            response = bytearray(256)
            struct.pack_into("<III", response, 0, 401, 1001, 4)
            response[12] = 21  # Parameter packets below select the 64/80 mode.
            writer.write(response)
            await writer.drain()
            self.assertEqual(struct.unpack_from("<II", await reader.readexactly(256)), (303, 0x3001))
            # Parameter header body must be skipped even when TCP coalesces it.
            params = bytearray(12)
            if parameter_marker == 0x2F:
                params[:2] = b"\x2f\x16"
                struct.pack_into("<H", params, 2, 5)
                params += b"param"
            else:
                params[0] = 0x1F
                params[5] = 22
            media_packet = fragment(frame[:29], 2, 0) + fragment(frame[29:], 2, 1)
            writer.write(params + media_packet * 15)
            await writer.drain()
            acknowledgement = await reader.readexactly(16)
            self.assertEqual(struct.unpack_from("<IH", acknowledgement), (5005, 30))
            self.assertEqual(acknowledgement[8:], bytes(8))
            await reader.read()
            completed.set()

        class MemoryWriter:
            def __init__(self, peer):
                self.peer = peer
                self.closed = False

            def write(self, data):
                if self.closed:
                    raise ConnectionError("closed")
                self.peer.feed_data(bytes(data))

            async def drain(self):
                await asyncio.sleep(0)

            def close(self):
                if not self.closed:
                    self.closed = True
                    self.peer.feed_eof()

            async def wait_closed(self):
                await asyncio.sleep(0)

        handlers = {8000: dispatch, 8089: login, 8800: relay}

        async def memory_connection(host, port):
            self.assertEqual(host, "127.0.0.1")
            client_reader, server_reader = asyncio.StreamReader(), asyncio.StreamReader()
            client_writer, server_writer = MemoryWriter(server_reader), MemoryWriter(client_reader)
            task = asyncio.create_task(guarded(handlers[port])(server_reader, server_writer))
            tasks.add(task)
            return client_reader, client_writer

        servers = []
        if use_tcp:
            try:
                for handler in (dispatch, login, relay):
                    servers.append(await asyncio.start_server(guarded(handler), "127.0.0.1", 0))
            except PermissionError:
                for server in servers:
                    server.close()
                    await server.wait_closed()
                self.skipTest("The sandbox does not permit loopback server sockets")
            client = live.CloudLiveClient({"device_id": device_id}, camera_password, dispatch_server="127.0.0.1", dispatch_port=servers[0].sockets[0].getsockname()[1], login_port=servers[1].sockets[0].getsockname()[1], relay_port=servers[2].sockets[0].getsockname()[1], timeout=2)
            connection_context = nullcontext()
        else:
            client = live.CloudLiveClient({"device_id": device_id}, camera_password, dispatch_server="127.0.0.1", timeout=2)
            connection_context = patch.object(asyncio, "open_connection", side_effect=memory_connection)
        generator = client.async_frames()
        try:
            with connection_context:
                decoded = await asyncio.wait_for(anext(generator), 5)
                for _ in range(14):
                    await asyncio.wait_for(anext(generator), 5)
            self.assertEqual((decoded.payload, decoded.codec, decoded.timestamp, decoded.keyframe), (plain, "h264", 1234000, True))
        finally:
            await generator.aclose()
            for server in servers:
                server.close()
                await server.wait_closed()
            await asyncio.wait_for(completed.wait(), 2)
            if tasks:
                await asyncio.gather(*tasks)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
