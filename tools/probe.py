#!/usr/bin/env python3
"""Interactive cloud check. Never accepts passwords on the command line.

This contacts only the V380 services and camera cloud relays for the account
entered interactively. It does not change camera configuration or save footage.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import importlib
import json
from pathlib import Path
import sys
import types

try:
    import aiohttp
except ImportError:
    raise SystemExit("Install aiohttp and cryptography in a virtual environment first.")

component = Path(__file__).resolve().parents[1] / "custom_components" / "v380_cloud"
package = types.ModuleType("_v380_probe")
package.__path__ = [str(component)]
sys.modules[package.__name__] = package
account = importlib.import_module("_v380_probe.account")
live = importlib.import_module("_v380_probe.live")
recordings = importlib.import_module("_v380_probe.recordings")
errors = importlib.import_module("_v380_probe.errors")


async def run(args):
    username = input("V380 cloud email/phone account: ").strip()
    password = getpass.getpass("V380 cloud account password: ")
    report = {"account_login": "not_checked", "camera_count": 0, "cameras": []}
    async with aiohttp.ClientSession() as session:
        client = account.AccountClient(session, username, password)
        try:
            await client.async_login()
            devices = await client.async_devices()
            report["account_login"] = "accepted"
            report["camera_count"] = len(devices)
            for index, device in enumerate(devices, 1):
                item = {"index": index, "cloud_live_protocol": "new_key_exchange_unsupported"
                        if int(device.metadata.get("type", 0)) >= 2 else "legacy_relay_implemented"}
                report["cameras"].append(item)
                if index != args.camera:
                    continue
                if args.test_live:
                    transport = None
                    frames = None
                    try:
                        transport = live.CloudLiveClient(device.metadata, account.device_password(device.metadata), audio=False)
                        frames = transport.async_frames()
                        async with asyncio.timeout(90):
                            async for frame in frames:
                                if frame.codec in ("h264", "hevc") and frame.keyframe:
                                    item["live"] = {"decrypted_keyframe_received": True, "codec": frame.codec,
                                                    "bytes": len(frame.payload)}
                                    break
                            else:
                                item["live"] = {"decrypted_keyframe_received": False}
                    except errors.CloudError as exc:
                        item["live"] = {"status": type(exc).__name__, "message": str(exc)}
                    except (OSError, TimeoutError):
                        item["live"] = {"status": "connection_failed_or_timed_out"}
                    finally:
                        if frames is not None:
                            await frames.aclose()
                        if transport is not None:
                            await transport.async_close()
                if args.test_recordings:
                    try:
                        catalog = recordings.CloudRecordings(client, allow_insecure_http=args.allow_insecure_http)
                        dates = await catalog.async_dates(device.device_id)
                        result = {"date_count": len(dates)}
                        item["recordings"] = result
                        if dates:
                            clips = await catalog.async_recordings(device.device_id, dates[0])
                            result["latest_day_clip_count"] = len(clips)
                            if clips:
                                source = await catalog.async_source(device.device_id, clips[0].recording_id)
                                url = source.url or (source.segments[0].url if source.segments else None)
                                if url:
                                    async with session.get(url, headers={"Range": "bytes=0-4095"}, allow_redirects=False,
                                                           timeout=aiohttp.ClientTimeout(total=30)) as response:
                                        data = await response.content.read(4096)
                                        result["sample_http_status"] = response.status
                                        result["sample_container"] = (
                                            "mp4" if b"ftyp" in data[:32] else
                                            "mpegts" if data[:1] == b"\x47" else "unrecognized"
                                        )
                    except errors.CloudError as exc:
                        item["recordings"] = {"status": type(exc).__name__, "message": str(exc)}
                    except (aiohttp.ClientError, TimeoutError):
                        item["recordings"] = {"status": "connection_failed_or_timed_out"}
        except errors.CloudError as exc:
            report["account_login"] = type(exc).__name__
            report["message"] = str(exc)
        finally:
            await client.async_close()
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, default=1, help="Camera index to check (default: 1)")
    parser.add_argument("--test-live", action="store_true", help="Fetch one remote video keyframe")
    parser.add_argument("--test-recordings", action="store_true", help="Check newest cloud recording and a small media sample")
    parser.add_argument("--allow-insecure-http", action="store_true", help="Permit vendor HTTP recording requests carrying cloud tokens")
    args = parser.parse_args()
    if args.camera < 1:
        parser.error("--camera must be positive")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        raise SystemExit("Stopped")
    except Exception as exc:
        # Unexpected failures still must not print credentials or private URLs.
        raise SystemExit(f"Probe failed ({type(exc).__name__}); no credential details printed.") from None


if __name__ == "__main__":
    main()
