"""Portable legacy V380 cloud relay transport, recovered from the supplied APK.

Static sources: classes9.dex DispatchServerGetter / LoginHelper / Functions;
libhsMediaLibrary.so HSLiveDataV2Transmitter and libaes.so media_data_decrypt.
The newer MQTT/key-exchange platform is deliberately reported unsupported.
No camera, LAN discovery or Android native library is required.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass
import json
import secrets
import socket
import struct
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import AuthError, CloudError, UnsupportedError

STATIC_KEY = b"macrovideo+*#!^@"
DEFAULT_DISPATCH_SERVER = "dispa1.av380.net"
RPC_MAGIC = 0xFE000300
MAX_JSON = 1024 * 1024
MAX_FRAME = 8 * 1024 * 1024


def _aes_ecb(data: bytes, key: bytes, *, decrypt: bool = False) -> bytes:
    context = Cipher(algorithms.AES(key), modes.ECB())
    worker = context.decryptor() if decrypt else context.encryptor()
    return worker.update(data) + worker.finalize()


def _padded_encrypt(data: bytes, key: bytes) -> bytes:
    padder = PKCS7(128).padder()
    return _aes_ecb(padder.update(data) + padder.finalize(), key)


def double_encrypt(data: bytes, random_key: bytes) -> bytes:
    """Match Functions.encrypt(Functions.encrypt(data, static), random)."""
    return _padded_encrypt(_padded_encrypt(data, STATIC_KEY), random_key)


def media_key(handle: int) -> bytes:
    """HSLiveDataV2Transmitter::updateAesKey, VA 0x455d0."""
    return struct.pack("<IQI", handle & 0xFFFFFFFF, 0x618123462C14795C, 0x82800DF0)


def decrypt_media(data: bytes, key: bytes, *, audio: bool, first_2k: bool = False) -> bytes:
    """Apply the native block selection without treating the tail as padding."""
    result = bytearray(data)
    if audio or first_2k:
        count = len(data) // 16 * 16
        if first_2k and not audio:
            count = min(count, 2048)
        if count:
            result[:count] = _aes_ecb(data[:count], key, decrypt=True)
    else:
        # libaes media_data_decrypt: 64 encrypted bytes per 80 byte group.
        for offset in range(0, len(data) - 63, 80):
            result[offset:offset + 64] = _aes_ecb(data[offset:offset + 64], key, decrypt=True)
    return bytes(result)


def dispatch_request(device_id: int, zone: str, random_key: bytes) -> bytes:
    encrypted_zone = double_encrypt(zone.encode("utf-8"), random_key)
    encrypted_device = double_encrypt(struct.pack("<I", device_id) + random_key, random_key)
    packet = (
        struct.pack("<HH", 100, len(encrypted_zone)) + encrypted_zone
        + struct.pack("<H", len(encrypted_device)) + encrypted_device + random_key
    )
    if len(packet) > 144:
        raise CloudError("The cloud routing zone is too long")
    return packet.ljust(144, b"\x00")


def parse_dispatch(data: bytes) -> list[str]:
    if len(data) != 64 or struct.unpack_from("<H", data)[0] != 200:
        code = struct.unpack_from("<H", data)[0] if len(data) >= 2 else None
        raise CloudError("The cloud dispatcher rejected the routing request",
                         stage="dispatcher_response", reason="dispatcher_rejected", code=code)
    normal, pano = data[6], data[7]
    if normal + pano > 14 or not normal + pano:
        raise CloudError("The cloud dispatcher returned no usable relays",
                         stage="dispatcher_response", reason="no_relays")
    # Normal servers are first; panoramic devices can use the second group.
    return [socket.inet_ntoa(data[8 + index * 4:12 + index * 4]) for index in range(normal + pano)]


def relay_request(device_id: int, domain: str, port: int, handle: int, session: int, *, audio: bool) -> bytes:
    """Native command 301, recovered at VA 0x47c14 (packed, unaligned)."""
    domain_bytes = domain.encode("ascii")
    if len(domain_bytes) > 49:
        raise CloudError("The camera cloud domain is too long")
    packet = bytearray(256)
    struct.pack_into("<II", packet, 0, 301, 1002)
    packet[8:8 + len(domain_bytes)] = domain_bytes
    struct.pack_into("<IIII", packet, 58, port, device_id, handle & 0xFFFFFFFF, session & 0xFFFFFFFF)
    struct.pack_into("<I", packet, 74, 0)  # default stream / resolution
    packet[78] = 20  # requested frame rate
    struct.pack_into("<I", packet, 79, 4097 if audio else 4096)
    struct.pack_into("<I", packet, 83, 0)
    struct.pack_into("<H", packet, 88, 0x0101)
    packet[90] = 1
    return bytes(packet)


@dataclass(frozen=True, slots=True)
class Frame:
    payload: bytes
    codec: str
    timestamp: int
    keyframe: bool = False
    sequence: int = 0


class FrameDecoder:
    """Reassemble bounded packet fragments, then decrypt the complete frame."""

    def __init__(self, handle: int, communication_version: int, media_version: int) -> None:
        self.key = media_key(handle)
        self.communication_version = communication_version
        self.media_version = media_version
        self._parts: list[bytes] = []
        self._total = 0
        self._next = 0
        self._size = 0

    def feed(self, header: bytes, payload: bytes) -> Frame | None:
        if len(header) != 12 or header[0] != 0x7F:
            raise CloudError("Invalid cloud media packet")
        total, current, length = struct.unpack_from("<HHH", header, 3)
        if not total or current >= total or length != len(payload) or length > 8000:
            raise CloudError("Invalid cloud media fragment bounds")
        if current == 0:
            self._parts = []
            self._total, self._next, self._size = total, 0, 0
        if total != self._total or current != self._next:
            self._parts = []
            self._total = self._next = self._size = 0
            return None
        self._parts.append(payload)
        self._next += 1
        self._size += len(payload)
        if self._size > MAX_FRAME:
            raise CloudError("Cloud media frame exceeds the size limit")
        if self._next != self._total:
            return None
        data = b"".join(self._parts)
        self._parts = []
        self._total = self._next = self._size = 0
        if len(data) < 16:
            raise CloudError("Cloud media frame metadata is incomplete")
        sequence, frame_type, _channel, timestamp = struct.unpack_from("<IHHQ", data)
        payload = data[16:]
        codec = {0: "h264", 1: "h264", 40: "hevc", 41: "hevc", 50: "h264", 51: "h264", 52: "hevc", 53: "hevc", 24: "aac"}.get(frame_type)
        if codec is None:
            # Other values include ADPCM, telemetry and camera control notices.
            return None
        if self.communication_version >= 31:
            payload = decrypt_media(payload, self.key, audio=codec == "aac", first_2k=self.media_version == 21)
        keyframe = frame_type in (0, 40, 50, 52)
        if keyframe and not payload.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            raise CloudError("The cloud video key did not produce a valid Annex B frame",
                             stage="media_stream", reason="video_key_invalid")
        return Frame(payload, codec, timestamp, keyframe, sequence)


class CloudLiveClient:
    """A one-viewer cloud session; the caller consumes and closes async_frames."""

    def __init__(
        self,
        metadata: Mapping[str, Any],
        device_password: str = "",
        *,
        zone: str = "",
        dispatch_server: str | None = None,
        dispatch_port: int = 8000,
        login_port: int = 8089,
        relay_port: int = 8800,
        timeout: float = 15,
        audio: bool = True,
    ) -> None:
        self.metadata = dict(metadata)
        self.device_id = int(metadata.get("device_id", metadata.get("id", 0)))
        if not 0 < self.device_id < 2**32:
            raise CloudError("A valid numeric V380 camera ID is required")
        if int(metadata.get("type", 0)) >= 2:
            raise UnsupportedError("This camera uses the newer cloud key-exchange video protocol")
        self._password = device_password
        self.username = str(metadata.get("device_account") or "admin")
        self.domain = f"{self.device_id}.nvdvr.net"
        self.camera_port = int(metadata.get("device_port") or 8800)
        self.zone = zone
        sub_server = metadata.get("sub_server")
        self.dispatch_server = dispatch_server or (sub_server if isinstance(sub_server, str) and sub_server else DEFAULT_DISPATCH_SERVER)
        self.dispatch_port = dispatch_port
        self.login_port = login_port
        self.relay_port = relay_port
        self.timeout = timeout
        self.audio = audio
        self._writer: asyncio.StreamWriter | None = None

    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(asyncio.open_connection(host, port), self.timeout)

    async def _read(self, reader: asyncio.StreamReader, length: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(length), self.timeout)

    @staticmethod
    async def _close(writer: asyncio.StreamWriter) -> None:
        writer.close()
        with suppress(OSError, asyncio.CancelledError):
            await writer.wait_closed()

    async def async_relays(self) -> list[str]:
        """Ask the app's dispatcher for cloud relays; never probe the LAN."""
        writer = None
        stage = "dispatcher_connect"
        try:
            reader, writer = await self._open(self.dispatch_server, self.dispatch_port)
            stage = "dispatcher_response"
            random_key = self._random_key()
            writer.write(dispatch_request(self.device_id, self.zone, random_key))
            await writer.drain()
            return parse_dispatch(await self._read(reader, 64))
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise self._transport_error(err, stage) from err
        finally:
            if writer is not None:
                await self._close(writer)

    @staticmethod
    def _random_key() -> bytes:
        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(secrets.choice(alphabet) for _ in range(16)).encode("ascii")

    @staticmethod
    def _transport_error(err: Exception, stage: str) -> CloudError:
        if isinstance(err, CloudError) and err.diagnostic:
            return err
        if isinstance(err, socket.gaierror):
            reason = "dns_failed"
        elif isinstance(err, TimeoutError):
            reason = "network_timeout"
        elif isinstance(err, asyncio.IncompleteReadError):
            reason = "incomplete_response"
        elif isinstance(err, OSError):
            reason = "connection_failed"
        elif stage == "media_stream":
            reason = "media_packet_invalid"
        else:
            reason = "session_failed"
        return CloudError("The V380 cloud video request failed", stage=stage, reason=reason)

    async def _login(self, host: str) -> dict[str, Any]:
        writer = None
        stage = "device_login_connect"
        try:
            reader, writer = await self._open(host, self.login_port)
            stage = "device_login_response"
            random_key = self._random_key()
            request_id = secrets.randbelow(99999999) + 100
            payload = {
                "id": request_id,
                "method": "login",
                "params": {
                    "version": 31,
                    "phoneType": 1012,
                    "deviceId": self.device_id,
                    "domain": self.domain,
                    "port": self.camera_port,
                    "accountId": 11,
                    "username": self.username,
                    "password": base64.b64encode(double_encrypt(self._password.encode("utf-8"), random_key)[:112]).decode("ascii"),
                    "randomKey": random_key.decode("ascii"),
                    "connectType": 0,
                    "securityLevel": 1,
                    "agora": 0,
                    "ectx": 0,
                    "p2pIdx": int(self.metadata.get("p_id") or 0),
                },
            }
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            writer.write(struct.pack("<II", RPC_MAGIC, len(data)) + data)
            await writer.drain()
            header = await self._read(reader, 8)
            magic, length = struct.unpack("<II", header)
            if magic != RPC_MAGIC or not 0 < length <= MAX_JSON:
                raise CloudError("Invalid cloud login response framing", stage=stage, reason="invalid_framing")
            try:
                response = json.loads(await self._read(reader, length))
            except (ValueError, UnicodeError) as err:
                raise CloudError("Invalid cloud login response", stage=stage, reason="invalid_json") from err
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise CloudError("The cloud login response does not match this request", stage=stage, reason="request_mismatch")
            result = response.get("result") or {}
            if not isinstance(result, dict):
                raise CloudError("The cloud login result is invalid", stage=stage, reason="invalid_result")
            if result.get("code") != 1001:
                if result.get("code") in (1011, 1012):
                    raise AuthError("The cloud camera rejected its device credentials", stage=stage,
                                    reason="login_rejected", code=result.get("code"), error_id=result.get("errorId"))
                raise CloudError("The camera cloud login failed", stage=stage, reason="login_rejected",
                                 code=result.get("code"), error_id=result.get("errorId"))
            info = response.get("v380")
            if not isinstance(info, dict) or "handle" not in info or "session" not in info:
                raise CloudError("The cloud login did not return a video session", stage=stage, reason="missing_session")
            if int(info.get("version", 0)) < 21:
                raise UnsupportedError("This camera's old media framing is not supported", stage=stage,
                                       reason="media_version_unsupported", protocol_version=int(info.get("version", 0)))
            return info
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise self._transport_error(err, stage) from err
        finally:
            if writer is not None:
                await self._close(writer)

    async def async_frames(self) -> AsyncIterator[Frame]:
        """Yield decrypted compressed frames over a source-backed cloud relay."""
        relays = await self.async_relays()
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        for host in relays:
            emitted = False
            stage = "device_login_connect"
            try:
                login = await self._login(host)
                stage = "relay_connect"
                reader, writer = await self._open(host, self.relay_port)
                self._writer = writer
                stage = "relay_handshake"
                handle, session = int(login["handle"]), int(login["session"])
                writer.write(relay_request(self.device_id, self.domain, self.camera_port, handle, session, audio=self.audio))
                await writer.drain()
                response = await self._read(reader, 32)
                command, result, _status = struct.unpack_from("<III", response)
                media_version = response[12]
                if command != 401 or result != 1001:
                    raise CloudError("The cloud relay rejected the video session", stage=stage,
                                     reason="relay_rejected", code=result)
                if media_version < 21:
                    raise UnsupportedError("This camera's old media framing is not supported", stage=stage,
                                           reason="media_version_unsupported", protocol_version=media_version)
                # SDK V2 expects 256 bytes of login response for v21+ devices.
                await self._read(reader, 224)
                start = struct.pack("<II", 303, 0x3001).ljust(256, b"\x00")
                writer.write(start)
                await writer.drain()
                decoder = FrameDecoder(handle, int(login.get("version", 31)), media_version)
                stage = "media_stream"
                received_fragments = 0
                last_ack = asyncio.get_running_loop().time()
                while True:
                    header = await self._read(reader, 12)
                    marker = header[0]
                    if marker == 0x7F:
                        length = struct.unpack_from("<H", header, 7)[0]
                        if length > 8000:
                            raise CloudError("The cloud relay sent an oversized fragment")
                        frame = decoder.feed(header, await self._read(reader, length))
                        received_fragments += 1
                        # Native V2 acknowledges each 30 fragments, command5005.
                        if received_fragments % 30 == 0:
                            now = asyncio.get_running_loop().time()
                            elapsed = int((now - last_ack) * 1000) & 0xFFFF
                            writer.write(struct.pack("<IHH", 5005, received_fragments & 0xFFFF, elapsed).ljust(16, b"\x00"))
                            await writer.drain()
                            last_ack = now
                        if frame is not None:
                            emitted = True
                            yield frame
                    elif marker == 0x2F:
                        length = struct.unpack_from("<H", header, 2)[0]
                        await self._read(reader, length)
                        if header[1] < 20:
                            raise UnsupportedError("This camera selected an old media frame format", stage=stage,
                                                   reason="media_version_unsupported", protocol_version=header[1])
                        decoder.media_version = header[1]
                    elif marker == 0x1F:
                        if header[5] < 20:
                            raise UnsupportedError("This camera selected an old media frame format", stage=stage,
                                                   reason="media_version_unsupported", protocol_version=header[5])
                        decoder.media_version = header[5]
                    elif marker not in (0x3F, 0x4F, 0x5F, 0x6F):
                        raise UnsupportedError("The cloud relay sent an unsupported media packet", stage=stage,
                                               reason="packet_type_unsupported", packet_type=marker)
            except AuthError:
                raise
            except UnsupportedError:
                raise
            except (CloudError, OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, TypeError) as err:
                detail = self._transport_error(err, stage)
                attempts.append(dict(detail.diagnostic))
                detail.relay_attempts = attempts[-14:]
                if emitted:
                    if detail is err:
                        raise
                    raise detail from err
                last_error = detail
            finally:
                if self._writer is not None:
                    await self._close(self._writer)
                    self._writer = None
        if last_error is not None:
            raise last_error
        raise CloudError("No V380 cloud relay could start this camera")

    async def async_close(self) -> None:
        if self._writer is not None:
            await self._close(self._writer)
            self._writer = None
