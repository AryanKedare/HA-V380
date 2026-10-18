"""Authenticated HA media routes; vendor credentials stay in the backend."""
from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
import time
import re
from urllib.parse import quote

from aiohttp import ClientError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import get_url
from homeassistant.helpers.http import KEY_HASS

from .const import DOMAIN
from .errors import AuthError, CloudError, UnsupportedError
from .mpegts import MpegTsMuxer


def signed_media_url(
    hass: HomeAssistant, entry_id: str, kind: str, device_id: str, recording_id: str = ""
) -> str:
    parts = [entry_id, device_id] + ([recording_id] if kind == "recording" else [])
    path = f"/api/{DOMAIN}/{kind}/" + "/".join(quote(part, safe="") for part in parts)
    signed = async_sign_path(hass, path, timedelta(minutes=10), use_content_user=True)
    return get_url(hass, prefer_external=kind == "recording") + signed


def _client(hass: HomeAssistant, entry_id: str, device_id: str):
    runtime = hass.data.get(DOMAIN, {}).get(entry_id)
    if runtime is None or device_id not in runtime.coordinator.data:
        raise web.HTTPNotFound()
    return runtime.client


class V380LiveView(HomeAssistantView):
    url = f"/api/{DOMAIN}/live/{{entry_id}}/{{device_id}}"
    name = f"api:{DOMAIN}:live"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, device_id: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        client = _client(hass, entry_id, device_id)
        client.register_media_task()
        frames = client.async_live_frames(device_id)
        response = web.StreamResponse(headers={"Content-Type": "video/mp2t", "Cache-Control": "no-store"})
        try:
            # Wait for a video frame before committing HTTP200. Some cameras
            # send telemetry or audio first. A failed relay must be a real error.
            async with asyncio.timeout(60):
                while True:
                    first = await anext(frames)
                    if first.codec in ("h264", "hevc", "h265"):
                        break
            mux = MpegTsMuxer(first.codec)
            await response.prepare(request)
            # The raw SDK clock unit differs by firmware; arrival time keeps a
            # valid monotonic timeline without inventing a unit for that field.
            await response.write(mux.push(first.payload, time.monotonic(), keyframe=first.keyframe))
            async for frame in frames:
                if frame.codec not in ("h264", "hevc", "h265"):
                    continue
                if frame.codec != first.codec:
                    raise UnsupportedError("The cloud camera changed video codec")
                await response.write(mux.push(frame.payload, time.monotonic(), keyframe=frame.keyframe))
        except AuthError:
            if entry := hass.config_entries.async_get_entry(entry_id):
                entry.async_start_reauth(hass)
            if not response.prepared:
                raise web.HTTPBadGateway(text="V380 camera authentication failed") from None
        except UnsupportedError:
            if not response.prepared:
                raise web.HTTPNotImplemented(text="This V380 cloud video protocol is not supported") from None
        except (CloudError, ClientError, TimeoutError, StopAsyncIteration, ValueError):
            if not response.prepared:
                raise web.HTTPBadGateway(text="V380 cloud video could not start") from None
        except ConnectionError:
            pass  # HA/browser stopped watching.
        finally:
            await frames.aclose()
            client.unregister_media_task()
        return response


async def _stream_download(request: web.Request, client, url: str, mime: str) -> web.StreamResponse:
    headers = {}
    if "Range" in request.headers:
        value = request.headers["Range"]
        if len(value) > 128 or re.fullmatch(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", value) is None:
            raise web.HTTPBadRequest(text="Invalid byte range")
        headers["Range"] = value
    async with client.session.get(
        url, headers=headers, allow_redirects=False,
        timeout=ClientTimeout(total=None, connect=15, sock_read=45),
    ) as upstream:
        if upstream.status not in (200, 206, 416):
            raise CloudError("The V380 recording download failed")
        chunks = upstream.content.iter_chunked(65536)
        first = await anext(chunks, b"")
        if upstream.status in (200, 206):
            content_type = upstream.headers.get("Content-Type", "").lower()
            start = upstream.headers.get("Content-Range", "bytes 0-").startswith("bytes 0-")
            if any(value in content_type for value in ("application/json", "text/html", "text/plain")) or (
                start and first.lstrip().startswith((b"{", b"[", b"<"))
            ):
                raise CloudError("V380 returned an error document instead of video")
        out_headers = {"Cache-Control": "no-store", "Content-Type": mime}
        for name in ("Content-Range", "Content-Length", "Accept-Ranges"):
            if name in upstream.headers:
                out_headers[name] = upstream.headers[name]
        response = web.StreamResponse(status=upstream.status, headers=out_headers)
        await response.prepare(request)
        try:
            if first:
                await response.write(first)
            async for chunk in chunks:
                await response.write(chunk)
            await response.write_eof()
        except (ClientError, ConnectionError, TimeoutError):
            # A partial upstream object cannot be changed into HTTP502 once the
            # response has started; close it so the player can retry a range.
            response.force_close()
        return response


class V380RecordingView(HomeAssistantView):
    url = f"/api/{DOMAIN}/recording/{{entry_id}}/{{device_id}}/{{recording_id}}"
    name = f"api:{DOMAIN}:recording"
    requires_auth = True

    async def get(
        self, request: web.Request, entry_id: str, device_id: str, recording_id: str
    ) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        client = _client(hass, entry_id, device_id)
        client.register_media_task()
        try:
            source = await client.async_recording_download(device_id, recording_id)
            if source.url is not None:
                return await _stream_download(request, client, source.url, source.mime_type)
            if not source.segments:
                raise UnsupportedError("This V380 recording has no playable media")
            target = max(1, int(max(segment.duration for segment in source.segments)) + 1)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
                     "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
            for index, segment in enumerate(source.segments):
                path = f"/api/{DOMAIN}/segment/" + "/".join(quote(part, safe="") for part in
                    (entry_id, device_id, recording_id, str(index)))
                signed = async_sign_path(hass, path, timedelta(minutes=30), use_content_user=True)
                if index:
                    lines.append("#EXT-X-DISCONTINUITY")
                lines.extend([f"#EXTINF:{segment.duration:.3f},", signed])
            lines.append("#EXT-X-ENDLIST")
            return web.Response(text="\n".join(lines) + "\n",
                                content_type="application/vnd.apple.mpegurl",
                                headers={"Cache-Control": "no-store"})
        except AuthError:
            if entry := hass.config_entries.async_get_entry(entry_id):
                entry.async_start_reauth(hass)
            raise web.HTTPBadGateway(text="V380 cloud authorization expired") from None
        except UnsupportedError:
            raise web.HTTPNotImplemented(text="This V380 recording protocol is not supported") from None
        except (CloudError, ClientError, TimeoutError):
            raise web.HTTPBadGateway(text="V380 cloud recording could not be downloaded") from None
        finally:
            client.unregister_media_task()


class V380SegmentView(HomeAssistantView):
    url = f"/api/{DOMAIN}/segment/{{entry_id}}/{{device_id}}/{{recording_id}}/{{index}}"
    name = f"api:{DOMAIN}:segment"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, device_id: str,
                  recording_id: str, index: str) -> web.StreamResponse:
        client = _client(request.app[KEY_HASS], entry_id, device_id)
        client.register_media_task()
        try:
            if not index.isdecimal() or len(index) > 6:
                raise web.HTTPNotFound()
            source = await client.async_recording_download(device_id, recording_id)
            position = int(index)
            if position >= len(source.segments):
                raise web.HTTPNotFound()
            segment = source.segments[position]
            return await _stream_download(request, client, segment.url, segment.mime_type)
        except AuthError:
            hass = request.app[KEY_HASS]
            if entry := hass.config_entries.async_get_entry(entry_id):
                entry.async_start_reauth(hass)
            raise web.HTTPBadGateway(text="V380 cloud authorization expired") from None
        except UnsupportedError:
            raise web.HTTPNotImplemented(text="This V380 recording protocol is not supported") from None
        except (CloudError, ClientError, TimeoutError):
            raise web.HTTPBadGateway(text="V380 recording segment could not be downloaded") from None
        finally:
            client.unregister_media_task()


def register_views(hass: HomeAssistant) -> None:
    if not hass.data.get(f"{DOMAIN}_http_registered"):
        hass.http.register_view(V380LiveView())
        hass.http.register_view(V380RecordingView())
        hass.http.register_view(V380SegmentView())
        hass.data[f"{DOMAIN}_http_registered"] = True
