"""V380 account protocol recovered from classes10.dex.

Sources: OkHttpUtil.accountLogin/updateUserDeviceList/renewUserToken,
GlobalDefines.encrypt/md5, LoginJsonParse, UserDeivceListJsonParse.
Development uses fixtures; separate user-run validation is in docs/VALIDATION.md.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import hashlib
import json
import time
from typing import Any

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .errors import AuthError, CloudError, UnsupportedError
from .shared import normalize_shared_device

API_BASE = "https://mapi.av380.net/"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=10)
MAX_JSON_BYTES = 8 * 1024 * 1024
_APP_AES_KEY = b"1234567890123456"
_APP_SIGN_SUFFIX = "hsshop2016"


def md5(value: str) -> str:
    """Match the app's lowercase UTF-8 MD5, used for protocol compatibility."""
    return hashlib.md5(value.encode("utf-8"), usedforsecurity=False).hexdigest()


def encode_account_password(password: str) -> str:
    padder = PKCS7(128).padder()
    padded = padder.update(password.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(_APP_AES_KEY), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


def login_request(username: str, password: str, timestamp: int) -> dict[str, Any]:
    encoded = encode_account_password(password)
    return {
        "username": username,
        "password": encoded,
        "phonetoken": "100",
        "sign": md5(f"password={encoded}&phonetoken=100&timestamp={timestamp}&username={username}{_APP_SIGN_SUFFIX}"),
        "timestamp": timestamp,
    }


def devices_request(access_token: str, timestamp: int) -> dict[str, Any]:
    # get_sub_server=10 asks for remote dispatcher metadata. Do not register a
    # mobile push ID or change account/device configuration.
    canonical = (
        f"accesstoken={access_token}&from_app=V380Pro&get_sub_server=10"
        f"&language=en&registrationid=&timestamp={timestamp}&type=all&ver=1008"
    )
    return {
        "accesstoken": access_token,
        "from_app": "V380Pro",
        "get_sub_server": 10,
        "language": "en",
        "registrationid": "",
        "sign": md5(canonical + _APP_SIGN_SUFFIX),
        "timestamp": timestamp,
        "type": "all",
        "ver": 1008,
    }


@dataclass(frozen=True, slots=True)
class CloudDevice:
    device_id: str
    name: str
    online: bool | None = None  # device/list does not establish online status.
    metadata: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class CloudRecording:
    recording_id: str
    title: str
    mime_type: str = "video/mp4"


def device_password(metadata: dict[str, Any]) -> str:
    """Decode legacy device credentials exactly as BaseActivity$55 does.

    New-platform devices authenticate with key exchange; do not mistake their
    encrypted rk/public_key values for a cleartext camera password.
    """
    if int(metadata.get("type", 0)) >= 2:
        raise UnsupportedError("This camera requires the newer cloud key-exchange protocol")
    encoded = metadata.get("device_password")
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded, validate=True)
        if int(metadata.get("ver", 0)) == 2:
            key = (str(metadata.get("rand_key", "")) + "hongshi1").encode()
            if len(key) not in (16, 24, 32):
                raise ValueError("Invalid key length")
            decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            padded = decryptor.update(raw) + decryptor.finalize()
            unpadder = PKCS7(128).unpadder()
            raw = unpadder.update(padded) + unpadder.finalize()
        return raw.decode("utf-8")
    except (ValueError, TypeError, UnicodeError) as err:
        raise CloudError("Camera credential encoding is unsupported",
                         stage="device_credentials", reason="invalid_metadata") from err


class AccountClient:
    """Shared account session. The caller owns the aiohttp ClientSession."""

    def __init__(self, session: aiohttp.ClientSession, username: str, password: str) -> None:
        self.session = session
        self.username = username
        self._password = password
        self.access_token = ""
        self.user_id = 0
        self.account_id = ""
        self.user_position = ""
        self.billing_url = API_BASE
        self.account_metadata: dict[str, Any] = {}
        self.devices: dict[str, CloudDevice] = {}
        self.device_list_complete = False
        self.last_device_list_result: int | None = None
        self.partial_device_list_responses = 0
        self.device_removal_checks = 0
        self.empty_catalog_recoveries = 0
        self._devices_lock = asyncio.Lock()
        self._devices_generation = 0
        self._auth_lock = asyncio.Lock()
        self._auth_generation = 0
        self._credentials_rejected = False
        self._login_at = 0.0
        self._closed = False

    async def _request_json(
        self, url: str, payload: dict[str, Any] | None, *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Bounded requests; never include payloads/URLs with tokens in errors."""
        if self._closed:
            raise CloudError("Cloud session is closed")
        try:
            async with self.session.request(
                "POST" if payload is not None else "GET", url,
                data=json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8") if payload is not None else None,
                headers={"Content-Type": "application/json; charset=utf-8"},
                params=params, timeout=REQUEST_TIMEOUT, allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise AuthError("Cloud authorization was rejected")
                if response.status != 200:
                    raise CloudError(f"Cloud service returned HTTP {response.status}")
                # Chunk iteration bounds memory even for chunked responses.
                body = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    body.extend(chunk)
                    if len(body) > MAX_JSON_BYTES:
                        raise CloudError("Cloud response exceeds the size limit")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise CloudError("Cloud service returned an unexpected JSON document")
            return value
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError, UnicodeError) as err:
            raise CloudError("Cannot communicate with the V380 cloud service") from None

    @staticmethod
    def _check_result(response: dict[str, Any], *, login: bool = False, allow_partial_devices: bool = False) -> None:
        # These exact credential errors are mapped by LoginActivity.
        result = response.get("result")
        error = response.get("error_code", 0)
        if result == 0 and error == 0:
            return
        if error == 21011:
            raise AuthError("V380 account authorization has expired")
        if login and error in (21005, 21006):
            raise AuthError("V380 rejected the account username or password")
        # Token failures require reauthentication; unknown vendor codes remain
        # service errors, rather than repeatedly prompting for a password.
        if result in (10005, 10007) and allow_partial_devices:
            return  # app also processes the available owned/shared lists.
        raise CloudError("V380 rejected the request (result %s, error %s)" % (
            result if isinstance(result, int) else "missing",
            error if isinstance(error, int) else "invalid",
        ))

    async def async_login(self) -> str:
        async with self._auth_lock:
            self._credentials_rejected = False
            return await self._login_locked()

    async def _login_locked(self) -> str:
        """Sign in with saved credentials while the caller owns _auth_lock."""
        if self._credentials_rejected:
            raise AuthError("V380 rejected the saved account credentials")
        try:
            response = await self._request_json(API_BASE + "user/login", login_request(
                self.username, self._password, int(time.time())
            ))
            self._check_result(response, login=True)
        except AuthError:
            self._credentials_rejected = True
            raise
        if self._closed:
            raise CloudError("Cloud session is closed")
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            raise CloudError("Cloud login did not return an access token")
        uid = data.get("user_id")
        if type(uid) is not int or uid <= 0 or not data["access_token"]:
            raise CloudError("Cloud login did not return a valid account identity")
        if self.account_id and self.account_id != str(uid):
            self._credentials_rejected = True
            raise AuthError("The cloud session changed account identity")
        self.user_id = uid
        self.account_id = str(uid)
        self.access_token = data["access_token"]
        self.user_position = str(response.get("user_position") or "")
        self.account_metadata = data
        self._login_at = time.monotonic()
        self._auth_generation += 1
        return self.account_id

    async def _ensure_login(self) -> None:
        async with self._auth_lock:
            if self._closed:
                raise CloudError("Cloud session is closed")
            if not self.access_token:
                await self._login_locked()

    async def _recover_login(self, generation: int) -> None:
        """Refresh saved credentials once; concurrent callers share the login."""
        async with self._auth_lock:
            if self._closed:
                raise CloudError("Cloud session is closed")
            if self.access_token and self._auth_generation != generation:
                return
            self.access_token = ""
            await self._login_locked()

    async def async_devices(self) -> list[CloudDevice]:
        """Commit one verified catalog; concurrent refreshes share its result."""
        generation = self._devices_generation
        async with self._devices_lock:
            if self._closed:
                raise CloudError("Cloud session is closed")
            if self._devices_generation != generation:
                return list(self.devices.values())
            previous = dict(self.devices)
            try:
                devices, complete = await self._device_snapshot(previous)
                missing = previous.keys() - devices.keys()
                if missing:
                    self.device_removal_checks = min(self.device_removal_checks + 1, 65535)
                    if not devices:
                        # Reloading HA restores some stale cloud sessions. Do
                        # that bounded recovery here before confirming an empty
                        # catalog, without treating network errors as bad auth.
                        self.empty_catalog_recoveries = min(self.empty_catalog_recoveries + 1, 65535)
                        await self._recover_login(self._auth_generation)
                    confirmed, complete = await self._device_snapshot({**previous, **devices})
                    # A camera absent only in the second reply is not a
                    # confirmed removal. Keep it until a subsequent refresh.
                    unconfirmed = (previous.keys() - confirmed.keys()) - missing
                    confirmed.update((uid, previous[uid]) for uid in unconfirmed)
                    devices = confirmed
                    complete = complete and not unconfirmed
                if self._closed:
                    raise CloudError("Cloud session is closed")
            except CloudError:
                self.device_list_complete = False
                raise
            self.devices = devices
            self.device_list_complete = bool(complete)
            self._devices_generation += 1
            return list(devices.values())

    async def _device_snapshot(
        self, previous: dict[str, CloudDevice]
    ) -> tuple[dict[str, CloudDevice], bool]:
        response = await self._device_response()
        devices, complete = self._merge_devices(response, previous)
        result = response.get("result")
        self.last_device_list_result = result if type(result) is int and result in (0, 10005, 10007) else None
        if not complete:
            self.partial_device_list_responses = min(self.partial_device_list_responses + 1, 65535)
        return devices, complete

    async def _device_response(self) -> dict[str, Any]:
        """Request a catalog, retaining the existing one through token recovery."""
        for attempt in range(2):
            await self._ensure_login()
            generation = self._auth_generation
            try:
                response = await self._request_json(API_BASE + "device/list", devices_request(
                    self.access_token, int(time.time())
                ))
                if self._closed:
                    raise CloudError("Cloud session is closed")
                self._check_result(response, allow_partial_devices=True)
                break
            except AuthError:
                if attempt:
                    raise
                await self._recover_login(generation)
        if response.get("renew_token") is True:
            await self._renew_token(generation)
        return response

    @staticmethod
    def _merge_devices(
        response: dict[str, Any], previous: dict[str, CloudDevice]
    ) -> tuple[dict[str, CloudDevice], bool]:
        """Missing/partial sections update known cameras but cannot remove them."""
        rows = response.get("data")
        shared = response.get("data_share")
        partial = response.get("result") in (10005, 10007)
        if (
            rows is not None and not isinstance(rows, list)
            or shared is not None and not isinstance(shared, list)
        ):
            raise CloudError("Cloud device list has an unsupported shape")
        if rows is None and shared is None and not (partial and previous):
            raise CloudError("Cloud device list did not contain a camera catalog")
        owned_complete = not partial and isinstance(rows, list)
        shared_complete = not partial and isinstance(shared, list)
        normalized_shared = []
        for item in shared or []:
            try:
                normalized_shared.append(normalize_shared_device(item))
            except (ValueError, TypeError, AttributeError):
                # DeviceManager.parseShareDevice also drops malformed shares.
                # A dropped row cannot confirm that a known share was removed.
                shared_complete = False
                continue
        updates: dict[str, CloudDevice] = {}
        for item in rows or []:
            if not isinstance(item, dict):
                raise CloudError("Cloud device list contains an invalid device")
            uid = item.get("device_id")
            if type(uid) is not int or uid <= 0:
                raise CloudError("Cloud device list contains an invalid device ID")
            metadata = {**item, "shared": False}
            updates[str(uid)] = CloudDevice(str(uid), str(item.get("nickname") or f"V380 {uid}"), metadata=metadata)
        for item in normalized_shared:
            uid = item["device_id"]
            updates.setdefault(str(uid), CloudDevice(str(uid), str(item.get("nickname") or f"V380 {uid}"), metadata=item))
        devices = {
            uid: device for uid, device in previous.items()
            if not (shared_complete if device.metadata.get("shared") is True else owned_complete)
        }
        devices.update(updates)
        return devices, owned_complete and shared_complete

    async def _renew_token(self, generation: int | None = None) -> None:
        async with self._auth_lock:
            if self._closed:
                raise CloudError("Cloud session is closed")
            if generation is not None and self._auth_generation != generation:
                return
            timestamp = int(time.time())
            try:
                response = await self._request_json(API_BASE + "user/get-new-token", {
                    "accesstoken": self.access_token,
                    "timestamp": timestamp,
                    "sign": md5(f"accesstoken={self.access_token}&timestamp={timestamp}{_APP_SIGN_SUFFIX}"),
                })
                self._check_result(response)
            except AuthError:
                self.access_token = ""
                await self._login_locked()
                return
            if self._closed:
                raise CloudError("Cloud session is closed")
            token = response.get("access_token")
            if not isinstance(token, str) or not token:
                raise CloudError("Cloud token renewal has an unsupported response")
            self.access_token = token
            self._auth_generation += 1

    async def async_close(self) -> None:
        self._closed = True
        self.access_token = ""
        self.devices.clear()
        self.device_list_complete = False
        self.last_device_list_result = None
        self.account_metadata.clear()
        self._password = ""
