"""Offline recording protocol fixtures; no vendor requests or real credentials."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

_COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "v380_cloud"
_PACKAGE = "_v380_recording_test_protocol"
if _PACKAGE not in sys.modules:
    package = types.ModuleType(_PACKAGE)
    package.__path__ = [str(_COMPONENT)]
    sys.modules[_PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{_PACKAGE}.recordings", _COMPONENT / "recordings.py")
recordings = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recordings
spec.loader.exec_module(recordings)


class FakeAPI:
    access_token = "fixture-token"
    user_id = 77
    user_position = "IE"
    billing_url = "https://billing.example.com/"
    _check_result = staticmethod(recordings.AccountClient._check_result)

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.devices = {"7": object(), "8": object()}

    async def _request_json(self, url, payload, *, params=None):
        self.calls.append((url, payload, params))
        if not self.responses:
            raise AssertionError("Unexpected network operation")
        return self.responses.pop(0)


LEGACY_SERVICE = {"result": 0, "error_code": 0, "bind_status": 1,
                  "bucket": {"bucket_index": 11, "list": "https://list.example.com",
                             "download": "https://video.example.com"}}
LEGACY_ROW = {"id": 901, "user_id": 77, "device_id": 7, "channel": 0,
              "date": 20261010, "time": 123456, "rand_num": 123,
              "bucket_index": 11, "file_name": "20261010123456.mp4"}
S3_SERVICE = {"result": 0, "error_code": 0, "s3_url": "https://token.example.com",
              "buck_list": [{"id": 11}, {"id": 11}, {"id": 12}]}
S3_ROW = {"bdx": 11, "bnm": "video-bucket", "zn": 0, "dst": 0,
          "pth": "7/20261010/", "fth": "clip.m3u8",
          "slf": [{"ste": 1791633600000, "ede": 1791633605000,
                   "fne": "one.ts", "rte": 1},
                  {"ste": 1791633605000, "ede": 1791633610000,
                   "fne": "two.ts", "rte": 2}]}
S3_CREDENTIAL = {"id": 11, "ak": "fixture-access-key", "sk": "fixture-secret-key",
                 "token": "fixture-session-token", "exp": 4102444800,
                 "host": "https://storage.example.com", "scheme": "https",
                 "name": "video-bucket", "region": "eu-west-1", "btype": "s3"}


class SigningTests(unittest.TestCase):
    def test_service_request_matches_bytecode_canonical_order(self):
        payload = recordings.service_request("token", 77, 7, "IE", 1000)
        expected = ("accesstoken=token&deviceid=7&timestamp=1000&user_id=77"
                    "&user_position=IE&ver=1008hsshop2016")
        self.assertEqual(payload["sign"], hashlib.md5(expected.encode()).hexdigest())

    def test_s3_list_signing_and_date_encoding(self):
        payload = recordings.s3_list_request("token", 77, 7, [11, 11, 12], 1000,
                                               day="2026-10-10")
        expected = ('access_token=token&date=20261010&device_id=7&firm_list='
                    '[{"bucket_index":11},{"bucket_index":12}]&timestamp=1000'
                    '&type=list&user_id=77hongshi2025s3')
        self.assertEqual(payload["sign"], hashlib.md5(expected.encode()).hexdigest())
        self.assertEqual(payload["date"], "20261010")
        self.assertNotIn("cursor", payload)

    def test_s3_presigning_matches_independent_botocore_implementation(self):
        try:
            from botocore.auth import S3SigV4QueryAuth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials
        except ImportError:
            self.skipTest("botocore is only needed for this independent signing comparison")
        when = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)
        credential = dict(S3_CREDENTIAL)
        key = "folder/a file+name.ts"
        result = recordings.s3_presigned_url(credential, key, now=when)
        request = AWSRequest(method="GET", url="https://storage.example.com/video-bucket/folder/a%20file%2Bname.ts")
        signer = S3SigV4QueryAuth(Credentials(credential["ak"], credential["sk"], credential["token"]),
                                "s3", credential["region"], expires=900)
        with patch("botocore.auth.get_current_datetime", return_value=when):
            signer.add_auth(request)
        self.assertEqual(urlsplit(result).path, urlsplit(request.url).path)
        self.assertEqual(parse_qs(urlsplit(result).query), parse_qs(urlsplit(request.url).query))

    def test_download_parameter_is_plain_base64_and_string_device_id(self):
        result = recordings.ucloud_download_parameter("token", LEGACY_ROW)
        raw = base64.b64decode(result)
        self.assertEqual(raw, b'{"access_token":"token","user_id":77,"device_id":"7",'
                         b'"channel":0,"date":20261010,"time":123456,"rand_num":123}')
        self.assertNotIn("\n", result)

    def test_s3_endpoint_uses_exact_credential_scheme(self):
        credential = dict(S3_CREDENTIAL, host="storage.example.com", scheme="https")
        result = recordings.s3_presigned_url(credential, "one.ts")
        self.assertEqual(urlsplit(result).scheme, "https")
        credential["scheme"] = "http"
        self.assertEqual(urlsplit(recordings.s3_presigned_url(credential, "one.ts")).scheme, "http")
        del credential["scheme"]
        with self.assertRaises(recordings.CloudError):
            recordings.s3_presigned_url(credential, "one.ts")

    def test_s3_explicit_host_uri_retains_its_scheme(self):
        credential = dict(S3_CREDENTIAL, host="https://storage.example.com", scheme="http")
        result = recordings.s3_presigned_url(credential, "one.ts")
        self.assertEqual(urlsplit(result).scheme, "https")


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_service_date_list_and_private_mp4_source(self):
        api = FakeAPI([LEGACY_SERVICE,
                       {"result": 200, "count": 1, "data": [{"date": "2026-10-10"}]},
                       {"result": 200, "count": 1, "data": [LEGACY_ROW]}])
        client = recordings.CloudRecordings(api)
        self.assertEqual(await client.async_dates("7"), ["2026-10-10"])
        url, payload, params = api.calls[1]
        self.assertEqual(url, "https://list.example.com/list.php")
        self.assertIsNone(payload)
        self.assertTrue(params["param"].endswith("\n"))
        date_payload = json.loads(base64.b64decode(params["param"]))
        self.assertEqual(date_payload["type"], 2)
        self.assertEqual(date_payload["pageSize"], 365)
        self.assertNotIn("date", date_payload)
        clips = await client.async_recordings("7", "2026-10-10")
        self.assertEqual(len(clips), 1)
        self.assertEqual(clips[0].title, "12:34:56")
        self.assertEqual(clips[0].mime_type, "video/mp4")
        self.assertRegex(clips[0].recording_id, r"^[0-9a-f]{32}$")
        source = await client.async_source("7", clips[0].recording_id)
        self.assertEqual(source.mime_type, "video/mp4")
        self.assertEqual(urlsplit(source.url).path, "/download.php")
        self.assertNotIn("fixture-token", repr(source))
        encoded = parse_qs(urlsplit(source.url).query)["param"][0]
        self.assertEqual(json.loads(base64.b64decode(encoded))["device_id"], "7")
        with self.assertRaises(recordings.CloudError):
            client.validate_recording("8", clips[0].recording_id)

    async def test_s3_dates_cursor_bucket_selection_and_private_segments(self):
        api = FakeAPI([S3_SERVICE,
                       {"result": 200, "data": {"cursor": 2, "dates": [
                           {"fne": "20261010", "flt": [{"bdx": 11}]}]}},
                       {"result": 200, "data": {"cursor": -1, "dates": [
                           {"fne": "20261009", "flt": [{"bdx": 12}]}]}},
                       {"result": 200, "data": [S3_ROW]},
                       {"code": 7800, "data": {"bucket": [S3_CREDENTIAL]}}])
        client = recordings.CloudRecordings(api, search_base="https://search.example.com")
        self.assertEqual(await client.async_dates("7"), ["2026-10-10", "2026-10-09"])
        self.assertEqual([call[1]["cursor"] for call in api.calls[1:]], [1, 2])
        clips = await client.async_recordings("7", "2026-10-10")
        self.assertEqual(api.calls[3][1]["firm_list"], [{"bucket_index": 11}])
        self.assertEqual(api.calls[3][1]["date"], "20261010")
        with patch.object(recordings.time, "time", return_value=1000):
            source = await client.async_source("7", clips[0].recording_id)
        self.assertIsNone(source.url)
        self.assertEqual(source.mime_type, recordings.HLS_MIME)
        self.assertEqual([part.duration for part in source.segments], [5.0, 5.0])
        self.assertEqual(urlsplit(source.segments[0].url).path,
                         "/video-bucket/7/20261010/one.ts")
        self.assertNotIn("fixture-", repr(source))
        token_url, body, params = api.calls[-1]
        self.assertEqual(token_url, "https://token.example.com/v1/app/gettoken")
        self.assertEqual(body, {"user_token": "fixture-token", "buck_id": [11]})
        body_hash = hashlib.md5(b'{"user_token":"fixture-token","buck_id":[11]}').hexdigest()
        expected_sign = hashlib.md5((body_hash + "GzHong^&*20251000").encode()).hexdigest()
        self.assertEqual(params, {"signature": expected_sign, "timestamp": 1000})
        # Reuses temporary credentials, and public IDs are stable after reloading.
        await client.async_source("7", clips[0].recording_id)
        self.assertFalse(api.responses)
        api.responses.append({"result": 200, "data": [S3_ROW]})
        reloaded = await client.async_recordings("7", "2026-10-10")
        self.assertEqual(reloaded[0].recording_id, clips[0].recording_id)

    async def test_http_is_explicit_and_never_tried_silently(self):
        api = FakeAPI([S3_SERVICE])
        client = recordings.CloudRecordings(api)
        with self.assertRaisesRegex(recordings.UnsupportedError, "HTTP"):
            await client.async_dates("7")
        self.assertEqual(len(api.calls), 1)
        client = recordings.CloudRecordings(api, allow_insecure_http=True)
        self.assertEqual(client._cloud_url("video.example.com"), "http://video.example.com")
        with self.assertRaises(recordings.CloudError):
            client._cloud_url("127.0.0.1")

    async def test_legacy_pagination_and_bucket_specific_download_host(self):
        moved_row = dict(LEGACY_ROW, bucket_index=12)
        api = FakeAPI([LEGACY_SERVICE,
                       {"result": 200, "count": 2, "data": [{"date": "2026-10-10"}]},
                       {"result": 200, "count": 2, "data": [{"date": "2026-10-09"}]},
                       {"result": 200, "data": [moved_row]},
                       {"result": 0, "error_code": 0, "data": {
                           "bucket_index": 12, "download": "https://oldvideo.example.com"}}])
        client = recordings.CloudRecordings(api)
        service = await client._service("7")
        rows = await client._ucloud_catalog(service, 2, page_size=1)
        self.assertEqual(len(rows), 2)
        self.assertEqual([json.loads(base64.b64decode(call[2]["param"]))["page"]
                          for call in api.calls[1:]], [1, 2])
        clips = await client.async_recordings("7", "2026-10-10")
        source = await client.async_source("7", clips[0].recording_id)
        self.assertEqual(urlsplit(source.url).hostname, "oldvideo.example.com")
        self.assertEqual(api.calls[-1][0], "https://billing.example.com/disk/get-host-list")
        self.assertEqual(api.calls[-1][1]["bucketindex"], 12)

    async def test_catalog_error_is_not_misreported_as_no_recordings(self):
        api = FakeAPI([LEGACY_SERVICE, {"result": 403, "data": []}])
        with self.assertRaises(recordings.CloudError):
            await recordings.CloudRecordings(api).async_dates("7")

    async def test_s3_repeated_cursor_stops_with_an_error(self):
        api = FakeAPI([S3_SERVICE, {"result": 200, "data": {"cursor": 1, "dates": []}}])
        client = recordings.CloudRecordings(api, search_base="https://search.example.com")
        with self.assertRaisesRegex(recordings.CloudError, "pagination"):
            await client.async_dates("7")


if __name__ == "__main__":
    unittest.main()
