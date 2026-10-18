"""Real loopback HTTP tests of recovered account requests; no vendor traffic."""
from __future__ import annotations
import asyncio
import base64
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "v380_cloud"
_PACKAGE = "_v380_account_test_protocol"
pkg = types.ModuleType(_PACKAGE);pkg.__path__ = [str(_COMPONENT)];sys.modules[_PACKAGE] = pkg
account = importlib.import_module(f"{_PACKAGE}.account")


class AccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.renew = False
        self.auth_error = False
        self.broken = False
        async def handler(request):
            data = await request.json()
            self.requests.append((request.path, data))
            if request.path == "/user/login":
                if self.auth_error:
                    return web.json_response({"result": -1, "error_code": 21006})
                return web.json_response({"result": 0, "error_code": 0, "user_position": "EU",
                    "data": {"access_token": "fixture-token", "user_id": 42, "username": "fixture"}})
            if request.path == "/user/get-new-token":
                return web.json_response({"result": 0, "error_code": 0, "access_token": "renewed-token"})
            if self.broken:
                return web.json_response({"unexpected": True})
            return web.json_response({"result": 0, "error_code": 0, "renew_token": self.renew,
                "data": [{"device_id": 12345, "nickname": "Front", "device_password": "c2VjcmV0", "type": 1}],
                "data_share": [{"device_id": 12345, "nickname": "Duplicate"},
                               {"device_id": 67890, "nickname": "Shared"}]})
        app = web.Application();app.router.add_post("/{path:.*}", handler)
        self.runner = web.AppRunner(app);await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0);await self.site.start()
        self.base = f"http://127.0.0.1:{self.site._server.sockets[0].getsockname()[1]}/"
        self.patch = patch.object(account, "API_BASE", self.base);self.patch.start()
        self.session = aiohttp.ClientSession()
        self.client = account.AccountClient(self.session, "fixture", "fixture-password")

    async def asyncTearDown(self):
        await self.client.async_close();await self.session.close();await self.runner.cleanup();self.patch.stop()

    async def test_login_wire_and_device_enumeration(self):
        with patch.object(account.time, "time", return_value=1700000000):
            self.assertEqual(await self.client.async_login(), "42")
            devices = await self.client.async_devices()
        path, request = self.requests[0]
        self.assertEqual(path, "/user/login")
        # Ciphertext independently generated with OpenSSL AES128ECB+PKCS7.
        self.assertEqual(request["password"], "f0a94927d1d8c0ed2af723decc007160050187a0cde5a9872cbab091ab73e553")
        self.assertEqual(request["phonetoken"], "100")
        self.assertEqual(request["timestamp"], 1700000000)
        self.assertNotIn("fixture-password", str(request))
        self.assertEqual([d.device_id for d in devices], ["12345", "67890"])
        self.assertEqual(devices[0].name, "Front")
        self.assertIsNone(devices[0].online)
        self.assertNotIn("device_password", repr(devices[0]))
        self.assertEqual(self.client.user_position, "EU")
        self.assertEqual(self.requests[1][1]["get_sub_server"], 10)
        self.assertEqual(self.requests[1][1]["registrationid"], "")
        self.assertEqual(account.device_password(devices[0].metadata), "secret")

    async def test_version_two_device_password_uses_aes(self):
        # OpenSSL AES128ECB vector with key12345678hongshi1. The DEX branch
        # compares ver to2 (BaseActivity$55 offset0x108 ->0x8d4).
        self.assertEqual(account.device_password({
            "type": 1, "ver": 2, "rand_key": "12345678",
            "device_password": "SGzE7BB1F/WuyL2r1Gbk2A==",
        }), "camera-password")
        self.assertEqual(account.device_password({
            "type": 1, "ver": 1, "device_password": "cGxhaW4=",
        }), "plain")

    async def test_renewal_uses_top_level_access_token(self):
        self.renew = True
        await self.client.async_devices()
        self.assertEqual(self.client.access_token, "renewed-token")
        self.assertEqual(self.requests[-1][0], "/user/get-new-token")
        self.assertEqual(self.requests[-1][1]["accesstoken"], "fixture-token")

    async def test_rejected_credentials_and_malformed_service(self):
        self.auth_error = True
        with self.assertRaises(account.AuthError):await self.client.async_login()
        self.auth_error = False;self.broken = True
        with self.assertRaises(account.CloudError):await self.client.async_devices()
        with self.assertRaises(account.AuthError):
            account.AccountClient._check_result({"result": -1, "error_code": 21011})

    async def test_closed_session_and_newer_platform(self):
        await self.client.async_close()
        self.assertEqual(self.client.access_token, "")
        with self.assertRaises(account.CloudError):await self.client.async_login()
        with self.assertRaises(account.UnsupportedError):
            account.device_password({"type": 2, "rk": "encrypted"})
        with self.assertRaises(account.CloudError):
            account.device_password({"type": 1, "device_password": "!bad-base64!"})
