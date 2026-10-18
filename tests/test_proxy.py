"""Verify HA media authorization and proxy boundaries without vendor traffic."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlencode, urlsplit

from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import jwt
import pytest

pytest.importorskip("homeassistant")
from homeassistant import auth
from homeassistant.components.http.auth import DATA_SIGN_SECRET, async_setup_auth
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers.http import KEY_HASS, request_handler_factory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.v380_cloud.const import DOMAIN
from custom_components.v380_cloud.proxy import (
    V380LiveView,
    V380RecordingView,
    V380SegmentView,
    _stream_download,
    signed_media_url,
)
from custom_components.v380_cloud.recordings import RecordingSegment, RecordingSource


@asynccontextmanager
async def _hass_and_app(tmp_path):
    """Use actual HA users, signing and authentication middleware."""
    hass = HomeAssistant(str(tmp_path))
    hass.config.internal_url = "http://ha.example.invalid:8123"
    hass.config.external_url = "https://ha.example.invalid"
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    hass.auth = await auth.auth_manager_from_config(hass, [], [])
    app = web.Application()
    app[KEY_HASS] = hass
    await async_setup_auth(hass, app)
    try:
        yield hass, app
    finally:
        await hass.async_stop(force=True)


def _request(app, url, *, entry_id="entryfixture", device_id="1234", recording_id="clipfixture"):
    parsed = urlsplit(url)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    return make_mocked_request(
        "GET",
        path,
        app=app,
        match_info={"entry_id": entry_id, "device_id": device_id, "recording_id": recording_id},
    )


def _cloud_client():
    return SimpleNamespace(
        register_media_task=Mock(),
        unregister_media_task=Mock(),
        async_recording_download=AsyncMock(
            return_value=RecordingSource(
                mime_type="application/vnd.apple.mpegurl",
                filename="fixture.m3u8",
                segments=(
                    RecordingSegment("https://vendor.example.invalid/part1.ts?access_token=vendor-secret", 4.5),
                    RecordingSegment("https://vendor.example.invalid/part2.ts?access_token=vendor-secret", 3.0),
                ),
            )
        ),
    )


def test_hls_uses_scoped_signed_ha_paths_without_vendor_tokens(tmp_path):
    async def run():
        async with _hass_and_app(tmp_path) as (hass, app):
            client = _cloud_client()
            hass.data[DOMAIN] = {
                "entryfixture": SimpleNamespace(client=client, coordinator=SimpleNamespace(data={"1234": object()}))
            }
            url = signed_media_url(hass, "entryfixture", "recording", "1234", "clipfixture")
            assert url.startswith("https://ha.example.invalid/api/v380_cloud/recording/")
            assert "vendor-secret" not in url
            token = parse_qs(urlsplit(url).query)["authSig"][0]
            claims = jwt.decode(token, hass.data[DATA_SIGN_SECRET], algorithms=["HS256"])
            assert claims["path"] == "/api/v380_cloud/recording/entryfixture/1234/clipfixture"
            assert claims["params"] == []
            assert claims["exp"] - claims["iat"] == 10 * 60

            view = V380RecordingView()
            handler = request_handler_factory(hass, view, view.get)
            response = await app.middlewares[0](_request(app, url), handler)
            assert response.status == 200
            assert response.content_type == "application/vnd.apple.mpegurl"
            assert "vendor.example.invalid" not in response.text
            assert "vendor-secret" not in response.text
            assert "access_token" not in response.text
            segment_urls = [line for line in response.text.splitlines() if line.startswith("/")]
            assert len(segment_urls) == 2
            for index, segment_url in enumerate(segment_urls):
                parsed = urlsplit(segment_url)
                expected = f"/api/v380_cloud/segment/entryfixture/1234/clipfixture/{index}"
                assert parsed.path == expected
                segment_token = parse_qs(parsed.query)["authSig"][0]
                segment_claims = jwt.decode(segment_token, hass.data[DATA_SIGN_SECRET], algorithms=["HS256"])
                assert segment_claims["path"] == expected
                assert segment_claims["exp"] - segment_claims["iat"] == 30 * 60
            client.async_recording_download.assert_awaited_once_with("1234", "clipfixture")
            client.unregister_media_task.assert_called_once_with()

            # A valid signature authorizes its exact camera/recording path only.
            tampered = url.replace("/1234/", "/5678/")
            with pytest.raises(web.HTTPUnauthorized):
                await app.middlewares[0](_request(app, tampered, device_id="5678"), handler)
            assert client.async_recording_download.await_count == 1

            # Expiration is enforced by HA's real authentication middleware.
            claims["exp"] = claims["iat"] - 1
            expired = urlsplit(url).path + "?" + urlencode({"authSig": jwt.encode(claims, hass.data[DATA_SIGN_SECRET], algorithm="HS256")})
            with pytest.raises(web.HTTPUnauthorized):
                await app.middlewares[0](_request(app, expired), handler)
    asyncio.run(run())


@pytest.mark.parametrize("view_type", [V380LiveView, V380RecordingView, V380SegmentView])
def test_media_routes_reject_unauthenticated_requests(tmp_path, view_type):
    async def run():
        async with _hass_and_app(tmp_path) as (hass, app):
            view = view_type()
            handler = request_handler_factory(hass, view, view.get)
            with pytest.raises(web.HTTPUnauthorized):
                await app.middlewares[0](_request(app, "/api/v380_cloud/unauthorized"), handler)
    asyncio.run(run())


def test_signed_media_rejects_camera_outside_account(tmp_path):
    async def run():
        async with _hass_and_app(tmp_path) as (hass, app):
            client = _cloud_client()
            hass.data[DOMAIN] = {
                "entryfixture": SimpleNamespace(client=client, coordinator=SimpleNamespace(data={"1234": object()}))
            }
            url = signed_media_url(hass, "entryfixture", "recording", "5678", "clipfixture")
            view = V380RecordingView()
            handler = request_handler_factory(hass, view, view.get)
            with pytest.raises(web.HTTPNotFound):
                await app.middlewares[0](_request(app, url, device_id="5678"), handler)
            client.async_recording_download.assert_not_awaited()
    asyncio.run(run())


def test_download_preserves_byte_ranges_and_partial_content():
    async def run():
        async def chunks(size):
            assert size == 65536
            yield b"da"
            yield b"ta"

        upstream = SimpleNamespace(
            status=206,
            headers={"Content-Range": "bytes 100-103/200", "Content-Length": "4", "Accept-Ranges": "bytes"},
            content=SimpleNamespace(iter_chunked=chunks),
        )

        @asynccontextmanager
        async def upstream_response():
            yield upstream

        get = Mock(return_value=upstream_response())
        client = SimpleNamespace(session=SimpleNamespace(get=get))
        request = make_mocked_request("GET", "/recording", headers={"Range": "bytes=100-103"})
        writer = request._payload_writer
        response = await _stream_download(request, client, "https://vendor.example.invalid/private.mp4?token=vendor-secret", "video/mp4")
        assert response.status == 206
        assert response.headers["Content-Range"] == "bytes 100-103/200"
        assert response.headers["Content-Length"] == "4"
        assert response.headers["Accept-Ranges"] == "bytes"
        assert response.headers["Cache-Control"] == "no-store"
        assert get.call_args.kwargs["headers"] == {"Range": "bytes=100-103"}
        assert get.call_args.kwargs["allow_redirects"] is False
        assert b"".join(call.args[0] for call in writer.write.call_args_list) == b"data"
    asyncio.run(run())


def test_download_rejects_invalid_ranges_before_vendor_request():
    async def run():
        get = Mock()
        client = SimpleNamespace(session=SimpleNamespace(get=get))
        request = make_mocked_request("GET", "/recording", headers={"Range": "items=0-2"})
        with pytest.raises(web.HTTPBadRequest):
            await _stream_download(request, client, "https://vendor.example.invalid/private.mp4", "video/mp4")
        get.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize("body,headers", [
    (b'{"error":"expired-token"}', {}),
    (b'permission denied', {"Content-Type": "text/plain"}),
])
def test_download_rejects_http200_error_documents(body, headers):
    async def run():
        async def chunks(size):
            yield body
        upstream = SimpleNamespace(status=200, headers=headers, content=SimpleNamespace(iter_chunked=chunks))
        @asynccontextmanager
        async def upstream_response():
            yield upstream
        client = SimpleNamespace(session=SimpleNamespace(get=Mock(return_value=upstream_response())))
        request = make_mocked_request("GET", "/recording")
        from custom_components.v380_cloud.errors import CloudError
        with pytest.raises(CloudError, match="instead of video"):
            await _stream_download(request, client, "https://vendor.example.invalid/recording", "video/mp4")
        request._payload_writer.write.assert_not_called()
    asyncio.run(run())
