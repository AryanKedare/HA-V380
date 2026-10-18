#!/usr/bin/env python3
"""V380 cloud and optional Home Assistant diagnostic (Python 3.11+).

Prompts for passwords/tokens locally. Tests every discovered camera by default.
Does not change device configuration. Only --save-media writes private footage.
The JSON report excludes account/camera names, IDs, tokens and playback URLs.
"""
from __future__ import annotations

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
