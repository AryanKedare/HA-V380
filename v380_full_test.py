#!/usr/bin/env python3
# Standalone V380 cloud / Home Assistant diagnostic. Python3.11+.
# Install: python3 -m pip install aiohttp cryptography av certifi
# Run: python3 v380_full_test.py
# Also check installed HA: python3 v380_full_test.py --ha-url http://homeassistant.local:8123
# The audited protocol modules below are embedded as readable source literals.
# They are loaded from this file only; no remote code is downloaded or executed.
# Source is the experimental integration created from the supplied V380 Pro APK.
"""Test V380 cloud login, live video decoding and recordings; optionally check Home Assistant.
Credentials are prompted locally. The report excludes account/camera IDs, secrets and URLs.
Requires Python3.11+ and aiohttp, cryptography, av, certifi.
"""
from __future__ import annotations

_EMBEDDED_PROTOCOL = {
    'errors': (
        '"""Public errors; messages deliberately exclude server responses and credentials."""\n'
        '\n'
        'class CloudError(Exception):\n'
        '    """V380 service request failed or returned an unsupported response."""\n'
        '\n'
        'class AuthError(CloudError):\n'
        '    """Account authentication expired or was rejected."""\n'
        '\n'
        'class UnsupportedError(CloudError):\n'
        '    """A protocol variant has not been implemented."""\n'
        '\n'
        '\n'
        'class LegacyHttpRequired(UnsupportedError):\n'
        '    """A vendor recording endpoint needs the explicit HTTP preference."""\n'
    ),
    'shared': (
        '"""Normalize data_share using the APK\'s DeviceManager.parseShareDevice.\n'
        '\n'
        'Inner legacy password is clear text after Base64/JSON decode.\n'
        "Encode it once to match account.device_password's ordinary ver=0 representation.\n"
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'import base64\n'
        'from collections.abc import Mapping\n'
        'import json\n'
        'from typing import Any\n'
        '\n'
        '\n'
        'def normalize_shared_device(item: Mapping[str, Any]) -> dict[str, Any]:\n'
        '    """Return owned-style metadata for a ShareNewsResponse$ShareNewBean row.\n'
        '\n'
        '    Raise ValueError for malformed shared credential records. The Android method\n'
        '    catches such parse failures and returns null; callers can skip malformed\n'
        '    shared rows without discarding otherwise valid owned devices.\n'
        '\n'
        '    No status filtering is introduced: the inspected device/list handler calls\n'
        '    parseShareDevice for every data_share row. Other sharing UI status handling\n'
        "    is outside this method's evidence boundary.\n"
        '    """\n'
        '    uid = item.get("device_id")\n'
        '    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:\n'
        '        raise ValueError("Shared camera has an invalid device ID")\n'
        '    encoded = item.get("str")\n'
        '    inner: dict[str, Any] = {}\n'
        '    if encoded:\n'
        '        if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:\n'
        '            raise ValueError("Shared camera credential encoding is unsupported")\n'
        '        try:\n'
        '            # Android Base64.DEFAULT accepts line-wrapped input.\n'
        '            compact = "".join(encoded.split())\n'
        '            data = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8")\n'
        '            inner = json.loads(data)\n'
        '        except (ValueError, UnicodeError) as err:\n'
        '            raise ValueError("Shared camera credential encoding is unsupported") from err\n'
        '        if not isinstance(inner, dict):\n'
        '            raise ValueError("Shared camera credential record is not an object")\n'
        '    username = inner.get("username", "admin")\n'
        '    password = inner.get("password", "")\n'
        '    nickname = item.get("nickname") or inner.get("nickname") or str(uid)\n'
        '    if not isinstance(username, str) or not isinstance(password, str) or not isinstance(nickname, str):\n'
        '        raise ValueError("Shared camera credential record contains invalid strings")\n'
        '    metadata = dict(item)\n'
        '    metadata.pop("str", None)\n'
        '    metadata["shared"] = True\n'
        '    metadata["nickname"] = nickname\n'
        '    metadata["device_port"] = 8800\n'
        '    metadata["ver"] = 0\n'
        '    if all(key in inner for key in ("public_key", "permission", "model")):\n'
        '        permission = inner["permission"]\n'
        '        if isinstance(permission, str):\n'
        '            try:\n'
        '                permission = int(permission)\n'
        '            except ValueError as err:\n'
        '                raise ValueError("Shared camera key-exchange permission is invalid") from err\n'
        '        if not isinstance(inner["public_key"], str) or not isinstance(inner["model"], str) or not isinstance(permission, int) or isinstance(permission, bool):\n'
        '            raise ValueError("Shared camera key-exchange record is invalid")\n'
        '        metadata.update(type=2, device_account="", device_password="", public_key=inner["public_key"], permission=permission, model=inner["model"])\n'
        '    else:\n'
        '        metadata.update(type=0, device_account=username, device_password=base64.b64encode(password.encode("utf-8")).decode("ascii"))\n'
        '    return metadata\n'
    ),
    'account': (
        '"""V380 account protocol recovered from classes10.dex.\n'
        '\n'
        'Sources: OkHttpUtil.accountLogin/updateUserDeviceList/renewUserToken,\n'
        'GlobalDefines.encrypt/md5, LoginJsonParse, UserDeivceListJsonParse.\n'
        'Only fixture-tested: no account was supplied during development.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'import asyncio\n'
        'import base64\n'
        'from dataclasses import dataclass, field\n'
        'import hashlib\n'
        'import json\n'
        'import time\n'
        'from typing import Any\n'
        '\n'
        'import aiohttp\n'
        'from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes\n'
        'from cryptography.hazmat.primitives.padding import PKCS7\n'
        '\n'
        'from .errors import AuthError, CloudError, UnsupportedError\n'
        'from .shared import normalize_shared_device\n'
        '\n'
        'API_BASE = "https://mapi.av380.net/"\n'
        'REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)\n'
        'MAX_JSON_BYTES = 8 * 1024 * 1024\n'
        '_APP_AES_KEY = b"1234567890123456"\n'
        '_APP_SIGN_SUFFIX = "hsshop2016"\n'
        '\n'
        '\n'
        'def md5(value: str) -> str:\n'
        '    """Match the app\'s lowercase UTF-8 MD5, used for protocol compatibility."""\n'
        '    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()\n'
        '\n'
        '\n'
        'def encode_account_password(password: str) -> str:\n'
        '    padder = PKCS7(128).padder()\n'
        '    padded = padder.update(password.encode("utf-8")) + padder.finalize()\n'
        '    encryptor = Cipher(algorithms.AES(_APP_AES_KEY), modes.ECB()).encryptor()\n'
        '    return (encryptor.update(padded) + encryptor.finalize()).hex()\n'
        '\n'
        '\n'
        'def login_request(username: str, password: str, timestamp: int) -> dict[str, Any]:\n'
        '    encoded = encode_account_password(password)\n'
        '    return {\n'
        '        "username": username,\n'
        '        "password": encoded,\n'
        '        "phonetoken": "100",\n'
        '        "sign": md5(f"password={encoded}&phonetoken=100&timestamp={timestamp}&username={username}{_APP_SIGN_SUFFIX}"),\n'
        '        "timestamp": timestamp,\n'
        '    }\n'
        '\n'
        '\n'
        'def devices_request(access_token: str, timestamp: int) -> dict[str, Any]:\n'
        '    # get_sub_server=10 asks for remote dispatcher metadata. Do not register a\n'
        '    # mobile push ID or change account/device configuration.\n'
        '    canonical = (\n'
        '        f"accesstoken={access_token}&from_app=V380Pro&get_sub_server=10"\n'
        '        f"&language=en&registrationid=&timestamp={timestamp}&type=all&ver=1008"\n'
        '    )\n'
        '    return {\n'
        '        "accesstoken": access_token,\n'
        '        "from_app": "V380Pro",\n'
        '        "get_sub_server": 10,\n'
        '        "language": "en",\n'
        '        "registrationid": "",\n'
        '        "sign": md5(canonical + _APP_SIGN_SUFFIX),\n'
        '        "timestamp": timestamp,\n'
        '        "type": "all",\n'
        '        "ver": 1008,\n'
        '    }\n'
        '\n'
        '\n'
        '@dataclass(frozen=True, slots=True)\n'
        'class CloudDevice:\n'
        '    device_id: str\n'
        '    name: str\n'
        '    online: bool | None = None  # device/list does not establish online status.\n'
        '    metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)\n'
        '\n'
        '\n'
        '@dataclass(frozen=True, slots=True)\n'
        'class CloudRecording:\n'
        '    recording_id: str\n'
        '    title: str\n'
        '    mime_type: str = "video/mp4"\n'
        '\n'
        '\n'
        'def device_password(metadata: dict[str, Any]) -> str:\n'
        '    """Decode legacy device credentials exactly as BaseActivity$55 does.\n'
        '\n'
        '    New-platform devices authenticate with key exchange; do not mistake their\n'
        '    encrypted rk/public_key values for a cleartext camera password.\n'
        '    """\n'
        '    if int(metadata.get("type", 0)) >= 2:\n'
        '        raise UnsupportedError("This camera requires the newer cloud key-exchange protocol")\n'
        '    encoded = metadata.get("device_password")\n'
        '    if not encoded:\n'
        '        return ""\n'
        '    try:\n'
        '        raw = base64.b64decode(encoded, validate=True)\n'
        '        if int(metadata.get("ver", 0)) == 2:\n'
        '            key = (str(metadata.get("rand_key", "")) + "hongshi1").encode()\n'
        '            if len(key) not in (16, 24, 32):\n'
        '                raise ValueError("Invalid key length")\n'
        '            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()\n'
        '            padded = decryptor.update(raw) + decryptor.finalize()\n'
        '            unpadder = PKCS7(128).unpadder()\n'
        '            raw = unpadder.update(padded) + unpadder.finalize()\n'
        '        return raw.decode("utf-8")\n'
        '    except (ValueError, TypeError, UnicodeError) as err:\n'
        '        raise CloudError("Camera credential encoding is unsupported") from err\n'
        '\n'
        '\n'
        'class AccountClient:\n'
        '    """Shared account session. The caller owns the aiohttp ClientSession."""\n'
        '\n'
        '    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:\n'
        '        self.session = session\n'
        '        self.username = username\n'
        '        self._password = password\n'
        '        self.access_token = ""\n'
        '        self.user_id = 0\n'
        '        self.account_id = ""\n'
        '        self.user_position = ""\n'
        '        self.billing_url = API_BASE\n'
        '        self.account_metadata: dict[str, Any] = {}\n'
        '        self.devices: dict[str, CloudDevice] = {}\n'
        '        self._auth_lock = asyncio.Lock()\n'
        '        self._login_at = 0.0\n'
        '        self._closed = False\n'
        '\n'
        '    async def _request_json(\n'
        '        self, url: str, payload: dict[str, Any] | None, *,\n'
        '        params: dict[str, Any] | None = None,\n'
        '    ) -> dict[str, Any]:\n'
        '        """Bounded requests; never include payloads/URLs with tokens in errors."""\n'
        '        if self._closed:\n'
        '            raise CloudError("Cloud session is closed")\n'
        '        try:\n'
        '            async with self.session.request(\n'
        '                "POST" if payload is not None else "GET", url,\n'
        '                data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") if payload is not None else None,\n'
        '                headers={"Content-Type": "application/json; charset=utf-8"},\n'
        '                params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False,\n'
        '            ) as response:\n'
        '                if response.status in (401, 403):\n'
        '                    raise AuthError("Cloud authorization was rejected")\n'
        '                if response.status != 200:\n'
        '                    raise CloudError(f"Cloud service returned HTTP {response.status}")\n'
        '                # Chunk iteration bounds memory even for chunked responses.\n'
        '                body = bytearray()\n'
        '                async for chunk in response.content.iter_chunked(65536):\n'
        '                    body.extend(chunk)\n'
        '                    if len(body) > MAX_JSON_BYTES:\n'
        '                        raise CloudError("Cloud response exceeds the size limit")\n'
        '            value = json.loads(body)\n'
        '            if not isinstance(value, dict):\n'
        '                raise CloudError("Cloud service returned an unexpected JSON document")\n'
        '            return value\n'
        '        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, UnicodeError) as err:\n'
        '            raise CloudError("Cannot communicate with the V380 cloud service") from None\n'
        '\n'
        '    @staticmethod\n'
        '    def _check_result(response: dict[str, Any], *, login: bool = False, allow_partial_devices: bool = False) -> None:\n'
        '        # These exact credential errors are mapped by LoginActivity.\n'
        '        result = response.get("result")\n'
        '        error = response.get("error_code", 0)\n'
        '        if result == 0 and error == 0:\n'
        '            return\n'
        '        if error == 21011:\n'
        '            raise AuthError("V380 account authorization has expired")\n'
        '        if login and error in (21005, 21006):\n'
        '            raise AuthError("V380 rejected the account username or password")\n'
        '        # Token failures require reauthentication; unknown vendor codes remain\n'
        '        # service errors, rather than repeatedly prompting for a password.\n'
        '        if result in (10005, 10007) and allow_partial_devices:\n'
        '            return  # app also processes the available owned/shared lists.\n'
        '        raise CloudError("V380 rejected the request (result %s, error %s)" % (\n'
        '            result if isinstance(result, int) else "missing",\n'
        '            error if isinstance(error, int) else "invalid",\n'
        '        ))\n'
        '\n'
        '    async def async_login(self) -> str:\n'
        '        async with self._auth_lock:\n'
        '            response = await self._request_json(API_BASE + "user/login", login_request(\n'
        '                self.username, self._password, int(time.time())\n'
        '            ))\n'
        '            self._check_result(response, login=True)\n'
        '            data = response.get("data")\n'
        '            if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):\n'
        '                raise CloudError("Cloud login did not return an access token")\n'
        '            uid = data.get("user_id")\n'
        '            if not isinstance(uid, int) or uid <= 0 or not data["access_token"]:\n'
        '                raise CloudError("Cloud login did not return a valid account identity")\n'
        '            if self.account_id and self.account_id != str(uid):\n'
        '                raise AuthError("The cloud session changed account identity")\n'
        '            self.user_id = uid\n'
        '            self.account_id = str(uid)\n'
        '            self.access_token = data["access_token"]\n'
        '            self.user_position = str(response.get("user_position") or "")\n'
        '            self.account_metadata = data\n'
        '            self._login_at = time.monotonic()\n'
        '            return self.account_id\n'
        '\n'
        '    async def _ensure_login(self) -> None:\n'
        '        if not self.access_token:\n'
        '            await self.async_login()\n'
        '\n'
        '    async def async_devices(self) -> list[CloudDevice]:\n'
        '        await self._ensure_login()\n'
        '        response = await self._request_json(API_BASE + "device/list", devices_request(\n'
        '            self.access_token, int(time.time())\n'
        '        ))\n'
        '        self._check_result(response, allow_partial_devices=True)\n'
        '        if response.get("renew_token") is True:\n'
        '            await self._renew_token()\n'
        '        rows = response.get("data")\n'
        '        shared = response.get("data_share")\n'
        '        if rows is None:\n'
        '            rows = []\n'
        '        if shared is None:\n'
        '            shared = []\n'
        '        if not isinstance(rows, list) or not isinstance(shared, list):\n'
        '            raise CloudError("Cloud device list has an unsupported shape")\n'
        '        normalized_shared = []\n'
        '        for item in shared:\n'
        '            try:\n'
        '                normalized_shared.append(normalize_shared_device(item))\n'
        '            except (ValueError, TypeError, AttributeError):\n'
        '                # DeviceManager.parseShareDevice also drops malformed shares.\n'
        '                continue\n'
        '        devices: dict[str, CloudDevice] = {}\n'
        '        for item in rows + normalized_shared:\n'
        '            if not isinstance(item, dict):\n'
        '                raise CloudError("Cloud device list contains an invalid device")\n'
        '            uid = item.get("device_id")\n'
        '            if not isinstance(uid, int) or uid <= 0:\n'
        '                raise CloudError("Cloud device list contains an invalid device ID")\n'
        '            device = CloudDevice(str(uid), str(item.get("nickname") or f"V380 {uid}"), metadata=item)\n'
        '            devices.setdefault(device.device_id, device)\n'
        '        self.devices = devices\n'
        '        return list(devices.values())\n'
        '\n'
        '    async def _renew_token(self) -> None:\n'
        '        async with self._auth_lock:\n'
        '            timestamp = int(time.time())\n'
        '            response = await self._request_json(API_BASE + "user/get-new-token", {\n'
        '                "accesstoken": self.access_token,\n'
        '                "timestamp": timestamp,\n'
        '                "sign": md5(f"accesstoken={self.access_token}&timestamp={timestamp}{_APP_SIGN_SUFFIX}"),\n'
        '            })\n'
        '            self._check_result(response)\n'
        '            token = response.get("access_token")\n'
        '            if not isinstance(token, str) or not token:\n'
        '                raise CloudError("Cloud token renewal has an unsupported response")\n'
        '            self.access_token = token\n'
        '\n'
        '    async def async_close(self) -> None:\n'
        '        self._closed = True\n'
        '        self.access_token = ""\n'
        '        self.devices.clear()\n'
        '        self.account_metadata.clear()\n'
        '        self._password = ""\n'
    ),
    'live': (
        '"""Portable legacy V380 cloud relay transport, recovered from the supplied APK.\n'
        '\n'
        'Static sources: classes9.dex DispatchServerGetter / LoginHelper / Functions;\n'
        'libhsMediaLibrary.so HSLiveDataV2Transmitter and libaes.so media_data_decrypt.\n'
        'The newer MQTT/key-exchange platform is deliberately reported unsupported.\n'
        'No camera, LAN discovery or Android native library is required.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'import asyncio\n'
        'import base64\n'
        'from collections.abc import AsyncIterator, Mapping\n'
        'from contextlib import suppress\n'
        'from dataclasses import dataclass\n'
        'import json\n'
        'import secrets\n'
        'import socket\n'
        'import struct\n'
        'from typing import Any\n'
        '\n'
        'from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes\n'
        'from cryptography.hazmat.primitives.padding import PKCS7\n'
        '\n'
        'from .errors import AuthError, CloudError, UnsupportedError\n'
        '\n'
        'STATIC_KEY = b"macrovideo+*#!^@"\n'
        'DEFAULT_DISPATCH_SERVER = "dispa1.av380.net"\n'
        'RPC_MAGIC = 0xFE000300\n'
        'MAX_JSON = 1024 * 1024\n'
        'MAX_FRAME = 8 * 1024 * 1024\n'
        '\n'
        '\n'
        'def _aes_ecb(data: bytes, key: bytes, *, decrypt: bool = False) -> bytes:\n'
        '    context = Cipher(algorithms.AES(key), modes.ECB())\n'
        '    worker = context.decryptor() if decrypt else context.encryptor()\n'
        '    return worker.update(data) + worker.finalize()\n'
        '\n'
        '\n'
        'def _padded_encrypt(data: bytes, key: bytes) -> bytes:\n'
        '    padder = PKCS7(128).padder()\n'
        '    return _aes_ecb(padder.update(data) + padder.finalize(), key)\n'
        '\n'
        '\n'
        'def double_encrypt(data: bytes, random_key: bytes) -> bytes:\n'
        '    """Match Functions.encrypt(Functions.encrypt(data, static), random)."""\n'
        '    return _padded_encrypt(_padded_encrypt(data, STATIC_KEY), random_key)\n'
        '\n'
        '\n'
        'def media_key(handle: int) -> bytes:\n'
        '    """HSLiveDataV2Transmitter::updateAesKey, VA 0x455d0."""\n'
        '    return struct.pack("<IQI", handle & 0xFFFFFFFF, 0x618123462C14795C, 0x82800DF0)\n'
        '\n'
        '\n'
        'def decrypt_media(data: bytes, key: bytes, *, audio: bool, first_2k: bool = False) -> bytes:\n'
        '    """Apply the native block selection without treating the tail as padding."""\n'
        '    result = bytearray(data)\n'
        '    if audio or first_2k:\n'
        '        count = len(data) // 16 * 16\n'
        '        if first_2k and not audio:\n'
        '            count = min(count, 2048)\n'
        '        if count:\n'
        '            result[:count] = _aes_ecb(data[:count], key, decrypt=True)\n'
        '    else:\n'
        '        # libaes media_data_decrypt: 64 encrypted bytes per 80 byte group.\n'
        '        for offset in range(0, len(data) - 63, 80):\n'
        '            result[offset:offset + 64] = _aes_ecb(data[offset:offset + 64], key, decrypt=True)\n'
        '    return bytes(result)\n'
        '\n'
        '\n'
        'def dispatch_request(device_id: int, zone: str, random_key: bytes) -> bytes:\n'
        '    encrypted_zone = double_encrypt(zone.encode("utf-8"), random_key)\n'
        '    encrypted_device = double_encrypt(struct.pack("<I", device_id) + random_key, random_key)\n'
        '    packet = (\n'
        '        struct.pack("<HH", 100, len(encrypted_zone)) + encrypted_zone\n'
        '        + struct.pack("<H", len(encrypted_device)) + encrypted_device + random_key\n'
        '    )\n'
        '    if len(packet) > 144:\n'
        '        raise CloudError("The cloud routing zone is too long")\n'
        '    return packet.ljust(144, b"\\x00")\n'
        '\n'
        '\n'
        'def parse_dispatch(data: bytes) -> list[str]:\n'
        '    if len(data) != 64 or struct.unpack_from("<H", data)[0] != 200:\n'
        '        raise CloudError("The cloud dispatcher rejected the routing request")\n'
        '    normal, pano = data[6], data[7]\n'
        '    if normal + pano > 14 or not normal + pano:\n'
        '        raise CloudError("The cloud dispatcher returned no usable relays")\n'
        '    # Normal servers are first; panoramic devices can use the second group.\n'
        '    return [socket.inet_ntoa(data[8 + index * 4:12 + index * 4]) for index in range(normal + pano)]\n'
        '\n'
        '\n'
        'def relay_request(device_id: int, domain: str, port: int, handle: int, session: int, *, audio: bool) -> bytes:\n'
        '    """Native command 301, recovered at VA 0x47c14 (packed, unaligned)."""\n'
        '    domain_bytes = domain.encode("ascii")\n'
        '    if len(domain_bytes) > 49:\n'
        '        raise CloudError("The camera cloud domain is too long")\n'
        '    packet = bytearray(256)\n'
        '    struct.pack_into("<II", packet, 0, 301, 1002)\n'
        '    packet[8:8 + len(domain_bytes)] = domain_bytes\n'
        '    struct.pack_into("<IIII", packet, 58, port, device_id, handle & 0xFFFFFFFF, session & 0xFFFFFFFF)\n'
        '    struct.pack_into("<I", packet, 74, 0)  # default stream / resolution\n'
        '    packet[78] = 20  # requested frame rate\n'
        '    struct.pack_into("<I", packet, 79, 4097 if audio else 4096)\n'
        '    struct.pack_into("<I", packet, 83, 0)\n'
        '    struct.pack_into("<H", packet, 88, 0x0101)\n'
        '    packet[90] = 1\n'
        '    return bytes(packet)\n'
        '\n'
        '\n'
        '@dataclass(frozen=True, slots=True)\n'
        'class Frame:\n'
        '    payload: bytes\n'
        '    codec: str\n'
        '    timestamp: int\n'
        '    keyframe: bool = False\n'
        '    sequence: int = 0\n'
        '\n'
        '\n'
        'class FrameDecoder:\n'
        '    """Reassemble bounded packet fragments, then decrypt the complete frame."""\n'
        '\n'
        '    def __init__(self, handle: int, communication_version: int, media_version: int) -> None:\n'
        '        self.key = media_key(handle)\n'
        '        self.communication_version = communication_version\n'
        '        self.media_version = media_version\n'
        '        self._parts: list[bytes] = []\n'
        '        self._total = 0\n'
        '        self._next = 0\n'
        '        self._size = 0\n'
        '\n'
        '    def feed(self, header: bytes, payload: bytes) -> Frame | None:\n'
        '        if len(header) != 12 or header[0] != 0x7F:\n'
        '            raise CloudError("Invalid cloud media packet")\n'
        '        total, current, length = struct.unpack_from("<HHH", header, 3)\n'
        '        if not total or current >= total or length != len(payload) or length > 8000:\n'
        '            raise CloudError("Invalid cloud media fragment bounds")\n'
        '        if current == 0:\n'
        '            self._parts = []\n'
        '            self._total, self._next, self._size = total, 0, 0\n'
        '        if total != self._total or current != self._next:\n'
        '            self._parts = []\n'
        '            self._total = self._next = self._size = 0\n'
        '            return None\n'
        '        self._parts.append(payload)\n'
        '        self._next += 1\n'
        '        self._size += len(payload)\n'
        '        if self._size > MAX_FRAME:\n'
        '            raise CloudError("Cloud media frame exceeds the size limit")\n'
        '        if self._next != self._total:\n'
        '            return None\n'
        '        data = b"".join(self._parts)\n'
        '        self._parts = []\n'
        '        self._total = self._next = self._size = 0\n'
        '        if len(data) < 16:\n'
        '            raise CloudError("Cloud media frame metadata is incomplete")\n'
        '        sequence, frame_type, _channel, timestamp = struct.unpack_from("<IHHQ", data)\n'
        '        payload = data[16:]\n'
        '        codec = {0: "h264", 1: "h264", 40: "hevc", 41: "hevc", 50: "h264", 51: "h264", 52: "hevc", 53: "hevc", 24: "aac"}.get(frame_type)\n'
        '        if codec is None:\n'
        '            # Other values include ADPCM, telemetry and camera control notices.\n'
        '            return None\n'
        '        if self.communication_version >= 31:\n'
        '            payload = decrypt_media(payload, self.key, audio=codec == "aac", first_2k=self.media_version == 21)\n'
        '        keyframe = frame_type in (0, 40, 50, 52)\n'
        '        if keyframe and not payload.startswith((b"\\x00\\x00\\x01", b"\\x00\\x00\\x00\\x01")):\n'
        '            raise CloudError("The cloud video key did not produce a valid Annex B frame")\n'
        '        return Frame(payload, codec, timestamp, keyframe, sequence)\n'
        '\n'
        '\n'
        'class CloudLiveClient:\n'
        '    """A one-viewer cloud session; the caller consumes and closes async_frames."""\n'
        '\n'
        '    def __init__(\n'
        '        self,\n'
        '        metadata: Mapping[str, Any],\n'
        '        device_password: str = "",\n'
        '        *,\n'
        '        zone: str = "",\n'
        '        dispatch_server: str | None = None,\n'
        '        dispatch_port: int = 8000,\n'
        '        login_port: int = 8089,\n'
        '        relay_port: int = 8800,\n'
        '        timeout: float = 15,\n'
        '        audio: bool = True,\n'
        '    ) -> None:\n'
        '        self.metadata = dict(metadata)\n'
        '        self.device_id = int(metadata.get("device_id", metadata.get("id", 0)))\n'
        '        if not 0 < self.device_id < 2**32:\n'
        '            raise CloudError("A valid numeric V380 camera ID is required")\n'
        '        if int(metadata.get("type", 0)) >= 2:\n'
        '            raise UnsupportedError("This camera uses the newer cloud key-exchange video protocol")\n'
        '        self._password = device_password\n'
        '        self.username = str(metadata.get("device_account") or "admin")\n'
        '        self.domain = f"{self.device_id}.nvdvr.net"\n'
        '        self.camera_port = int(metadata.get("device_port") or 8800)\n'
        '        self.zone = zone\n'
        '        sub_server = metadata.get("sub_server")\n'
        '        self.dispatch_server = dispatch_server or (sub_server if isinstance(sub_server, str) and sub_server else DEFAULT_DISPATCH_SERVER)\n'
        '        self.dispatch_port = dispatch_port\n'
        '        self.login_port = login_port\n'
        '        self.relay_port = relay_port\n'
        '        self.timeout = timeout\n'
        '        self.audio = audio\n'
        '        self._writer: asyncio.StreamWriter | None = None\n'
        '\n'
        '    async def _open(self, host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:\n'
        '        return await asyncio.wait_for(asyncio.open_connection(host, port), self.timeout)\n'
        '\n'
        '    async def _read(self, reader: asyncio.StreamReader, length: int) -> bytes:\n'
        '        return await asyncio.wait_for(reader.readexactly(length), self.timeout)\n'
        '\n'
        '    @staticmethod\n'
        '    async def _close(writer: asyncio.StreamWriter) -> None:\n'
        '        writer.close()\n'
        '        with suppress(OSError, asyncio.CancelledError):\n'
        '            await writer.wait_closed()\n'
        '\n'
        '    async def async_relays(self) -> list[str]:\n'
        '        """Ask the app\'s dispatcher for cloud relays; never probe the LAN."""\n'
        '        writer = None\n'
        '        try:\n'
        '            reader, writer = await self._open(self.dispatch_server, self.dispatch_port)\n'
        '            random_key = self._random_key()\n'
        '            writer.write(dispatch_request(self.device_id, self.zone, random_key))\n'
        '            await writer.drain()\n'
        '            return parse_dispatch(await self._read(reader, 64))\n'
        '        except (OSError, TimeoutError, asyncio.IncompleteReadError) as err:\n'
        '            raise CloudError("The V380 cloud dispatcher could not be reached") from err\n'
        '        finally:\n'
        '            if writer is not None:\n'
        '                await self._close(writer)\n'
        '\n'
        '    @staticmethod\n'
        '    def _random_key() -> bytes:\n'
        '        alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"\n'
        '        return "".join(secrets.choice(alphabet) for _ in range(16)).encode("ascii")\n'
        '\n'
        '    async def _login(self, host: str) -> dict[str, Any]:\n'
        '        reader, writer = await self._open(host, self.login_port)\n'
        '        try:\n'
        '            random_key = self._random_key()\n'
        '            request_id = secrets.randbelow(99999999) + 100\n'
        '            payload = {\n'
        '                "id": request_id,\n'
        '                "method": "login",\n'
        '                "params": {\n'
        '                    "version": 31,\n'
        '                    "phoneType": 1012,\n'
        '                    "deviceId": self.device_id,\n'
        '                    "domain": self.domain,\n'
        '                    "port": self.camera_port,\n'
        '                    "accountId": 11,\n'
        '                    "username": self.username,\n'
        '                    "password": base64.b64encode(double_encrypt(self._password.encode("utf-8"), random_key)[:112]).decode("ascii"),\n'
        '                    "randomKey": random_key.decode("ascii"),\n'
        '                    "connectType": 0,\n'
        '                    "securityLevel": 1,\n'
        '                    "agora": 0,\n'
        '                    "ectx": 0,\n'
        '                    "p2pIdx": int(self.metadata.get("p_id") or 0),\n'
        '                },\n'
        '            }\n'
        '            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")\n'
        '            writer.write(struct.pack("<II", RPC_MAGIC, len(data)) + data)\n'
        '            await writer.drain()\n'
        '            header = await self._read(reader, 8)\n'
        '            magic, length = struct.unpack("<II", header)\n'
        '            if magic != RPC_MAGIC or not 0 < length <= MAX_JSON:\n'
        '                raise CloudError("Invalid cloud login response framing")\n'
        '            try:\n'
        '                response = json.loads(await self._read(reader, length))\n'
        '            except (ValueError, UnicodeError) as err:\n'
        '                raise CloudError("Invalid cloud login response") from err\n'
        '            if not isinstance(response, dict) or response.get("id") != request_id:\n'
        '                raise CloudError("The cloud login response does not match this request")\n'
        '            result = response.get("result") or {}\n'
        '            if not isinstance(result, dict):\n'
        '                raise CloudError("The cloud login result is invalid")\n'
        '            if result.get("code") != 1001:\n'
        '                if result.get("code") in (1011, 1012):\n'
        '                    raise AuthError("The cloud camera rejected its device credentials")\n'
        '                raise CloudError("The camera cloud login failed")\n'
        '            info = response.get("v380")\n'
        '            if not isinstance(info, dict) or "handle" not in info or "session" not in info:\n'
        '                raise CloudError("The cloud login did not return a video session")\n'
        '            if int(info.get("version", 0)) < 21:\n'
        '                raise UnsupportedError("This camera\'s old media framing is not supported")\n'
        '            return info\n'
        '        finally:\n'
        '            await self._close(writer)\n'
        '\n'
        '    async def async_frames(self) -> AsyncIterator[Frame]:\n'
        '        """Yield decrypted compressed frames over a source-backed cloud relay."""\n'
        '        relays = await self.async_relays()\n'
        '        last_error: Exception | None = None\n'
        '        for host in relays:\n'
        '            emitted = False\n'
        '            try:\n'
        '                login = await self._login(host)\n'
        '                reader, writer = await self._open(host, self.relay_port)\n'
        '                self._writer = writer\n'
        '                handle, session = int(login["handle"]), int(login["session"])\n'
        '                writer.write(relay_request(self.device_id, self.domain, self.camera_port, handle, session, audio=self.audio))\n'
        '                await writer.drain()\n'
        '                response = await self._read(reader, 32)\n'
        '                command, result, _status = struct.unpack_from("<III", response)\n'
        '                media_version = response[12]\n'
        '                if command != 401 or result != 1001:\n'
        '                    raise CloudError("The cloud relay rejected the video session")\n'
        '                if media_version < 21:\n'
        '                    raise UnsupportedError("This camera\'s old media framing is not supported")\n'
        '                # SDK V2 expects 256 bytes of login response for v21+ devices.\n'
        '                await self._read(reader, 224)\n'
        '                start = struct.pack("<II", 303, 0x3001).ljust(256, b"\\x00")\n'
        '                writer.write(start)\n'
        '                await writer.drain()\n'
        '                decoder = FrameDecoder(handle, int(login.get("version", 31)), media_version)\n'
        '                received_fragments = 0\n'
        '                last_ack = asyncio.get_running_loop().time()\n'
        '                while True:\n'
        '                    header = await self._read(reader, 12)\n'
        '                    marker = header[0]\n'
        '                    if marker == 0x7F:\n'
        '                        length = struct.unpack_from("<H", header, 7)[0]\n'
        '                        if length > 8000:\n'
        '                            raise CloudError("The cloud relay sent an oversized fragment")\n'
        '                        frame = decoder.feed(header, await self._read(reader, length))\n'
        '                        received_fragments += 1\n'
        '                        # Native V2 acknowledges each 30 fragments, command5005.\n'
        '                        if received_fragments % 30 == 0:\n'
        '                            now = asyncio.get_running_loop().time()\n'
        '                            elapsed = int((now - last_ack) * 1000) & 0xFFFF\n'
        '                            writer.write(struct.pack("<IHH", 5005, received_fragments & 0xFFFF, elapsed).ljust(16, b"\\x00"))\n'
        '                            await writer.drain()\n'
        '                            last_ack = now\n'
        '                        if frame is not None:\n'
        '                            emitted = True\n'
        '                            yield frame\n'
        '                    elif marker == 0x2F:\n'
        '                        length = struct.unpack_from("<H", header, 2)[0]\n'
        '                        await self._read(reader, length)\n'
        '                        if header[1] < 20:\n'
        '                            raise UnsupportedError("This camera selected an old media frame format")\n'
        '                        decoder.media_version = header[1]\n'
        '                    elif marker == 0x1F:\n'
        '                        if header[5] < 20:\n'
        '                            raise UnsupportedError("This camera selected an old media frame format")\n'
        '                        decoder.media_version = header[5]\n'
        '                    elif marker not in (0x3F, 0x4F, 0x5F, 0x6F):\n'
        '                        raise UnsupportedError("The cloud relay sent an unsupported media packet")\n'
        '            except AuthError:\n'
        '                raise\n'
        '            except UnsupportedError:\n'
        '                raise\n'
        '            except (CloudError, OSError, TimeoutError, asyncio.IncompleteReadError, ValueError, TypeError) as err:\n'
        '                if emitted:\n'
        '                    raise CloudError("The cloud video session ended") from err\n'
        '                last_error = err\n'
        '            finally:\n'
        '                if self._writer is not None:\n'
        '                    await self._close(self._writer)\n'
        '                    self._writer = None\n'
        '        raise CloudError("No V380 cloud relay could start this camera") from last_error\n'
        '\n'
        '    async def async_close(self) -> None:\n'
        '        if self._writer is not None:\n'
        '            await self._close(self._writer)\n'
        '            self._writer = None\n'
    ),
    'mpegts': (
        '"""Minimal Annex-B H.264/H.265 to MPEG-TS packetizer (video only).\n'
        '\n'
        'No transcoding or decoder dependency. Each push is one camera access unit,\n'
        'with presentation time in seconds. Browser codec support is handled by HA.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'VIDEO_PID = 0x100\n'
        'PMT_PID = 0x1000\n'
        '\n'
        '\n'
        'def _crc32_mpeg(data: bytes) -> bytes:\n'
        '    crc = 0xFFFFFFFF\n'
        '    for value in data:\n'
        '        crc ^= value << 24\n'
        '        for _ in range(8):\n'
        '            crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF\n'
        '    return crc.to_bytes(4, "big")\n'
        '\n'
        '\n'
        'def _pts(value: int) -> bytes:\n'
        '    value &= (1 << 33) - 1\n'
        '    return bytes((\n'
        '        0x21 | (((value >> 30) & 7) << 1),\n'
        '        (value >> 22) & 255,\n'
        '        (((value >> 15) & 127) << 1) | 1,\n'
        '        (value >> 7) & 255,\n'
        '        ((value & 127) << 1) | 1,\n'
        '    ))\n'
        '\n'
        '\n'
        'class MpegTsMuxer:\n'
        '    """Create PAT/PMT and PES packets with independent continuity counters."""\n'
        '\n'
        '    def __init__(self, codec: str) -> None:\n'
        '        if codec not in ("h264", "h265", "hevc"):\n'
        '            raise ValueError("Unsupported compressed video codec")\n'
        '        self.codec = codec\n'
        '        self._counters: dict[int, int] = {}\n'
        '        self._frames = 0\n'
        '        self._last_pts = -1\n'
        '        self._origin: float | None = None\n'
        '\n'
        '    def _packets(self, pid: int, payload: bytes, *, pcr: int | None = None) -> bytes:\n'
        '        output = bytearray()\n'
        '        position = 0\n'
        '        first = True\n'
        '        while position < len(payload):\n'
        '            cc = self._counters.get(pid, 0)\n'
        '            self._counters[pid] = (cc + 1) % 16\n'
        '            header = bytes((0x47, (0x40 if first else 0) | (pid >> 8), pid & 255))\n'
        '            # An adaptation field carrying PCR needs 8 bytes, leaving176.\n'
        '            max_payload = 176 if first and pcr is not None else 184\n'
        '            part = payload[position:position + max_payload]\n'
        '            spare = 184 - len(part)\n'
        '            if spare:\n'
        '                adaptation = bytearray((spare - 1,))\n'
        '                if spare > 1:\n'
        '                    has_pcr = first and pcr is not None\n'
        '                    adaptation.append(0x10 if has_pcr else 0)\n'
        '                    if has_pcr:\n'
        '                        base = pcr & ((1 << 33) - 1)\n'
        '                        adaptation.extend(bytes((\n'
        '                            (base >> 25) & 255, (base >> 17) & 255,\n'
        '                            (base >> 9) & 255, (base >> 1) & 255,\n'
        '                            ((base & 1) << 7) | 0x7E, 0,\n'
        '                        )))\n'
        '                    adaptation.extend(b"\\xff" * (spare - len(adaptation)))\n'
        '                output.extend(header + bytes((0x30 | cc,)) + adaptation + part)\n'
        '            else:\n'
        '                output.extend(header + bytes((0x10 | cc,)) + part)\n'
        '            first = False\n'
        '            position += len(part)\n'
        '        return bytes(output)\n'
        '\n'
        '    def tables(self) -> bytes:\n'
        '        pat = bytes.fromhex("00b00d0001c100000001f000")\n'
        '        pat += _crc32_mpeg(pat)\n'
        '        stream_type = 0x1B if self.codec == "h264" else 0x24\n'
        '        pmt = bytes.fromhex("02b0120001c10000e100f000") + bytes((stream_type, 0xE1, 0, 0xF0, 0))\n'
        '        pmt += _crc32_mpeg(pmt)\n'
        '        return self._packets(0, b"\\x00" + pat) + self._packets(PMT_PID, b"\\x00" + pmt)\n'
        '\n'
        '    def push(self, data: bytes, timestamp: float | None = None, *, keyframe: bool = False) -> bytes:\n'
        '        if not data:\n'
        '            return b""\n'
        '        if not data.startswith((b"\\x00\\x00\\x01", b"\\x00\\x00\\x00\\x01")):\n'
        '            raise ValueError("Video payload is not an Annex-B access unit")\n'
        '        if timestamp is None:\n'
        '            pts = max(0, self._last_pts + 3600)  # fallback25fps until clock known\n'
        '        else:\n'
        '            if self._origin is None:\n'
        '                self._origin = timestamp\n'
        '            pts = max(self._last_pts + 1, int((timestamp - self._origin) * 90000))\n'
        '        self._last_pts = pts\n'
        '        tables = self.tables() if self._frames % 50 == 0 or keyframe else b""\n'
        '        self._frames += 1\n'
        '        pes = b"\\x00\\x00\\x01\\xe0\\x00\\x00\\x80\\x80\\x05" + _pts(pts) + data\n'
        '        return tables + self._packets(VIDEO_PID, pes, pcr=pts)\n'
    ),
    'recordings': (
        '"""V380 UCloud and S3 recording protocols recovered from the supplied APK.\n'
        '\n'
        'Cloud URLs and temporary credentials stay on the server. RecordingSource must\n'
        'only be consumed by the authenticated Home Assistant media proxy, never exposed\n'
        'directly to a browser or media player. See research/RECORDINGS_PROTOCOL.md.\n'
        '"""\n'
        'from __future__ import annotations\n'
        '\n'
        'import asyncio\n'
        'import base64\n'
        'from dataclasses import dataclass, field\n'
        'from datetime import date as Date, datetime, timezone\n'
        'import hashlib\n'
        'import hmac\n'
        'import ipaddress\n'
        'import json\n'
        'import re\n'
        'import time\n'
        'from typing import Any\n'
        'from urllib.parse import quote, urlencode, urlsplit, urlunsplit\n'
        '\n'
        'from .account import AccountClient, CloudRecording, md5\n'
        'from .errors import CloudError, LegacyHttpRequired, UnsupportedError\n'
        '\n'
        'S3_SEARCH_BASE = "http://hscsearch.av380.net:8080"\n'
        'HLS_MIME = "application/vnd.apple.mpegurl"\n'
        '_MAX_PAGES = 100\n'
        '_MAX_RECORDINGS = 10000\n'
        '\n'
        '\n'
        'def _compact(value: Any) -> str:\n'
        '    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))\n'
        '\n'
        '\n'
        'def _int(value: Any, label: str) -> int:\n'
        '    if isinstance(value, bool) or not isinstance(value, int):\n'
        '        raise CloudError(f"Cloud recording metadata has an invalid {label}")\n'
        '    return value\n'
        '\n'
        '\n'
        'def _day(value: Any) -> str:\n'
        '    if not isinstance(value, str):\n'
        '        raise CloudError("Cloud recording metadata has an invalid date")\n'
        '    if re.fullmatch(r"\\d{8}", value):\n'
        '        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"\n'
        '    try:\n'
        '        parsed = Date.fromisoformat(value)\n'
        '    except ValueError as err:\n'
        '        raise CloudError("Cloud recording metadata has an invalid date") from err\n'
        '    if parsed.isoformat() != value:\n'
        '        raise CloudError("Cloud recording metadata has an invalid date")\n'
        '    return value\n'
        '\n'
        '\n'
        'def _mime(filename: str) -> str:\n'
        '    extension = filename.rsplit(".", 1)[-1].lower()\n'
        '    return {"mp4": "video/mp4", "m4v": "video/mp4", "ts": "video/mp2t",\n'
        '            "m3u8": HLS_MIME}.get(extension, "application/octet-stream")\n'
        '\n'
        '\n'
        'def service_request(token: str, user_id: int, device_id: int, position: str,\n'
        '                    timestamp: int, version: int = 1008) -> dict[str, Any]:\n'
        '    canonical = (f"accesstoken={token}&deviceid={device_id}&timestamp={timestamp}"\n'
        '                 f"&user_id={user_id}&user_position={position}&ver={version}")\n'
        '    return {"accesstoken": token, "deviceid": device_id, "timestamp": timestamp,\n'
        '            "user_id": user_id, "user_position": position, "ver": version,\n'
        '            "sign": md5(canonical + "hsshop2016")}\n'
        '\n'
        '\n'
        'def s3_list_request(token: str, user_id: int, device_id: int,\n'
        '                    buckets: list[int], timestamp: int, *, day: str | None = None,\n'
        '                    cursor: int = 1, page_size: int = 365) -> dict[str, Any]:\n'
        '    firms = [{"bucket_index": value} for value in dict.fromkeys(buckets)]\n'
        '    if day is None:\n'
        '        kind = "date"\n'
        '        canonical = (f"access_token={token}&cursor={cursor}&device_id={device_id}"\n'
        '                     f"&firm_list={_compact(firms)}&page_size={page_size}"\n'
        '                     f"&timestamp={timestamp}&type=date&user_id={user_id}")\n'
        '    else:\n'
        '        kind = "list"\n'
        '        day = _day(day).replace("-", "")\n'
        '        canonical = (f"access_token={token}&date={day}&device_id={device_id}"\n'
        '                     f"&firm_list={_compact(firms)}&timestamp={timestamp}"\n'
        '                     f"&type=list&user_id={user_id}")\n'
        '    payload = {"access_token": token, "user_id": user_id, "device_id": device_id,\n'
        '               "type": kind}\n'
        '    if day is None:\n'
        '        payload.update(cursor=cursor, page_size=page_size)\n'
        '    else:\n'
        '        payload["date"] = day\n'
        '    payload.update(firm_list=firms, timestamp=timestamp,\n'
        '                   sign=md5(canonical + "hongshi2025s3"))\n'
        '    return payload\n'
        '\n'
        '\n'
        'def ucloud_download_parameter(token: str, row: dict[str, Any]) -> str:\n'
        '    """Match HttpDownloader.downloadUCloudRecordFile, including string device ID."""\n'
        '    payload = {"access_token": token, "user_id": _int(row.get("user_id"), "user ID"),\n'
        '               "device_id": str(_int(row.get("device_id"), "device ID")),\n'
        '               "channel": _int(row.get("channel", 0), "channel"),\n'
        '               "date": _int(row.get("date"), "date"),\n'
        '               "time": _int(row.get("time"), "time"),\n'
        '               "rand_num": _int(row.get("rand_num", 0), "random number")}\n'
        '    return base64.b64encode(_compact(payload).encode()).decode("ascii")\n'
        '\n'
        '\n'
        '@dataclass(frozen=True, slots=True)\n'
        'class RecordingSegment:\n'
        '    url: str = field(repr=False)\n'
        '    duration: float\n'
        '    mime_type: str = "video/mp2t"\n'
        '\n'
        '\n'
        '@dataclass(frozen=True, slots=True)\n'
        'class RecordingSource:\n'
        '    """Private, short-lived backend source for the HA proxy."""\n'
        '    mime_type: str\n'
        '    filename: str\n'
        '    url: str | None = field(default=None, repr=False)\n'
        '    segments: tuple[RecordingSegment, ...] = field(default=(), repr=False)\n'
        '\n'
        '\n'
        '@dataclass(slots=True)\n'
        'class _Service:\n'
        '    device_id: str\n'
        '    metadata: dict[str, Any] = field(repr=False)\n'
        '    fetched: float = field(default_factory=time.monotonic)\n'
        '    credentials: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)\n'
        '    date_buckets: dict[str, list[int]] = field(default_factory=dict)\n'
        '\n'
        '    @property\n'
        '    def s3(self) -> bool:\n'
        '        return bool(self.metadata.get("s3_url") and self.metadata.get("buck_list"))\n'
        '\n'
        '    @property\n'
        '    def bucket_ids(self) -> list[int]:\n'
        '        rows = self.metadata.get("buck_list", [])\n'
        '        if not isinstance(rows, list):\n'
        '            raise CloudError("Cloud bucket list has an invalid shape")\n'
        '        return list(dict.fromkeys(_int(row.get("id"), "bucket ID")\n'
        '                                  for row in rows if isinstance(row, dict)))\n'
        '\n'
        '\n'
        '@dataclass(slots=True)\n'
        'class _Recording:\n'
        '    public: CloudRecording\n'
        '    device_id: str\n'
        '    day: str\n'
        '    backend: str\n'
        '    row: dict[str, Any] = field(repr=False)\n'
        '\n'
        '\n'
        'def _s3_endpoint(credential: dict[str, Any]) -> str:\n'
        '    endpoint = credential.get("host")\n'
        '    if not isinstance(endpoint, str) or not endpoint:\n'
        '        raise CloudError("Cloud storage endpoint is missing")\n'
        '    if "://" not in endpoint:\n'
        '        scheme = credential.get("scheme")\n'
        '        if scheme not in ("http", "https"):\n'
        '            raise CloudError("Cloud storage scheme is missing or invalid")\n'
        '        endpoint = scheme + "://" + endpoint\n'
        '    return endpoint\n'
        '\n'
        '\n'
        'def _s3_recording_row(wire: dict[str, Any]) -> dict[str, Any]:\n'
        '    """Decode the APK\'s Gson SerializedName keys, not Java property names."""\n'
        '    raw_slices = wire.get("slf")\n'
        '    if raw_slices is None:\n'
        '        raw_slices = []\n'
        '    if not isinstance(raw_slices, list) or any(not isinstance(item, dict) for item in raw_slices):\n'
        '        raise CloudError("V380 recording segment list is invalid")\n'
        '    return {\n'
        '        "bucket_index": wire.get("bdx"), "bucket_name": wire.get("bnm"),\n'
        '        "endpoint": wire.get("ep"), "prefix_path": wire.get("pth"),\n'
        '        "file_path": wire.get("fth"), "zone": wire.get("zn", 0), "dst": wire.get("dst", 0),\n'
        '        "slice_inf": [{"start_time": item.get("ste"), "end_time": item.get("ede"),\n'
        '                       "filename": item.get("fne"), "rtype": item.get("rte", 0)}\n'
        '                      for item in raw_slices],\n'
        '    }\n'
        '\n'
        '\n'
        'def s3_presigned_url(credential: dict[str, Any], key: str, *,\n'
        '                     now: datetime | None = None, expires: int = 900) -> str:\n'
        '    """S3 SigV4 GET for the APK\'s standard S3 getObject operation.\n'
        '\n'
        '    Uses path addressing so custom S3-compatible endpoints retain their TLS\n'
        '    host. Credential region/host/scheme/bucket are supplied by V380, not guessed.\n'
        '    """\n'
        '    for name in ("host", "name", "region", "ak", "sk"):\n'
        '        if not isinstance(credential.get(name), str) or not credential[name]:\n'
        '            raise CloudError("Cloud storage credentials are incomplete")\n'
        '    endpoint = _s3_endpoint(credential)\n'
        '    parts = urlsplit(endpoint)\n'
        '    if (parts.scheme not in ("http", "https") or parts.query or parts.fragment\n'
        '            or parts.username or parts.password or not parts.hostname):\n'
        '        raise CloudError("Cloud storage endpoint is invalid")\n'
        '    now = now or datetime.now(timezone.utc)\n'
        '    stamp = now.strftime("%Y%m%dT%H%M%SZ")\n'
        '    day = now.strftime("%Y%m%d")\n'
        '    scope = f"{day}/{credential[\'region\']}/s3/aws4_request"\n'
        "    # Do not normalize object-key slashes or '..': those are significant in S3.\n"
        '    uri = quote(parts.path.rstrip("/") + "/" + credential["name"] + "/" + key,\n'
        '                safe="/-_.~")\n'
        '    query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256",\n'
        '             "X-Amz-Credential": f"{credential[\'ak\']}/{scope}",\n'
        '             "X-Amz-Date": stamp, "X-Amz-Expires": str(expires),\n'
        '             "X-Amz-SignedHeaders": "host"}\n'
        '    if credential.get("token"):\n'
        '        query["X-Amz-Security-Token"] = credential["token"]\n'
        '    canonical_query = "&".join(f"{quote(k, safe=\'-_.~\')}={quote(v, safe=\'-_.~\')}"\n'
        '                               for k, v in sorted(query.items()))\n'
        '    canonical = (f"GET\\n{uri}\\n{canonical_query}\\nhost:{parts.netloc}\\n\\nhost\\n"\n'
        '                 "UNSIGNED-PAYLOAD")\n'
        '    to_sign = f"AWS4-HMAC-SHA256\\n{stamp}\\n{scope}\\n{hashlib.sha256(canonical.encode()).hexdigest()}"\n'
        '    signing_key = ("AWS4" + credential["sk"]).encode()\n'
        '    for item in (day, credential["region"], "s3", "aws4_request"):\n'
        '        signing_key = hmac.new(signing_key, item.encode(), hashlib.sha256).digest()\n'
        '    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()\n'
        '    return urlunsplit((parts.scheme, parts.netloc, uri,\n'
        '                       canonical_query + "&X-Amz-Signature=" + signature, ""))\n'
        '\n'
        '\n'
        'class CloudRecordings:\n'
        '    """Read-only account cloud recording catalog and internal playback sources."""\n'
        '\n'
        '    def __init__(self, api: AccountClient, *, allow_insecure_http: bool = False,\n'
        '                 search_base: str = S3_SEARCH_BASE) -> None:\n'
        '        self.api = api\n'
        '        self.allow_insecure_http = allow_insecure_http\n'
        '        self.search_base = search_base\n'
        '        self._services: dict[str, _Service] = {}\n'
        '        self._records: dict[str, _Recording] = {}\n'
        '        self._locks: dict[str, asyncio.Lock] = {}\n'
        '\n'
        '    def clear(self) -> None:\n'
        '        self._services.clear()\n'
        '        self._records.clear()\n'
        '        self._locks.clear()\n'
        '\n'
        '    def validate_recording(self, device_id: str, recording_id: str) -> None:\n'
        '        """Check proxy-route ownership without a network request."""\n'
        '        record = self._records.get(recording_id)\n'
        '        if device_id not in self.api.devices or record is None or record.device_id != device_id:\n'
        '            raise CloudError("Cloud recording is no longer in the catalog")\n'
        '\n'
        '    def _cloud_url(self, value: Any, default_scheme: str = "http") -> str:\n'
        '        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):\n'
        '            raise CloudError("Cloud recording host is missing or invalid")\n'
        '        if "://" not in value:\n'
        '            value = default_scheme + "://" + value\n'
        '        parts = urlsplit(value)\n'
        '        if (parts.scheme not in ("http", "https") or not parts.hostname\n'
        '                or parts.username or parts.password or parts.query or parts.fragment):\n'
        '            raise CloudError("Cloud recording endpoint is invalid")\n'
        '        try:\n'
        '            address = ipaddress.ip_address(parts.hostname)\n'
        '        except ValueError:\n'
        '            if "." not in parts.hostname or parts.hostname.endswith((".local", ".localhost")):\n'
        '                raise CloudError("Cloud recording endpoint is not a public host")\n'
        '        else:\n'
        '            if not address.is_global:\n'
        '                raise CloudError("Cloud recording endpoint is not a public address")\n'
        '        if parts.scheme == "http" and not self.allow_insecure_http:\n'
        '            raise LegacyHttpRequired(\n'
        '                "V380 supplied an HTTP cloud recording endpoint. Enable the explicit "\n'
        '                "legacy HTTP option to use the app\'s protocol; cloud tokens and video "\n'
        '                "will travel without TLS on those requests."\n'
        '            )\n'
        '        return value.rstrip("/")\n'
        '\n'
        '    async def _service(self, device_id: str) -> _Service:\n'
        '        if device_id not in self.api.devices:\n'
        '            raise CloudError("Unknown camera")\n'
        '        async with self._locks.setdefault(device_id, asyncio.Lock()):\n'
        '            previous = self._services.get(device_id)\n'
        '            if previous and time.monotonic() - previous.fetched < 300:\n'
        '                return previous\n'
        '            response = await self.api._request_json(\n'
        '                self.api.billing_url.rstrip("/") + "/device/get-service-info",\n'
        '                service_request(self.api.access_token, self.api.user_id, int(device_id),\n'
        '                                self.api.user_position, int(time.time())),\n'
        '            )\n'
        '            self.api._check_result(response)\n'
        '            service = _Service(device_id, response)\n'
        '            if previous:\n'
        '                service.credentials = previous.credentials\n'
        '                service.date_buckets = previous.date_buckets\n'
        '            self._services[device_id] = service\n'
        '            return service\n'
        '\n'
        '    @staticmethod\n'
        '    def _catalog_result(response: dict[str, Any]) -> bool:\n'
        '        result = response.get("result")\n'
        '        if result == 200:\n'
        '            return True\n'
        '        if result in (-1002, 64534):  # signed and unsigned app NO_DATA values.\n'
        '            return False\n'
        '        raise CloudError("V380 recording catalog rejected the request (result %s)" %\n'
        '                         (result if isinstance(result, int) else "missing"))\n'
        '\n'
        '    async def _ucloud_catalog(self, service: _Service, kind: int, *,\n'
        '                              day: str | None = None, page_size: int = 365,\n'
        '                              channel: int = 0) -> list[dict[str, Any]]:\n'
        '        bucket = service.metadata.get("bucket")\n'
        '        if not isinstance(bucket, dict) or not bucket.get("list"):\n'
        '            if not service.metadata.get("bind_status"):\n'
        '                return []\n'
        '            raise UnsupportedError("V380 did not return a cloud recording list host")\n'
        '        endpoint = self._cloud_url(bucket["list"]) + "/list.php"\n'
        '        rows: list[dict[str, Any]] = []\n'
        '        seen_pages: set[str] = set()\n'
        '        for page in range(1, _MAX_PAGES + 1):\n'
        '            payload = {"access_token": self.api.access_token, "user_id": self.api.user_id,\n'
        '                       "type": kind, "device_id": int(service.device_id)}\n'
        '            if day is not None:\n'
        '                payload["date"] = _day(day)\n'
        '            payload.update(channel=channel, page=page, pageSize=page_size)\n'
        '            # NVCryptor.Encode uses Base64.DEFAULT, including LF line wrapping.\n'
        '            parameter = base64.encodebytes(_compact(payload).encode()).decode("ascii")\n'
        '            response = await self.api._request_json(endpoint, None, params={"param": parameter})\n'
        '            if not self._catalog_result(response):\n'
        '                break\n'
        '            batch = response.get("data")\n'
        '            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):\n'
        '                raise CloudError("V380 recording catalog has an invalid shape")\n'
        '            digest = hashlib.sha256(_compact(batch).encode()).hexdigest()\n'
        '            if batch and digest in seen_pages:\n'
        '                raise CloudError("V380 recording pagination did not advance")\n'
        '            seen_pages.add(digest)\n'
        '            rows.extend(batch)\n'
        '            if len(rows) > _MAX_RECORDINGS:\n'
        '                raise CloudError("V380 recording catalog exceeds the supported size")\n'
        '            count = response.get("count")\n'
        '            if len(batch) < page_size or (isinstance(count, int) and len(rows) >= count):\n'
        '                break\n'
        '        else:\n'
        '            raise CloudError("V380 recording catalog exceeds the supported page limit")\n'
        '        return rows\n'
        '\n'
        '    async def async_dates(self, device_id: str) -> list[str]:\n'
        '        service = await self._service(device_id)\n'
        '        if not service.s3:\n'
        '            rows = await self._ucloud_catalog(service, 2)\n'
        '            return sorted({_day(row.get("date")) for row in rows}, reverse=True)\n'
        '        endpoint = self._cloud_url(self.search_base) + "/replay/list"\n'
        '        cursor = 1\n'
        '        dates: set[str] = set()\n'
        '        for _ in range(_MAX_PAGES):\n'
        '            response = await self.api._request_json(endpoint, s3_list_request(\n'
        '                self.api.access_token, self.api.user_id, int(device_id),\n'
        '                service.bucket_ids, int(time.time()), cursor=cursor,\n'
        '            ))\n'
        '            if not self._catalog_result(response):\n'
        '                break\n'
        '            data = response.get("data")\n'
        '            if not isinstance(data, dict) or not isinstance(data.get("dates"), list):\n'
        '                raise CloudError("V380 recording date list has an invalid shape")\n'
        '            for row in data["dates"]:\n'
        '                if not isinstance(row, dict):\n'
        '                    raise CloudError("V380 recording date list contains an invalid item")\n'
        '                day = _day(row.get("fne"))\n'
        '                dates.add(day)\n'
        '                firms = row.get("flt")\n'
        '                if not isinstance(firms, list) or any(not isinstance(firm, dict) for firm in firms):\n'
        '                    raise CloudError("V380 recording date bucket list has an invalid shape")\n'
        '                service.date_buckets[day] = [_int(firm.get("bdx"), "bucket ID")\n'
        '                                             for firm in firms]\n'
        '            next_cursor = _int(data.get("cursor", -1), "date cursor")\n'
        '            if next_cursor == -1:\n'
        '                break\n'
        '            if next_cursor == cursor:\n'
        '                raise CloudError("V380 recording date pagination did not advance")\n'
        '            cursor = next_cursor\n'
        '        else:\n'
        '            raise CloudError("V380 recording dates exceed the supported page limit")\n'
        '        return sorted(dates, reverse=True)\n'
        '\n'
        '    async def async_recordings(self, device_id: str, day: str) -> list[CloudRecording]:\n'
        '        day = _day(day)\n'
        '        service = await self._service(device_id)\n'
        '        if service.s3:\n'
        '            endpoint = self._cloud_url(self.search_base) + "/replay/list"\n'
        '            response = await self.api._request_json(endpoint, s3_list_request(\n'
        '                self.api.access_token, self.api.user_id, int(device_id),\n'
        '                service.date_buckets.get(day) or service.bucket_ids, int(time.time()), day=day,\n'
        '            ))\n'
        '            if not self._catalog_result(response):\n'
        '                return []\n'
        '            rows = response.get("data")\n'
        '            backend = "s3"\n'
        '        else:\n'
        '            rows = await self._ucloud_catalog(service, 4, day=day, page_size=5000)\n'
        '            backend = "ucloud"\n'
        '        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):\n'
        '            raise CloudError("V380 recording list has an invalid shape")\n'
        '        if len(rows) > _MAX_RECORDINGS:\n'
        '            raise CloudError("V380 recording list exceeds the supported size")\n'
        '        result: list[CloudRecording] = []\n'
        '        # Expired entries are rebuilt from the cloud on each Media browser visit.\n'
        '        for key in [key for key, value in self._records.items()\n'
        '                    if value.device_id == device_id and value.day == day]:\n'
        '            self._records.pop(key)\n'
        '        for row in rows:\n'
        '            if backend == "s3":\n'
        '                row = _s3_recording_row(row)\n'
        '                slices = row.get("slice_inf")\n'
        '                if not isinstance(slices, list) or not slices:\n'
        '                    continue\n'
        '                if any(not isinstance(item, dict) for item in slices):\n'
        '                    raise CloudError("V380 recording segment list is invalid")\n'
        '                start = _int(slices[0].get("start_time"), "segment start")\n'
        '                zone = _int(row.get("zone", 0), "time zone")\n'
        '                dst = _int(row.get("dst", 0), "daylight saving")\n'
        '                title = datetime.fromtimestamp((start + zone + dst * 3600000) / 1000,\n'
        '                                              timezone.utc).strftime("%H:%M:%S")\n'
        '                identity = [row.get("bucket_index"), row.get("prefix_path"),\n'
        '                            row.get("file_path"), slices[0].get("filename"), start]\n'
        '                mime = HLS_MIME\n'
        '            else:\n'
        '                if str(row.get("device_id")) != device_id:\n'
        '                    raise CloudError("Cloud recording belongs to a different camera")\n'
        '                clock = _int(row.get("time"), "recording time")\n'
        '                title = f"{clock:06d}"\n'
        '                title = f"{title[:2]}:{title[2:4]}:{title[4:6]}"\n'
        '                filename = row.get("file_name")\n'
        '                if not isinstance(filename, str) or not filename:\n'
        '                    raise CloudError("V380 recording filename is missing")\n'
        '                identity = [row.get("id"), row.get("channel"), row.get("date"), clock,\n'
        '                            row.get("rand_num"), filename]\n'
        '                mime = _mime(filename)\n'
        '            recording_id = hashlib.sha256(_compact([device_id, day, backend, identity]).encode()).hexdigest()[:32]\n'
        '            public = CloudRecording(recording_id, title, mime)\n'
        '            if len(self._records) >= _MAX_RECORDINGS and recording_id not in self._records:\n'
        '                self._records.pop(next(iter(self._records)))\n'
        '            self._records[recording_id] = _Recording(public, device_id, day, backend, row)\n'
        '            result.append(public)\n'
        '        return result\n'
        '\n'
        '    async def _credential(self, service: _Service, bucket_id: int) -> dict[str, Any]:\n'
        '        previous = service.credentials.get(bucket_id)\n'
        '        if previous and isinstance(previous.get("exp"), int) and previous["exp"] > time.time() + 60:\n'
        '            return previous\n'
        '        endpoint = self._cloud_url(service.metadata.get("s3_url"))\n'
        '        payload = {"user_token": self.api.access_token, "buck_id": [bucket_id]}\n'
        '        timestamp = int(time.time())\n'
        '        signature = md5(md5(_compact(payload)) + "GzHong^&*2025" + str(timestamp))\n'
        '        response = await self.api._request_json(endpoint + "/v1/app/gettoken", payload,\n'
        '                                              params={"signature": signature, "timestamp": timestamp})\n'
        '        if response.get("code") != 7800:\n'
        '            raise CloudError("V380 did not grant cloud recording storage credentials")\n'
        '        data = response.get("data")\n'
        '        buckets = data.get("bucket") if isinstance(data, dict) else None\n'
        '        if not isinstance(buckets, list):\n'
        '            raise CloudError("V380 cloud recording storage credentials have an invalid shape")\n'
        '        for bucket in buckets:\n'
        '            if not isinstance(bucket, dict):\n'
        '                raise CloudError("V380 cloud recording storage credentials contain an invalid item")\n'
        '            service.credentials[_int(bucket.get("id"), "bucket ID")] = bucket\n'
        '        if bucket_id not in service.credentials:\n'
        '            raise CloudError("V380 did not grant credentials for this recording\'s storage bucket")\n'
        '        return service.credentials[bucket_id]\n'
        '\n'
        '    async def async_source(self, device_id: str, recording_id: str) -> RecordingSource:\n'
        '        self.validate_recording(device_id, recording_id)\n'
        '        record = self._records.get(recording_id)\n'
        '        if record is None or record.device_id != device_id:\n'
        '            raise CloudError("Cloud recording is no longer in the catalog")\n'
        '        service = await self._service(device_id)\n'
        '        row = record.row\n'
        '        if record.backend == "ucloud":\n'
        '            bucket = service.metadata.get("bucket")\n'
        '            if (not isinstance(bucket, dict)\n'
        '                    or bucket.get("bucket_index") != row.get("bucket_index")):\n'
        "                # Fetch the app's bucket-aware lookup, rather than downloading\n"
        '                # a recording from whichever bucket happened to be selected.\n'
        '                stamp = int(time.time())\n'
        '                bucket_id = _int(row.get("bucket_index"), "bucket ID")\n'
        '                payload = {"accesstoken": self.api.access_token, "bucketindex": bucket_id,\n'
        '                           "timestamp": stamp,\n'
        '                           "sign": md5(f"accesstoken={self.api.access_token}&bucketindex={bucket_id}"\n'
        '                                       f"&timestamp={stamp}hsshop2016")}\n'
        '                host_info = await self.api._request_json(\n'
        '                    self.api.billing_url.rstrip("/") + "/disk/get-host-list", payload)\n'
        '                self.api._check_result(host_info)\n'
        '                candidate = host_info.get("data")\n'
        '                if (not isinstance(candidate, dict)\n'
        '                        or candidate.get("bucket_index") != row.get("bucket_index")):\n'
        '                    raise UnsupportedError("V380 did not return a download host for this recording\'s bucket")\n'
        '                bucket = candidate\n'
        '            endpoint = self._cloud_url(bucket.get("download")) + "/download.php"\n'
        '            parameter = ucloud_download_parameter(self.api.access_token, row)\n'
        '            return RecordingSource(record.public.mime_type, row["file_name"],\n'
        '                                   endpoint + "?" + urlencode({"param": parameter}))\n'
        '        bucket_id = _int(row.get("bucket_index"), "bucket ID")\n'
        '        credential = await self._credential(service, bucket_id)\n'
        '        # Check scheme and public host before signing temporary credentials.\n'
        '        self._cloud_url(_s3_endpoint(credential))\n'
        '        prefix = row.get("prefix_path")\n'
        '        if not isinstance(prefix, str):\n'
        '            raise CloudError("V380 recording object prefix is missing")\n'
        '        segments: list[RecordingSegment] = []\n'
        '        for item in row["slice_inf"]:\n'
        '            filename = item.get("filename")\n'
        '            if not isinstance(filename, str) or not filename:\n'
        '                raise CloudError("V380 recording segment filename is missing")\n'
        '            start = _int(item.get("start_time"), "segment start")\n'
        '            end = _int(item.get("end_time"), "segment end")\n'
        '            if end <= start:\n'
        '                raise CloudError("V380 recording segment duration is invalid")\n'
        '            segments.append(RecordingSegment(s3_presigned_url(credential, prefix + filename),\n'
        '                                             (end - start) / 1000, _mime(filename)))\n'
        '        return RecordingSource(HLS_MIME, "recording.m3u8", segments=tuple(segments))\n'
    ),
}

_PROTOCOL_SOURCE_SHA256 = {'errors': '5b90ce98325faf9ace18d3519baa2361395a3f377c75b8b8b0c7bebc4259f13d', 'shared': '23eeebbb8422839d0fb26328a302d48a90aa34e4dffe0b4153db26989e74794e', 'account': '858b87bd4a7eb9f796984d6f19903782edf954165c5c402cd422e483b2ce3398', 'live': '14a676beb0d5c36c3a193bff88c4cff6dd2283fbb92d531adf358c48335d243b', 'mpegts': '5df2b4b70759cf278991d20c8ba3082b71eb3ac18d218350402dba4c81bfb59d', 'recordings': '3b30647f57ec2a1ff5756ee14d7a3f7ef1925c0b9a79d04882162e854daaa04f'}
_SELF_TEST_TS_B64 = 'R0AAMKYA////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AACwDQABwQAAAAHwACqxBLJHUAAwoQD/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////AAKwEgABwQAA4QDwABvhAPAAFb1NVkdBADAHEAAAAAB+AAAAAeAAAICABSEAAQABAAAAAWdCwAraEJoQAAADABAAAAMDKPEiagAAAAFozg/IAAABBgX//0LcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY1IC0gSC4yNjQvTVBFRy00IEFWQyBjb2RlYyAtIENvcHlsZWZ0IDIwMDMtMjAyNSAtIGh0dHA6Ly93d3cudmlkZW9sYW4ub3JnL3gyNjQuaHRtbCAtIG9wdGlvRwEAEW5zOiBjYWJhYz0wIHJlZj0xIGRlYmxvY2s9MDowOjAgYW5hbHlzZT0wOjAgbWU9ZGlhIHN1Ym1lPTAgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MCBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTAgOHg4ZGN0PTAgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZHAQASc2V0PTAgdGhyZWFkcz0xIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9MCB3ZWlnaHRwPTAga2V5aW50PTI1MCBrZXlpbnRfbWluPTI1IHNjZW5lY3V0PTAgaW50cmFfcmVmckcBADM9AP///////////////////////////////////////////////////////////////////////////////2VzaD0wIHJjPWNyZiBtYnRyZWU9MCBjcmY9MjMuMCBxY29tcD0wLjYwIHFwbWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0wAIAAAAFliIQ6EYoAAkqxwABEZjgACIzJycnXXXXXXXXXXXXgR0EAND4QAAAHCH4A/////////////////////////////////////////////////////////////////////////wAAAeAAAICABSEAARwhAAAAAUGaIDK8KRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUcAAQIo4AAgRYnxPifP5/P5/P5/P5/P5/P5/BHQQA1PhAAAA4QfgD/////////////////////////////////////////////////////////////////////////AAAB4AAAgIAFIQABOEEAAAABQZpAMrwpFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRwABAijgACBFifE+J8/n8/n8/n8/n8/n8/n8EdBADY+EAAAFRh+AP////////////////////////////////////////////////////////////////////////8AAAHgAACAgAUhAAFUYQAAAAFBmmAyvCkUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFHAAECKOAAIEWJ8T4nz+fz+fz+fz+fz+fz+fwR0EANz4QAAAcIH4A/////////////////////////////////////////////////////////////////////////wAAAeAAAICABSEAAXCBAAAAAUGagDK8KRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUcAAQIo4AAgRYnxPifP5/P5/P5/P5/P5/P5/BHQQA4PhAAACMofgD/////////////////////////////////////////////////////////////////////////AAAB4AAAgIAFIQABjKEAAAABQZqgMrwpFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRwABAijgACBFifE+J8/n8/n8/n8/n8/n8/n8EdBADk+EAAAKjB+AP////////////////////////////////////////////////////////////////////////8AAAHgAACAgAUhAAGowQAAAAFBmsAyvCkUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFHAAECKOAAIEWJ8T4nz+fz+fz+fz+fz+fz+fwR0EAOj4QAAAxOH4A/////////////////////////////////////////////////////////////////////////wAAAeAAAICABSEAAcThAAAAAUGa4DK8KRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUcAAQIo4AAgRYnxPifP5/P5/P5/P5/P5/P5/BHQQA7PhAAADhAfgD/////////////////////////////////////////////////////////////////////////AAAB4AAAgIAFIQAB4QEAAAABQZsAMrwpFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRwABAijgACBFifE+J8/n8/n8/n8/n8/n8/n8EdBADw+EAAAP0h+AP////////////////////////////////////////////////////////////////////////8AAAHgAACAgAUhAAH9IQAAAAFBmyAyvCkUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFHAAECKOAAIEWJ8T4nz+fz+fz+fz+fz+fz+fwR0EAPT4QAABGUH4A/////////////////////////////////////////////////////////////////////////wAAAeAAAICABSEAAxlBAAAAAUGbQDK8KRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUcAAQIo4AAgRYnxPifP5/P5/P5/P5/P5/P5/BHQQA+PhAAAE1YfgD/////////////////////////////////////////////////////////////////////////AAAB4AAAgIAFIQADNWEAAAABQZtgMrwpFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRwABAijgACBFifE+J8/n8/n8/n8/n8/n8/n8EdBAD+AEAAAVGB+AP////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////8AAAHgAACAgAUhAANRgQAAAAFBm4Ayo3FAAEFGOAAICscAAQFcT4nxPn8/n8/n8/n8/n8/n8/gR0EAMIYQAABbaH4A/////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////////wAAAeAAAICABSEAA22hAAAAAUGboDKjcUAA1HAMcA4nxPifP5/P5/P5/P5/P5/P5/BHQQAxPhAAAGJwfgD/////////////////////////////////////////////////////////////////////////AAAB4AAAgIAFIQADicEAAAABQZvAMrwpFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRQABACigACAFFAAEAKKAAIAUUAAQAooAAgBRwABAijgACBFifE+J8/n8/n8/n8/n8/n8/n8A=='
_SELF_TEST_EXPECTED_FRAMES = 15

#!/usr/bin/env python3
"""V380 cloud and optional Home Assistant diagnostic (Python 3.11+).

Prompts for passwords/tokens locally. Tests every discovered camera by default.
Does not change device configuration. Only --save-media writes private footage.
The JSON report excludes account/camera names, IDs, tokens and playback URLs.
"""

import argparse
import base64
import asyncio
from datetime import datetime, timezone
import getpass
import io
import json
from pathlib import Path
import re
import ssl
import sys
import time
import types
from urllib.parse import quote, urljoin, urlsplit

DEPENDENCIES = "aiohttp cryptography av certifi"
_MODULES = ("errors", "shared", "account", "live", "mpegts", "recordings")
_PROTOCOL = None


def load_protocol():
    global _PROTOCOL
    if _PROTOCOL is not None:
        return _PROTOCOL
    try:
        import aiohttp
        import av
        import certifi
        import cryptography
    except ImportError:
        raise SystemExit(f"Missing dependencies. Run: python3 -m pip install {DEPENDENCIES}") from None
    av.logging.set_level(av.logging.PANIC)
    sources = globals().get("_EMBEDDED_PROTOCOL")
    if sources is None:
        root = Path(__file__).resolve().parents[1] / "custom_components" / "v380_cloud"
        sources = {name: (root / f"{name}.py").read_text() for name in _MODULES}
    package_name = "_v380_full_test_protocol"
    package = types.ModuleType(package_name)
    package.__path__ = []
    sys.modules[package_name] = package
    modules = {}
    for name in _MODULES:
        full_name = package_name + "." + name
        module = types.ModuleType(full_name)
        module.__package__ = package_name
        module.__file__ = f"<embedded V380 {name}>"
        sys.modules[full_name] = module
        exec(compile(sources[name], module.__file__, "exec"), module.__dict__)
        modules[name] = module
    _PROTOCOL = types.SimpleNamespace(**modules, aiohttp=aiohttp, av=av, certifi=certifi)
    return _PROTOCOL


class CheckError(Exception):
    def __init__(self, code, status="fail", **details):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details


def failure(exc):
    p = load_protocol()
    if isinstance(exc, CheckError):
        return {"status": exc.status, "reason": exc.code, **exc.details}
    if isinstance(exc, p.errors.LegacyHttpRequired):
        return {"status": "blocked", "reason": "legacy_http_required",
                "hint": "The vendor requires HTTP. Use --allow-insecure-http only if you accept unencrypted cloud-token/video traffic."}
    if isinstance(exc, p.errors.AuthError):
        return {"status": "fail", "reason": "authentication_rejected"}
    if isinstance(exc, p.errors.UnsupportedError):
        return {"status": "unsupported", "reason": "cloud_protocol_not_implemented"}
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return {"status": "fail", "reason": "timeout"}
    if isinstance(exc, p.aiohttp.ClientSSLError):
        return {"status": "fail", "reason": "tls_verification_failed"}
    if isinstance(exc, (p.aiohttp.ClientError, OSError)):
        return {"status": "fail", "reason": "connection_failed"}
    result = {"status": "fail", "reason": "protocol_error" if isinstance(exc, p.errors.CloudError) else "unexpected_error",
              "exception_type": type(exc).__name__}
    # Only fixed numeric vendor codes are retained, never arbitrary server text.
    codes = re.findall(r"\b(?:code|result|error)\s+(-?\d+)\b", str(exc))
    if codes:
        result["vendor_codes"] = [int(value) for value in codes[:4]]
    return result


def log(label, result):
    print(f"{label}: {result['status'].upper()}" + (f" ({result['reason']})" if "reason" in result else ""), flush=True)


def combine(results):
    statuses = [item["status"] for item in results]
    if "fail" in statuses:
        return "fail"
    if not statuses or any(value != "pass" for value in statuses):
        return "incomplete"
    return "pass"


def decode_video(data, *, format=None):
    p = load_protocol()
    count, first_pts, last_pts, video_stream, codec, width, height = 0, None, None, None, None, 0, 0
    try:
        with p.av.open(io.BytesIO(data), mode="r", format=format) as media:
            if not media.streams.video:
                raise CheckError("no_video_stream")
            video_stream = media.streams.video[0]
            codec = video_stream.codec_context.name
            for frame in media.decode(video=0):
                count += 1
                width, height = frame.width, frame.height
                if frame.pts is not None and frame.time_base is not None:
                    pts = float(frame.pts * frame.time_base)
                    first_pts = pts if first_pts is None else first_pts
                    last_pts = pts
                if count >= 300:
                    break
    except CheckError:
        raise
    except Exception:
        raise CheckError("video_decode_failed") from None
    if not count:
        raise CheckError("no_decoded_video_frames")
    result = {"status": "pass", "codec": codec, "decoded_frames": count,
              "width": width, "height": height, "sample_bytes": len(data)}
    if first_pts is not None and last_pts is not None:
        result["decoded_span_seconds"] = round(max(0, last_pts - first_pts), 3)
    return result


def save_sample(args, name, data):
    if args.save_media:
        root = args.save_media.expanduser()
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        # Choose another local filename instead of overwriting earlier footage.
        for suffix in range(1000):
            candidate = path if suffix == 0 else path.with_name(f"{path.stem}-{suffix}{path.suffix}")
            try:
                with candidate.open("xb") as output:
                    output.write(data)
                path = candidate
                break
            except FileExistsError:
                continue
        else:
            raise CheckError("media_output_filename_limit", "incomplete")
        print(f"  Private media saved: {path}", flush=True)


async def download(session, url, limit, *, timeout=45, headers=None):
    async with session.get(url, headers=headers, allow_redirects=False,
                           timeout=load_protocol().aiohttp.ClientTimeout(total=timeout)) as response:
        if response.status not in (200, 206):
            raise CheckError("media_http_error", http_status=response.status)
        length = response.content_length
        if length is not None and length > limit:
            raise CheckError("download_limit", "incomplete", declared_bytes=length, limit_bytes=limit)
        body = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            body.extend(chunk)
            if len(body) > limit:
                raise CheckError("download_limit", "incomplete", limit_bytes=limit)
        content_type = response.headers.get("Content-Type", "").lower()
        if not body:
            raise CheckError("empty_media_response")
        if any(part in content_type for part in ("application/json", "text/html", "text/plain")) or body.lstrip().startswith((b"{", b"[", b"<")):
            raise CheckError("error_document_instead_of_video")
        return bytes(body)


async def live_check(client, device, args, index):
    p = load_protocol()
    transport, frames = None, None
    try:
        if int(device.metadata.get("type", 0)) >= 2:
            raise CheckError("new_key_exchange_live_protocol", "unsupported")
        transport = p.live.CloudLiveClient(device.metadata, p.account.device_password(device.metadata), audio=False)
        frames = transport.async_frames()
        mux = None
        data = bytearray()
        started = None
        received = 0
        # This bounds discovery/login and then the requested sample interval.
        async with asyncio.timeout(args.startup_timeout + args.live_seconds):
            async for frame in frames:
                if frame.codec not in ("h264", "hevc", "h265"):
                    continue
                if mux is None:
                    if not frame.keyframe:
                        continue
                    mux = p.mpegts.MpegTsMuxer(frame.codec)
                    started = time.monotonic()
                if mux.codec != frame.codec:
                    raise CheckError("codec_changed_during_sample")
                data.extend(mux.push(frame.payload, time.monotonic(), keyframe=frame.keyframe))
                received += 1
                if len(data) > args.max_bytes:
                    raise CheckError("live_sample_limit", "incomplete", limit_bytes=args.max_bytes)
                if time.monotonic() - started >= args.live_seconds and received >= 2:
                    break
        if not data:
            raise CheckError("no_live_keyframe")
        if received < 2 or started is None or time.monotonic() - started < args.live_seconds:
            raise CheckError("live_session_ended_before_sample_completed")
        result = await asyncio.to_thread(decode_video, bytes(data), format="mpegts")
        result["received_access_units"] = received
        result["transport"] = "remote_cloud_relay"
        save_sample(args, f"camera-{index:03d}-live.ts", data)
        return result
    finally:
        if frames is not None:
            await frames.aclose()
        if transport is not None:
            await transport.async_close()


async def recording_check(client, device, args, index):
    catalog = load_protocol().recordings.CloudRecordings(client, allow_insecure_http=args.allow_insecure_http)
    dates = await catalog.async_dates(device.device_id)
    if not dates:
        return {"status": "incomplete", "reason": "no_cloud_recording_dates", "date_count": 0}
    clips = await catalog.async_recordings(device.device_id, dates[0])
    result = {"status": "incomplete", "date_count": len(dates), "latest_day_clip_count": len(clips), "samples": []}
    if not clips:
        result["reason"] = "no_recordings_on_latest_date"
        return result
    for number, clip in enumerate(sorted(clips, key=lambda c: c.title, reverse=True)[:args.recordings_per_camera], 1):
        try:
            source = await catalog.async_source(device.device_id, clip.recording_id)
            if source.url:
                data = await download(client.session, source.url, args.max_bytes)
                item = await asyncio.to_thread(decode_video, data)
                item["backend"] = "ucloud_download"
                save_sample(args, f"camera-{index:03d}-recording-{number:02d}.mp4", data)
            elif source.segments:
                segments, total = [], 0
                for part_number, part in enumerate(source.segments[:args.segments_per_recording], 1):
                    data = await download(client.session, part.url, args.max_bytes - total)
                    total += len(data)
                    checked = await asyncio.to_thread(decode_video, data)
                    segments.append(checked)
                    save_sample(args, f"camera-{index:03d}-recording-{number:02d}-part-{part_number:02d}.ts", data)
                item = {"status": combine(segments), "backend": "s3_segments", "tested_segments": len(segments),
                        "total_segments": len(source.segments), "segments": segments}
            else:
                raise CheckError("recording_has_no_playable_source")
        except Exception as exc:
            item = failure(exc)
        result["samples"].append(item)
        log(f"  Camera {index} recording {number}", item)
    result["status"] = combine(result["samples"])
    return result


def same_origin(base, value):
    target = urljoin(base, value)
    def origin(url):
        p = urlsplit(url)
        return p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80)
    if origin(target) != origin(base) or urlsplit(target).username is not None or urlsplit(target).password is not None:
        raise CheckError("external_ha_media_url", "unsupported")
    return target


class HAConnection:
    def __init__(self, session, base, token):
        self.session, self.base, self.token = session, base.rstrip("/"), token
        self.headers = {"Authorization": "Bearer " + token}
        self.ws = None
        self.counter = 0

    async def json(self, path):
        url = same_origin(self.base, path)
        async with self.session.get(url, headers=self.headers, allow_redirects=False,
                                    timeout=load_protocol().aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise CheckError("ha_http_error", http_status=response.status)
            data = await response.content.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise CheckError("ha_response_limit", "incomplete")
            try:
                return json.loads(data)
            except (ValueError, UnicodeError):
                raise CheckError("ha_invalid_json") from None

    async def connect(self):
        target = same_origin(self.base, "/api/websocket")
        async with asyncio.timeout(30):
            self.ws = await self.session.ws_connect(
                target, timeout=load_protocol().aiohttp.ClientWSTimeout(ws_receive=30, ws_close=10),
                max_msg_size=8 * 1024 * 1024,
            )
        # aiohttp may follow a WebSocket handshake redirect. Check the actual
        # endpoint before sending a long-lived token in the auth message.
        handshake = getattr(self.ws, "_response", None)
        if handshake is None:
            raise CheckError("ha_websocket_origin_not_available", "unsupported")
        same_origin(self.base, str(handshake.url))
        async with asyncio.timeout(30):
            hello = await self.ws.receive_json()
            if hello.get("type") != "auth_required":
                raise CheckError("ha_websocket_handshake")
            await self.ws.send_json({"type": "auth", "access_token": self.token})
            response = await self.ws.receive_json()
            if response.get("type") != "auth_ok":
                raise CheckError("ha_authentication_rejected")

    async def command(self, type, **fields):
        self.counter += 1
        ident = self.counter
        await self.ws.send_json({"id": ident, "type": type, **fields})
        async with asyncio.timeout(120):
            while True:
                response = await self.ws.receive_json()
                if response.get("id") == ident:
                    if not response.get("success"):
                        # Arbitrary server error text can contain private IDs/URLs.
                        raise CheckError("ha_command_failed")
                    return response.get("result")

    async def close(self):
        if self.ws is not None:
            await self.ws.close()


async def ha_hls(connection, source, args, depth=0):
    if depth > 2:
        raise CheckError("ha_hls_nested_playlist_limit", "incomplete")
    url = same_origin(connection.base, source)
    async with connection.session.get(url, headers=connection.headers, allow_redirects=False,
                                      timeout=load_protocol().aiohttp.ClientTimeout(total=90)) as response:
        if response.status != 200:
            raise CheckError("ha_hls_http_error", http_status=response.status)
        data = await response.content.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise CheckError("ha_hls_playlist_limit", "incomplete")
        text = data.decode("utf-8-sig")
    if not text.lstrip().startswith("#EXTM3U"):
        raise CheckError("ha_invalid_hls_playlist")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if any(line.startswith("#EXT-X-KEY") and "METHOD=NONE" not in line for line in lines):
        raise CheckError("ha_encrypted_hls_not_tested", "unsupported")
    references = [line for line in lines if not line.startswith("#")]
    if not references:
        # HA low-latency HLS can initially expose complete parts before a full
        # segment exists. Use actual part objects with the same init map.
        for line in lines:
            if line.startswith("#EXT-X-PART:"):
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    references.append(match.group(1))
        if not references:
            raise CheckError("ha_hls_has_no_segments", "incomplete")
    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        return await ha_hls(connection, same_origin(url, references[0]), args, depth + 1)
    output = bytearray()
    for line in lines:
        if line.startswith("#EXT-X-MAP:"):
            match = re.search(r'URI="([^"]+)"', line)
            if not match:
                raise CheckError("ha_invalid_hls_init_map")
            output.extend(await download(connection.session, same_origin(url, match.group(1)), args.max_bytes,
                                         headers=connection.headers, timeout=90))
            break
    for ref in references[:3]:
        output.extend(await download(connection.session, same_origin(url, ref), args.max_bytes - len(output),
                                     headers=connection.headers, timeout=90))
    return await asyncio.to_thread(decode_video, bytes(output))


async def homeassistant_check(session, args):
    token = getpass.getpass("Home Assistant long-lived access token (hidden): ")
    connection = HAConnection(session, args.ha_url, token)
    result = {"status": "incomplete", "checks": []}
    try:
        config = await connection.json("/api/config")
        result["version"] = str(config.get("version", "unknown"))[:32]
        await connection.connect()
        registry = await connection.command("config/entity_registry/list")
        cameras = [row for row in registry if row.get("platform") == "v380_cloud"
                   and row.get("entity_id", "").startswith("camera.") and not row.get("disabled_by")]
        result["camera_count"] = len(cameras)
        if not cameras:
            raise CheckError("no_enabled_v380_cloud_camera_entities", "incomplete")
        for index, row in enumerate(cameras, 1):
            entity = row["entity_id"]
            item = {"camera_index": index}
            try:
                state = await connection.json("/api/states/" + quote(entity, safe=""))
                if state.get("state") in ("unavailable", "unknown"):
                    raise CheckError("ha_camera_unavailable")
                stream = await connection.command("camera/stream", entity_id=entity, format="hls")
                if not isinstance(stream, dict) or not isinstance(stream.get("url"), str):
                    raise CheckError("ha_camera_stream_missing")
                item["live"] = await ha_hls(connection, stream["url"], args)
            except Exception as exc:
                item["live"] = failure(exc)
            result["checks"].append(item["live"])
            log(f"Home Assistant camera {index} live", item["live"])
        try:
            identifier = "media-source://v380_cloud"
            for level in range(4):
                node = await connection.command("media_source/browse_media", media_content_id=identifier)
                children = node.get("children", []) if isinstance(node, dict) else []
                if not children:
                    raise CheckError("ha_no_cloud_recordings_to_test", "incomplete")
                playable = [child for child in children if child.get("can_play")]
                if playable:
                    clip = playable[0]
                    resolved = await connection.command("media_source/resolve_media", media_content_id=clip["media_content_id"])
                    url = same_origin(connection.base, resolved["url"])
                    if "mpegurl" in resolved.get("mime_type", ""):
                        checked = await ha_hls(connection, url, args)
                    else:
                        data = await download(session, url, args.max_bytes, headers=connection.headers, timeout=90)
                        checked = await asyncio.to_thread(decode_video, data)
                    result["recording"] = checked
                    break
                identifier = children[0].get("media_content_id")
                if not isinstance(identifier, str):
                    raise CheckError("ha_media_browser_identifier_missing")
            else:
                raise CheckError("ha_media_browser_depth_limit", "incomplete")
        except Exception as exc:
            result["recording"] = failure(exc)
        result["checks"].append(result["recording"])
        log("Home Assistant cloud recording", result["recording"])
        result["status"] = combine(result["checks"])
    except Exception as exc:
        result.update(failure(exc))
    finally:
        await connection.close()
        connection.token = ""
        connection.headers.clear()
    return result


async def run(args):
    p = load_protocol()
    report = {"script_version": "1.0.0", "started_utc": datetime.now(timezone.utc).isoformat(),
              "protocol_snapshot": globals().get("_PROTOCOL_SOURCE_SHA256", "source_tree"),
              "mode": "cloud_and_home_assistant" if args.ha_url and not args.ha_only else "home_assistant" if args.ha_only else "cloud",
              "scope": {"cloud_live_sample_seconds": args.live_seconds, "recordings_per_camera": args.recordings_per_camera,
                        "segments_per_recording": args.segments_per_recording, "max_bytes_per_sample": args.max_bytes,
                        "browser_rendering": "not_tested", "all_historical_recordings": "not_tested", "decoder_frame_limit_per_sample": 300,
                        "home_assistant_api": "requested" if args.ha_url else "not_tested",
                        "home_assistant_recording_scope": "representative_clip" if args.ha_url else "not_tested",
                        "adapter_interpreter_shutdown": "not_tested"},
              "cameras": [], "checks": []}
    context = ssl.create_default_context(cafile=p.certifi.where())
    async with p.aiohttp.ClientSession(connector=p.aiohttp.TCPConnector(ssl=context)) as session:
        if not args.ha_only:
            username = input("V380 cloud email/phone account: ").strip()
            password = getpass.getpass("V380 cloud account password (hidden): ")
            client = p.account.AccountClient(session, username, password)
            try:
                await client.async_login()
                report["login"] = {"status": "pass"}
                log("Cloud account login", report["login"])
                devices = await client.async_devices()
                report["discovery"] = {"status": "pass", "camera_count": len(devices)}
                log("Cloud camera discovery", report["discovery"])
                selected = [(i, device) for i, device in enumerate(devices, 1) if args.camera is None or i == args.camera]
                if not selected:
                    raise CheckError("no_matching_cloud_cameras", "incomplete")
                for index, device in selected:
                    item = {"camera_index": index, "shared": bool(device.metadata.get("shared")),
                            "platform": "new_key_exchange" if int(device.metadata.get("type", 0)) >= 2 else "legacy"}
                    print(f"\nTesting camera {index} ({item['platform']})", flush=True)
                    for key, function in (("live", live_check), ("recordings", recording_check)):
                        try:
                            item[key] = await function(client, device, args, index)
                        except Exception as exc:
                            item[key] = failure(exc)
                        log(f"Camera {index} {key}", item[key])
                        report["checks"].append(item[key])
                    report["cameras"].append(item)
            except Exception as exc:
                report["cloud_error"] = failure(exc)
                report["checks"].append(report["cloud_error"])
                log("Cloud test", report["cloud_error"])
            finally:
                await client.async_close()
                password = ""
            report["checks"].extend([report[key] for key in ("login", "discovery") if key in report])
        if args.ha_url:
            print("\nTesting the installed Home Assistant integration", flush=True)
            report["home_assistant"] = await homeassistant_check(session, args)
            report["checks"].append(report["home_assistant"])
    report["overall"] = combine(report["checks"])
    report["finished_utc"] = datetime.now(timezone.utc).isoformat()
    args.report.expanduser().parent.mkdir(parents=True, exist_ok=True)
    args.report.expanduser().write_text(json.dumps(report, indent=2) + "\n")
    print(f"\nOverall: {report['overall'].upper()}\nSanitized report: {args.report}", flush=True)
    print("PASS covers the requested samples; browser rendering and untested firmware remain outside this check.")
    return 0 if report["overall"] == "pass" else 1 if report["overall"] == "fail" else 2


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--camera", type=int, help="Test this camera index only; default tests every discovered camera")
    p.add_argument("--live-seconds", type=float, default=10, help="Live sample length after the first keyframe")
    p.add_argument("--startup-timeout", type=float, default=60, help="Maximum cloud discovery/login/keyframe startup time")
    p.add_argument("--recordings-per-camera", type=int, default=1, help="Newest recordings sampled from the newest available day")
    p.add_argument("--segments-per-recording", type=int, default=3, help="Maximum S3 segments decoded for one recording")
    p.add_argument("--max-download-mib", type=int, default=32, help="Maximum MiB per live/recording/HA sample")
    p.add_argument("--allow-insecure-http", action="store_true", help="Explicitly permit vendor HTTP recording requests carrying cloud tokens/video")
    p.add_argument("--ha-url", help="Also test the installed integration, e.g. http://homeassistant.local:8123; token is prompted")
    p.add_argument("--ha-only", action="store_true", help="Skip direct V380 checks; requires --ha-url")
    p.add_argument("--report", type=Path, default=Path("v380-test-report.json"), help="Sanitized JSON report path")
    p.add_argument("--save-media", type=Path, help="Explicitly save private test footage to this directory; files are never overwritten")
    p.add_argument("--self-test", action="store_true", help="Run local decode/protocol self-tests without credentials or vendor requests")
    return p


def self_test():
    p = load_protocol()
    if globals().get("_SELF_TEST_TS_B64"):
        sample = base64.b64decode(_SELF_TEST_TS_B64)
        expected_frames = _SELF_TEST_EXPECTED_FRAMES
    else:
        from fractions import Fraction
        encoder = p.av.CodecContext.create("libx264", "w")
        encoder.width = encoder.height = 64
        encoder.pix_fmt = "yuv420p"
        encoder.time_base = Fraction(1, 25)
        encoder.framerate = Fraction(25, 1)
        encoder.options = {"preset": "ultrafast", "tune": "zerolatency"}
        encoder.open()
        mux = p.mpegts.MpegTsMuxer("h264")
        sample = bytearray()
        for index in range(12):
            frame = p.av.VideoFrame(64, 64, "yuv420p")
            frame.pts = index
            frame.time_base = Fraction(1, 25)
            for plane in frame.planes:
                plane.update(bytes([index * 15]) * plane.buffer_size)
            for packet in encoder.encode(frame):
                sample.extend(mux.push(bytes(packet), index / 25, keyframe=packet.is_keyframe))
        for packet in encoder.encode(None):
            sample.extend(mux.push(bytes(packet)))
        expected_frames = 12
    decoded = decode_video(bytes(sample), format="mpegts")
    assert decoded["decoded_frames"] == expected_frames
    assert p.account.encode_account_password("fixture-password") == "f0a94927d1d8c0ed2af723decc007160050187a0cde5a9872cbab091ab73e553"
    assert p.account.device_password({"type": 1, "ver": 2, "rand_key": "12345678", "device_password": "SGzE7BB1F/WuyL2r1Gbk2A=="}) == "camera-password"
    try:
        same_origin("http://ha.example:8123", "https://other.example/private")
    except CheckError:
        pass
    else:
        raise CheckError("self_test_origin_guard_failed")
    print(f"SELF-TEST PASS: account vectors, device password, MPEGTS decoding ({expected_frames} frames), and HA token-origin guard.")
    return 0


def main():
    p = parser()
    args = p.parse_args()
    if sys.version_info < (3, 11):
        p.error("Python3.11 or newer is required")
    if args.ha_only and not args.ha_url:
        p.error("--ha-only requires --ha-url")
    if args.ha_url:
        value = urlsplit(args.ha_url)
        if value.scheme not in ("http", "https") or not value.hostname or value.username or value.password or value.query or value.fragment or value.path not in ("", "/"):
            p.error("--ha-url must be an HTTP(S) origin without credentials, query, or subpath")
    if args.camera is not None and args.camera < 1:
        p.error("--camera must be positive")
    if not 1 <= args.live_seconds <= 120 or not 5 <= args.startup_timeout <= 180:
        p.error("--live-seconds must be1–120 and --startup-timeout5–180")
    if not 1 <= args.recordings_per_camera <= 20 or not 1 <= args.segments_per_recording <= 20 or not 1 <= args.max_download_mib <= 1024:
        p.error("Recording/segment counts must be1–20 and download limit1–1024MiB")
    args.max_bytes = args.max_download_mib * 1024 * 1024
    if not args.self_test and not sys.stdin.isatty():
        print("Run this script in an interactive terminal so passwords and tokens can be entered without echo.", file=sys.stderr)
        return 2
    try:
        return self_test() if args.self_test else asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print exception bodies, tracebacks or credential-bearing URLs.
        print(f"Diagnostic stopped ({type(exc).__name__}). No credential details printed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
