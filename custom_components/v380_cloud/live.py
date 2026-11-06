"""Portable V380 legacy cloud transports.

Static sources: classes9.dex DispatchServerGetter / LoginHelper / Functions;
libhsMediaLibrary.so HSLiveDataV2Transmitter and libaes.so media_data_decrypt.
The binary 1022 forwarding path follows the source reference below and was
validated by a user-run AWT3710D live decode; it is the HA adapter's default.
The newer MQTT/key-exchange platform is deliberately reported unsupported.
No LAN discovery or Android native library is required.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
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


def parse_relay_response(response: bytes) -> int:
    """Validate the legacy 1002 reply without applying forwarding semantics."""
    if len(response) != 32:
        raise CloudError("Invalid cloud relay response framing",
                         stage="relay_handshake", reason="invalid_framing")
    command, result, status = struct.unpack_from("<Iii", response)
    media_version = response[12]
    diagnostic = dict(stage="relay_handshake", code=result,
                      relay_command=command, relay_status=status,
                      protocol_version=media_version)
    if command != 401:
        raise CloudError("Invalid cloud relay response command",
                         reason="invalid_framing", **diagnostic)
    if result != 1001:
        raise CloudError("The cloud relay did not accept the video session",
                         reason="relay_rejected", **diagnostic)
    if media_version < 21:
        raise UnsupportedError("This camera's old media framing is not supported",
                               reason="media_version_unsupported", **diagnostic)
    return media_version


@dataclass(frozen=True, slots=True)
class Frame:
    payload: bytes
    codec: str
    timestamp: int
    keyframe: bool = False
    sequence: int = 0


class FrameDecoder:
    """Reassemble bounded packet fragments, then decrypt the complete frame."""

    def __init__(self, handle: int, communication_version: int, media_version: int,
                 *, fragment_limit: int = 8000) -> None:
        self.key = media_key(handle)
        self.communication_version = communication_version
        self.media_version = media_version
        self.fragment_limit = fragment_limit
        self._parts: list[bytes] = []
        self._total = 0
        self._next = 0
        self._size = 0

    def feed(self, header: bytes, payload: bytes) -> Frame | None:
        if len(header) != 12 or header[0] != 0x7F:
            raise CloudError("Invalid cloud media packet")
        total, current, length = struct.unpack_from("<HHH", header, 3)
        if not total or current >= total or length != len(payload) or length > self.fragment_limit:
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


@dataclass(slots=True)
class RelayState:
    """Private per-camera routing hints; never put endpoint values in reports."""

    relays: list[str] = field(default_factory=list)
    discovered_at: float = float('-inf')
    preferred: str | None = None
    succeeded_at: float = float('-inf')
    attempted: dict[str, float] = field(default_factory=dict)
    failed: dict[str, float] = field(default_factory=dict)
    cache_seconds: float = 900.0
    cooldown_seconds: float = 60.0

    def order(self, relays: list[str], now: float) -> list[str]:
        def priority(host):
            blocked = now - self.failed.get(host, float('-inf')) < self.cooldown_seconds
            preferred = host == self.preferred and now - self.succeeded_at < self.cache_seconds
            return blocked, not (preferred and not blocked), self.attempted.get(host, float('-inf'))
        return sorted(dict.fromkeys(relays), key=priority)

    def cached(self, now: float) -> list[str]:
        relays = list(self.relays) if now - self.discovered_at < self.cache_seconds else []
        if self.preferred and now - self.succeeded_at < self.cache_seconds and self.preferred not in relays:
            relays.insert(0, self.preferred)
        return self.order(relays, now)

    def remember(self, relays: list[str], now: float) -> None:
        self.relays = list(dict.fromkeys(relays))[:14]
        self.discovered_at = now
        allowed = set(self.relays) | ({self.preferred} if self.preferred else set())
        self.attempted = {key: value for key, value in self.attempted.items() if key in allowed}
        self.failed = {key: value for key, value in self.failed.items() if key in allowed}

    def success(self, host: str, now: float) -> None:
        self.preferred, self.succeeded_at = host, now
        self.failed.pop(host, None)


class CloudLiveClient:
    """A one-viewer cloud session; the caller consumes and closes async_frames."""

    fragment_limit = 8000
    acknowledge_fragments = True
    reconnect_delays = (1.0, 2.0, 4.0)
    retry_reset_after = 30.0
    startup_timeout = 60.0
    # Start recovery before HA's 30-second source-read timeout expires.
    video_timeout = 15.0

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
        relay_state: RelayState | None = None,
        progress: dict[str, Any] | None = None,
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
        self.relay_state = relay_state if relay_state is not None else RelayState()
        self.progress = progress if progress is not None else {}
        for key in ('relays_tried', 'media_headers', 'video_frames', 'keyframes', 'transport_reconnects'):
            self.progress[key] = 0
        self.progress['stage'] = 'dispatcher_connect'
        self.login_protocol_version: int | None = None
        self.reconnect_count = 0
        self._last_relay: str | None = None
        self._close_event = asyncio.Event()
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
        self.progress['stage'] = stage
        try:
            reader, writer = await self._open(self.dispatch_server, self.dispatch_port)
            stage = "dispatcher_response"
            self.progress['stage'] = stage
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
        self.progress['stage'] = stage
        try:
            reader, writer = await self._open(host, self.login_port)
            stage = "device_login_response"
            self.progress['stage'] = stage
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

    async def _start_relay(self, host: str, login: Mapping[str, Any]) -> tuple[asyncio.StreamReader, int]:
        stage = "relay_connect"
        self.progress['stage'] = stage
        try:
            reader, writer = await self._open(host, self.relay_port)
            self._writer = writer
            stage = "relay_handshake"
            self.progress['stage'] = stage
            handle, session = int(login["handle"]), int(login["session"])
            writer.write(relay_request(self.device_id, self.domain, self.camera_port, handle, session, audio=self.audio))
            await writer.drain()
            response = await self._read(reader, 32)
            media_version = parse_relay_response(response)
            # SDK V2 expects 256 bytes of login response for v21+ devices.
            await self._read(reader, 224)
            writer.write(struct.pack("<II", 303, 0x3001).ljust(256, b"\x00"))
            await writer.drain()
            return reader, media_version
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise self._transport_error(err, stage) from err

    async def async_frames(self) -> AsyncIterator[Frame]:
        """Reauthenticate after temporary relay failures and resume at a keyframe."""
        failures = 0
        codec: str | None = None
        loop = asyncio.get_running_loop()
        while not self._close_event.is_set():
            session = self._async_session_frames()
            needs_keyframe = True
            healthy_since: float | None = None
            deadline = loop.time() + (self.startup_timeout if codec is None else self.video_timeout)
            try:
                while not self._close_event.is_set():
                    try:
                        async with asyncio.timeout_at(deadline):
                            frame = await anext(session)
                    except StopAsyncIteration:
                        return
                    except TimeoutError as err:
                        raise CloudError("V380 live video stopped arriving", stage=self.progress.get('stage', 'media_stream'),
                                         reason="network_timeout") from err
                    video = frame.codec in ("h264", "hevc", "h265")
                    if needs_keyframe:
                        if not video or not frame.keyframe:
                            continue
                        if codec is not None and frame.codec != codec:
                            raise UnsupportedError("The camera changed codec during reconnection",
                                                   stage="media_stream", reason="codec_changed")
                        codec, needs_keyframe = frame.codec, False
                    if video:
                        now = loop.time()
                        deadline = now + self.video_timeout
                        if healthy_since is None:
                            healthy_since = now
                        elif now - healthy_since >= self.retry_reset_after:
                            failures = 0
                    yield frame
                return
            except (AuthError, UnsupportedError):
                raise
            except CloudError as err:
                if self._close_event.is_set():
                    return
                if (err.diagnostic.get("detail") not in {
                        "network_timeout", "connection_failed", "incomplete_response", "dns_failed"}
                        or failures >= len(self.reconnect_delays)):
                    raise
                delay = self.reconnect_delays[failures]
                failures += 1
                self.reconnect_count += 1
                self.progress['transport_reconnects'] = self.reconnect_count
            finally:
                await session.aclose()
            try:
                await asyncio.wait_for(self._close_event.wait(), delay)
                return
            except TimeoutError:
                pass

    async def _async_session_frames(self) -> AsyncIterator[Frame]:
        """Yield frames from one authenticated relay session."""
        loop = asyncio.get_running_loop()
        cached = self.relay_state.cached(loop.time())
        relays = cached[:1]
        using_cache = bool(relays)
        attempted: set[str] = set()
        dispatched = False
        last_error: Exception | None = None
        attempts: list[dict[str, Any]] = []
        while True:
            if not relays:
                if dispatched:
                    break
                dispatched = True
                self.progress['stage'] = 'dispatcher_connect'
                try:
                    discovered = await self.async_relays()
                except CloudError as err:
                    last_error = err
                    relays = [host for host in self.relay_state.cached(loop.time()) if host not in attempted]
                    using_cache = True
                    if not relays:
                        raise
                else:
                    self.relay_state.remember(discovered, loop.time())
                    relays = [host for host in self.relay_state.order(discovered, loop.time()) if host not in attempted]
                    using_cache = False
                if not relays:
                    break
            host = relays.pop(0)
            attempted.add(host)
            self.relay_state.attempted[host] = loop.time()
            self.progress['relays_tried'] = min(self.progress['relays_tried'] + 1, 65535)
            self.progress['cached_relay_used'] = using_cache
            emitted = False
            video_started = False
            self.login_protocol_version = None
            stage = "device_login_connect"
            self.progress['stage'] = stage
            try:
                login = await self._login(host)
                login_version = int(login.get("version", 0))
                if 0 <= login_version <= 255:
                    self.login_protocol_version = login_version
                stage = "relay_connect"
                self.progress['stage'] = stage
                reader, media_version = await self._start_relay(host, login)
                writer = self._writer
                assert writer is not None
                handle, session = int(login["handle"]), int(login["session"])
                decoder = FrameDecoder(handle, int(login.get("version", 31)), media_version,
                                       fragment_limit=self.fragment_limit)
                stage = "media_stream"
                self.progress['stage'] = stage
                received_fragments = 0
                last_ack = asyncio.get_running_loop().time()
                while True:
                    header = await self._read(reader, 12)
                    self.progress['media_headers'] = min(self.progress['media_headers'] + 1, 65535)
                    marker = header[0]
                    if marker == 0x7F:
                        length = struct.unpack_from("<H", header, 7)[0]
                        if length > self.fragment_limit:
                            raise CloudError("The cloud relay sent an oversized fragment")
                        frame = decoder.feed(header, await self._read(reader, length))
                        received_fragments += 1
                        # Native V2 acknowledges each 30 fragments, command5005.
                        if self.acknowledge_fragments and received_fragments % 30 == 0:
                            now = asyncio.get_running_loop().time()
                            elapsed = int((now - last_ack) * 1000) & 0xFFFF
                            writer.write(struct.pack("<IHH", 5005, received_fragments & 0xFFFF, elapsed).ljust(16, b"\x00"))
                            await writer.drain()
                            last_ack = now
                        if frame is not None:
                            if frame.codec in ('h264', 'hevc', 'h265'):
                                self.progress['video_frames'] = min(self.progress['video_frames'] + 1, 65535)
                            if frame.keyframe and frame.codec in ("h264", "hevc", "h265"):
                                self._last_relay = host
                                video_started = True
                                self.progress['keyframes'] = min(self.progress['keyframes'] + 1, 65535)
                                self.relay_state.success(host, loop.time())
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
            except asyncio.CancelledError:
                # An outer startup deadline must advance the next attempt,
                # rather than restarting from the same slow first relay.
                if not video_started:
                    self.relay_state.failed[host] = loop.time()
                raise
            except AuthError:
                raise
            except UnsupportedError:
                raise
            except (CloudError, OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, TypeError) as err:
                self.relay_state.failed[host] = loop.time()
                detail = self._transport_error(err, stage)
                if self.login_protocol_version is not None:
                    detail.diagnostic["login_protocol_version"] = self.login_protocol_version
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
        self._close_event.set()
        if self._writer is not None:
            await self._close(self._writer)
            self._writer = None


def forwarded_login_request(device_id: int, domain: str, port: int, username: str,
                            password: str, random_key: bytes, *, padded: bool = False) -> bytes:
    """Binary cloud login (1167/1022), independently described by OpenV380.

    Reference: cimplesid/v380-mac, commit c3f5c5d17f4930fb90546be5d6c335c850975b86,
    Sources/V380/V380Client.swift authenticate(_:), encryptPassword(_:encoding:).
    This is a separate forwarding protocol, not a relaxed 1002 reply check.
    """
    domain_bytes, username_bytes = domain.encode("ascii"), username.encode("utf-8")
    password_bytes = password.encode("utf-8")
    if len(domain_bytes) > 49 or len(username_bytes) > 32 or len(random_key) != 16:
        raise CloudError("Invalid binary cloud login metadata",
                         stage="device_credentials", reason="invalid_metadata")
    if padded:
        encrypted = random_key + double_encrypt(password_bytes, random_key)[:64]
    else:
        if len(password_bytes) > 48:
            raise CloudError("The binary password requires padded encoding",
                             stage="device_credentials", reason="invalid_metadata")
        encrypted = random_key + _aes_ecb(_aes_ecb(password_bytes.ljust(48, b"\x00"), STATIC_KEY), random_key)
    packet = bytearray(520)
    struct.pack_into("<II", packet, 0, 1167, 1022)
    packet[8] = 31
    struct.pack_into("<II", packet, 9, 1, device_id)
    packet[17:17 + len(domain_bytes)] = domain_bytes
    struct.pack_into("<I", packet, 67, port)
    packet[71:71 + len(username_bytes)] = username_bytes
    packet[103:103 + len(encrypted)] = encrypted
    return bytes(packet)


class ForwardedCloudLiveClient(CloudLiveClient):
    """Binary 1022 cloud path; uses only dispatcher-provided relays.

    Account discovery and media cryptography are shared with the legacy client.
    The 401 reply body is opaque here: byte 12 is not a media-version field in
    this protocol. Media parameters, when supplied, select the decryption mode.
    """

    fragment_limit = 20000
    acknowledge_fragments = False
    reply_idle_timeout = 0.5

    async def _binary_login(self, host: str, username: str, *, padded: bool) -> dict[str, Any]:
        writer = None
        stage = "device_login_connect"
        self.progress['stage'] = stage
        try:
            reader, writer = await self._open(host, self.relay_port)
            stage = "device_login_response"
            self.progress['stage'] = stage
            writer.write(forwarded_login_request(self.device_id, self.domain, self.camera_port,
                username, self._password, self._random_key(), padded=padded))
            await writer.drain()
            command, result = struct.unpack("<ii", await self._read(reader, 8))
            if command != 1168:
                raise CloudError("The binary cloud login command is unsupported", stage=stage,
                                 reason="invalid_framing", code=command)
            if result != 1001:
                kind = AuthError if result in (1011, 1012, 1018) else CloudError
                raise kind("The cloud camera did not accept binary login", stage=stage,
                           reason="login_rejected", code=result)
            body = await self._read(reader, 15)  # Through deviceType/camType, offsets 21/22.
            response = bytes(8) + body
            version = response[12]
            if version < 21:
                raise UnsupportedError("This camera's old frame metadata is unsupported", stage=stage,
                                       reason="media_version_unsupported", protocol_version=version)
            handle, session = struct.unpack_from("<II", response, 13)
            return {"handle": handle, "session": session, "version": version}
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise self._transport_error(err, stage) from err
        finally:
            if writer is not None:
                await self._close(writer)

    async def _login(self, host: str) -> dict[str, Any]:
        # Vary only fields identified by explicit device credential errors.
        usernames = list(dict.fromkeys((self.username, str(self.device_id))))
        for username in usernames:
            encodings = (False, True) if len(self._password.encode("utf-8")) <= 48 else (True,)
            for padded in encodings:
                try:
                    return await self._binary_login(host, username, padded=padded)
                except AuthError as err:
                    code = err.diagnostic.get("code")
                    if code == 1011:
                        break
                    if code == 1012 and not padded:
                        continue
                    raise
            else:
                continue
            if username == usernames[-1]:
                raise AuthError("The cloud camera rejected its device username",
                                stage="device_login_response", reason="login_rejected", code=1011)
        raise CloudError("The cloud camera could not complete binary login",
                         stage="device_login_response", reason="login_rejected")

    async def _start_relay(self, host: str, login: Mapping[str, Any]) -> tuple[asyncio.StreamReader, int]:
        stage = "relay_connect"
        self.progress['stage'] = stage
        try:
            reader, writer = await self._open(host, self.relay_port)
            self._writer = writer
            stage = "relay_handshake"
            self.progress['stage'] = stage
            # Forwarding mode 1022 routes the camera's native stream protocol.
            packet = bytearray(256)
            struct.pack_into("<II", packet, 0, 301, 1022)
            domain = self.domain.encode("ascii")
            packet[8:8 + len(domain)] = domain
            struct.pack_into("<IIII", packet, 58, self.camera_port, self.device_id,
                             int(login["handle"]) & 0xFFFFFFFF, int(login["session"]) & 0xFFFFFFFF)
            packet[78] = 20
            struct.pack_into("<I", packet, 79, 1)
            writer.write(packet)
            await writer.drain()
            command, result = struct.unpack("<Ii", await self._read(reader, 8))
            if command != 401:
                raise CloudError("Invalid forwarded stream reply", stage=stage, reason="invalid_framing",
                                 relay_command=command, code=result)
            # Stream login has different result semantics from device login.
            # Actual media validation still follows the positive reply.
            if result not in (100, 402, 1001):
                raise CloudError("The forwarded stream session was rejected", stage=stage,
                                 reason="relay_rejected", relay_command=command, code=result)
            await self._read(reader, 18)  # Common minimum 401 header is 26 bytes.
            # Forwarded reply lengths vary (26/256/412). No media is requested
            # yet. Drain a bounded reply until quiet rather than imposing the
            # unrelated 1002 fixed 256-byte layout on this path.
            received = 26
            while True:
                try:
                    extra = await asyncio.wait_for(reader.read(1025 - received),
                                                   min(self.reply_idle_timeout, self.timeout))
                except TimeoutError:
                    break
                if not extra:
                    raise asyncio.IncompleteReadError(b"", 1)
                received += len(extra)
                if received > 1024:
                    raise CloudError("The forwarded stream reply exceeds its size limit",
                                     stage=stage, reason="invalid_framing")
            writer.write(struct.pack("<II", 303, 0x3001).ljust(256, b"\x00"))
            await writer.drain()
            # No relay media version was negotiated. The common selective-AES
            # mode applies until a real parameter packet selects version 21.
            return reader, 0
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise self._transport_error(err, stage) from err
