"""V380 UCloud and S3 recording protocols recovered from the supplied APK.

Cloud URLs and temporary credentials stay on the server. RecordingSource must
only be consumed by the authenticated Home Assistant media proxy, never exposed
directly to a browser or media player. See research/RECORDINGS_PROTOCOL.md.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from datetime import date as Date, datetime, timezone
import hashlib
import hmac
import ipaddress
import json
import re
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from .account import AccountClient, CloudRecording, md5
from .errors import CloudError, LegacyHttpRequired, UnsupportedError

S3_SEARCH_BASE = "http://hscsearch.av380.net:8080"
HLS_MIME = "application/vnd.apple.mpegurl"
_MAX_PAGES = 100
_MAX_RECORDINGS = 10000


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudError(f"Cloud recording metadata has an invalid {label}")
    return value


def _day(value: Any) -> str:
    if not isinstance(value, str):
        raise CloudError("Cloud recording metadata has an invalid date")
    if re.fullmatch(r"\d{8}", value):
        value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as err:
        raise CloudError("Cloud recording metadata has an invalid date") from err
    if parsed.isoformat() != value:
        raise CloudError("Cloud recording metadata has an invalid date")
    return value


def _mime(filename: str) -> str:
    extension = filename.rsplit(".", 1)[-1].lower()
    return {"mp4": "video/mp4", "m4v": "video/mp4", "ts": "video/mp2t",
            "m3u8": HLS_MIME}.get(extension, "application/octet-stream")


def service_request(token: str, user_id: int, device_id: int, position: str,
                    timestamp: int, version: int = 1008) -> dict[str, Any]:
    canonical = (f"accesstoken={token}&deviceid={device_id}&timestamp={timestamp}"
                 f"&user_id={user_id}&user_position={position}&ver={version}")
    return {"accesstoken": token, "deviceid": device_id, "timestamp": timestamp,
            "user_id": user_id, "user_position": position, "ver": version,
            "sign": md5(canonical + "hsshop2016")}


def s3_list_request(token: str, user_id: int, device_id: int,
                    buckets: list[int], timestamp: int, *, day: str | None = None,
                    cursor: int = 1, page_size: int = 365) -> dict[str, Any]:
    firms = [{"bucket_index": value} for value in dict.fromkeys(buckets)]
    if day is None:
        kind = "date"
        canonical = (f"access_token={token}&cursor={cursor}&device_id={device_id}"
                     f"&firm_list={_compact(firms)}&page_size={page_size}"
                     f"&timestamp={timestamp}&type=date&user_id={user_id}")
    else:
        kind = "list"
        day = _day(day).replace("-", "")
        canonical = (f"access_token={token}&date={day}&device_id={device_id}"
                     f"&firm_list={_compact(firms)}&timestamp={timestamp}"
                     f"&type=list&user_id={user_id}")
    payload = {"access_token": token, "user_id": user_id, "device_id": device_id,
               "type": kind}
    if day is None:
        payload.update(cursor=cursor, page_size=page_size)
    else:
        payload["date"] = day
    payload.update(firm_list=firms, timestamp=timestamp,
                   sign=md5(canonical + "hongshi2025s3"))
    return payload


def ucloud_download_parameter(token: str, row: dict[str, Any]) -> str:
    """Match HttpDownloader.downloadUCloudRecordFile, including string device ID."""
    payload = {"access_token": token, "user_id": _int(row.get("user_id"), "user ID"),
               "device_id": str(_int(row.get("device_id"), "device ID")),
               "channel": _int(row.get("channel", 0), "channel"),
               "date": _int(row.get("date"), "date"),
               "time": _int(row.get("time"), "time"),
               "rand_num": _int(row.get("rand_num", 0), "random number")}
    return base64.b64encode(_compact(payload).encode()).decode("ascii")


@dataclass(frozen=True, slots=True)
class RecordingSegment:
    url: str = field(repr=False)
    duration: float
    mime_type: str = "video/mp2t"


@dataclass(frozen=True, slots=True)
class RecordingSource:
    """Private, short-lived backend source for the HA proxy."""
    mime_type: str
    filename: str
    url: str | None = field(default=None, repr=False)
    segments: tuple[RecordingSegment, ...] = field(default=(), repr=False)


@dataclass(slots=True)
class _Service:
    device_id: str
    metadata: dict[str, Any] = field(repr=False)
    fetched: float = field(default_factory=time.monotonic)
    credentials: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)
    date_buckets: dict[str, list[int]] = field(default_factory=dict)

    @property
    def s3(self) -> bool:
        return bool(self.metadata.get("s3_url") and self.metadata.get("buck_list"))

    @property
    def bucket_ids(self) -> list[int]:
        rows = self.metadata.get("buck_list", [])
        if not isinstance(rows, list):
            raise CloudError("Cloud bucket list has an invalid shape")
        return list(dict.fromkeys(_int(row.get("id"), "bucket ID")
                                  for row in rows if isinstance(row, dict)))


@dataclass(slots=True)
class _Recording:
    public: CloudRecording
    device_id: str
    day: str
    backend: str
    row: dict[str, Any] = field(repr=False)


def _s3_endpoint(credential: dict[str, Any]) -> str:
    endpoint = credential.get("host")
    if not isinstance(endpoint, str) or not endpoint:
        raise CloudError("Cloud storage endpoint is missing")
    if "://" not in endpoint:
        scheme = credential.get("scheme")
        if scheme not in ("http", "https"):
            raise CloudError("Cloud storage scheme is missing or invalid")
        endpoint = scheme + "://" + endpoint
    return endpoint


def _s3_recording_row(wire: dict[str, Any]) -> dict[str, Any]:
    """Decode the APK's Gson SerializedName keys, not Java property names."""
    raw_slices = wire.get("slf")
    if raw_slices is None:
        raw_slices = []
    if not isinstance(raw_slices, list) or any(not isinstance(item, dict) for item in raw_slices):
        raise CloudError("V380 recording segment list is invalid")
    return {
        "bucket_index": wire.get("bdx"), "bucket_name": wire.get("bnm"),
        "endpoint": wire.get("ep"), "prefix_path": wire.get("pth"),
        "file_path": wire.get("fth"), "zone": wire.get("zn", 0), "dst": wire.get("dst", 0),
        "slice_inf": [{"start_time": item.get("ste"), "end_time": item.get("ede"),
                       "filename": item.get("fne"), "rtype": item.get("rte", 0)}
                      for item in raw_slices],
    }


def s3_presigned_url(credential: dict[str, Any], key: str, *,
                     now: datetime | None = None, expires: int = 900) -> str:
    """S3 SigV4 GET for the APK's standard S3 getObject operation.

    Uses path addressing so custom S3-compatible endpoints retain their TLS
    host. Credential region/host/scheme/bucket are supplied by V380, not guessed.
    """
    for name in ("host", "name", "region", "ak", "sk"):
        if not isinstance(credential.get(name), str) or not credential[name]:
            raise CloudError("Cloud storage credentials are incomplete")
    endpoint = _s3_endpoint(credential)
    parts = urlsplit(endpoint)
    if (parts.scheme not in ("http", "https") or parts.query or parts.fragment
            or parts.username or parts.password or not parts.hostname):
        raise CloudError("Cloud storage endpoint is invalid")
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    scope = f"{day}/{credential['region']}/s3/aws4_request"
    # Do not normalize object-key slashes or '..': those are significant in S3.
    uri = quote(parts.path.rstrip("/") + "/" + credential["name"] + "/" + key,
                safe="/-_.~")
    query = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256",
             "X-Amz-Credential": f"{credential['ak']}/{scope}",
             "X-Amz-Date": stamp, "X-Amz-Expires": str(expires),
             "X-Amz-SignedHeaders": "host"}
    if credential.get("token"):
        query["X-Amz-Security-Token"] = credential["token"]
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
                               for k, v in sorted(query.items()))
    canonical = (f"GET\n{uri}\n{canonical_query}\nhost:{parts.netloc}\n\nhost\n"
                 "UNSIGNED-PAYLOAD")
    to_sign = f"AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signing_key = ("AWS4" + credential["sk"]).encode()
    for item in (day, credential["region"], "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, item.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    return urlunsplit((parts.scheme, parts.netloc, uri,
                       canonical_query + "&X-Amz-Signature=" + signature, ""))


class CloudRecordings:
    """Read-only account cloud recording catalog and internal playback sources."""

    def __init__(self, api: AccountClient, *, allow_insecure_http: bool = False,
                 search_base: str = S3_SEARCH_BASE) -> None:
        self.api = api
        self.allow_insecure_http = allow_insecure_http
        self.search_base = search_base
        self._services: dict[str, _Service] = {}
        self._records: dict[str, _Recording] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._services.clear()
        self._records.clear()
        self._locks.clear()

    def validate_recording(self, device_id: str, recording_id: str) -> None:
        """Check proxy-route ownership without a network request."""
        record = self._records.get(recording_id)
        if device_id not in self.api.devices or record is None or record.device_id != device_id:
            raise CloudError("Cloud recording is no longer in the catalog")

    def _cloud_url(self, value: Any, default_scheme: str = "http") -> str:
        if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
            raise CloudError("Cloud recording host is missing or invalid")
        if "://" not in value:
            value = default_scheme + "://" + value
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise CloudError("Cloud recording endpoint is invalid")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            if "." not in parts.hostname or parts.hostname.endswith((".local", ".localhost")):
                raise CloudError("Cloud recording endpoint is not a public host")
        else:
            if not address.is_global:
                raise CloudError("Cloud recording endpoint is not a public address")
        if parts.scheme == "http" and not self.allow_insecure_http:
            raise LegacyHttpRequired(
                "V380 supplied an HTTP cloud recording endpoint. Enable the explicit "
                "legacy HTTP option to use the app's protocol; cloud tokens and video "
                "will travel without TLS on those requests."
            )
        return value.rstrip("/")

    async def _service(self, device_id: str) -> _Service:
        if device_id not in self.api.devices:
            raise CloudError("Unknown camera")
        async with self._locks.setdefault(device_id, asyncio.Lock()):
            previous = self._services.get(device_id)
            if previous and time.monotonic() - previous.fetched < 300:
                return previous
            response = await self.api._request_json(
                self.api.billing_url.rstrip("/") + "/device/get-service-info",
                service_request(self.api.access_token, self.api.user_id, int(device_id),
                                self.api.user_position, int(time.time())),
            )
            self.api._check_result(response)
            service = _Service(device_id, response)
            if previous:
                service.credentials = previous.credentials
                service.date_buckets = previous.date_buckets
            self._services[device_id] = service
            return service

    @staticmethod
    def _catalog_result(response: dict[str, Any]) -> bool:
        result = response.get("result")
        if result == 200:
            return True
        if result in (-1002, 64534):  # signed and unsigned app NO_DATA values.
            return False
        raise CloudError("V380 recording catalog rejected the request (result %s)" %
                         (result if isinstance(result, int) else "missing"))

    async def _ucloud_catalog(self, service: _Service, kind: int, *,
                              day: str | None = None, page_size: int = 365,
                              channel: int = 0) -> list[dict[str, Any]]:
        bucket = service.metadata.get("bucket")
        if not isinstance(bucket, dict) or not bucket.get("list"):
            if not service.metadata.get("bind_status"):
                return []
            raise UnsupportedError("V380 did not return a cloud recording list host")
        endpoint = self._cloud_url(bucket["list"]) + "/list.php"
        rows: list[dict[str, Any]] = []
        seen_pages: set[str] = set()
        for page in range(1, _MAX_PAGES + 1):
            payload = {"access_token": self.api.access_token, "user_id": self.api.user_id,
                       "type": kind, "device_id": int(service.device_id)}
            if day is not None:
                payload["date"] = _day(day)
            payload.update(channel=channel, page=page, pageSize=page_size)
            # NVCryptor.Encode uses Base64.DEFAULT, including LF line wrapping.
            parameter = base64.encodebytes(_compact(payload).encode()).decode("ascii")
            response = await self.api._request_json(endpoint, None, params={"param": parameter})
            if not self._catalog_result(response):
                break
            batch = response.get("data")
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise CloudError("V380 recording catalog has an invalid shape")
            digest = hashlib.sha256(_compact(batch).encode()).hexdigest()
            if batch and digest in seen_pages:
                raise CloudError("V380 recording pagination did not advance")
            seen_pages.add(digest)
            rows.extend(batch)
            if len(rows) > _MAX_RECORDINGS:
                raise CloudError("V380 recording catalog exceeds the supported size")
            count = response.get("count")
            if len(batch) < page_size or (isinstance(count, int) and len(rows) >= count):
                break
        else:
            raise CloudError("V380 recording catalog exceeds the supported page limit")
        return rows

    async def async_dates(self, device_id: str) -> list[str]:
        service = await self._service(device_id)
        if not service.s3:
            rows = await self._ucloud_catalog(service, 2)
            return sorted({_day(row.get("date")) for row in rows}, reverse=True)
        endpoint = self._cloud_url(self.search_base) + "/replay/list"
        cursor = 1
        dates: set[str] = set()
        for _ in range(_MAX_PAGES):
            response = await self.api._request_json(endpoint, s3_list_request(
                self.api.access_token, self.api.user_id, int(device_id),
                service.bucket_ids, int(time.time()), cursor=cursor,
            ))
            if not self._catalog_result(response):
                break
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("dates"), list):
                raise CloudError("V380 recording date list has an invalid shape")
            for row in data["dates"]:
                if not isinstance(row, dict):
                    raise CloudError("V380 recording date list contains an invalid item")
                day = _day(row.get("fne"))
                dates.add(day)
                firms = row.get("flt")
                if not isinstance(firms, list) or any(not isinstance(firm, dict) for firm in firms):
                    raise CloudError("V380 recording date bucket list has an invalid shape")
                service.date_buckets[day] = [_int(firm.get("bdx"), "bucket ID")
                                             for firm in firms]
            next_cursor = _int(data.get("cursor", -1), "date cursor")
            if next_cursor == -1:
                break
            if next_cursor == cursor:
                raise CloudError("V380 recording date pagination did not advance")
            cursor = next_cursor
        else:
            raise CloudError("V380 recording dates exceed the supported page limit")
        return sorted(dates, reverse=True)

    async def async_recordings(self, device_id: str, day: str) -> list[CloudRecording]:
        day = _day(day)
        service = await self._service(device_id)
        if service.s3:
            endpoint = self._cloud_url(self.search_base) + "/replay/list"
            response = await self.api._request_json(endpoint, s3_list_request(
                self.api.access_token, self.api.user_id, int(device_id),
                service.date_buckets.get(day) or service.bucket_ids, int(time.time()), day=day,
            ))
            if not self._catalog_result(response):
                return []
            rows = response.get("data")
            backend = "s3"
        else:
            rows = await self._ucloud_catalog(service, 4, day=day, page_size=5000)
            backend = "ucloud"
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise CloudError("V380 recording list has an invalid shape")
        if len(rows) > _MAX_RECORDINGS:
            raise CloudError("V380 recording list exceeds the supported size")
        result: list[CloudRecording] = []
        # Expired entries are rebuilt from the cloud on each Media browser visit.
        for key in [key for key, value in self._records.items()
                    if value.device_id == device_id and value.day == day]:
            self._records.pop(key)
        for row in rows:
            if backend == "s3":
                row = _s3_recording_row(row)
                slices = row.get("slice_inf")
                if not isinstance(slices, list) or not slices:
                    continue
                if any(not isinstance(item, dict) for item in slices):
                    raise CloudError("V380 recording segment list is invalid")
                start = _int(slices[0].get("start_time"), "segment start")
                zone = _int(row.get("zone", 0), "time zone")
                dst = _int(row.get("dst", 0), "daylight saving")
                title = datetime.fromtimestamp((start + zone + dst * 3600000) / 1000,
                                              timezone.utc).strftime("%H:%M:%S")
                identity = [row.get("bucket_index"), row.get("prefix_path"),
                            row.get("file_path"), slices[0].get("filename"), start]
                mime = HLS_MIME
            else:
                if str(row.get("device_id")) != device_id:
                    raise CloudError("Cloud recording belongs to a different camera")
                clock = _int(row.get("time"), "recording time")
                title = f"{clock:06d}"
                title = f"{title[:2]}:{title[2:4]}:{title[4:6]}"
                filename = row.get("file_name")
                if not isinstance(filename, str) or not filename:
                    raise CloudError("V380 recording filename is missing")
                identity = [row.get("id"), row.get("channel"), row.get("date"), clock,
                            row.get("rand_num"), filename]
                mime = _mime(filename)
            recording_id = hashlib.sha256(_compact([device_id, day, backend, identity]).encode()).hexdigest()[:32]
            public = CloudRecording(recording_id, title, mime)
            if len(self._records) >= _MAX_RECORDINGS and recording_id not in self._records:
                self._records.pop(next(iter(self._records)))
            self._records[recording_id] = _Recording(public, device_id, day, backend, row)
            result.append(public)
        return result

    async def _credential(self, service: _Service, bucket_id: int) -> dict[str, Any]:
        previous = service.credentials.get(bucket_id)
        if previous and isinstance(previous.get("exp"), int) and previous["exp"] > time.time() + 60:
            return previous
        endpoint = self._cloud_url(service.metadata.get("s3_url"))
        payload = {"user_token": self.api.access_token, "buck_id": [bucket_id]}
        timestamp = int(time.time())
        signature = md5(md5(_compact(payload)) + "GzHong^&*2025" + str(timestamp))
        response = await self.api._request_json(endpoint + "/v1/app/gettoken", payload,
                                              params={"signature": signature, "timestamp": timestamp})
        if response.get("code") != 7800:
            raise CloudError("V380 did not grant cloud recording storage credentials")
        data = response.get("data")
        buckets = data.get("bucket") if isinstance(data, dict) else None
        if not isinstance(buckets, list):
            raise CloudError("V380 cloud recording storage credentials have an invalid shape")
        for bucket in buckets:
            if not isinstance(bucket, dict):
                raise CloudError("V380 cloud recording storage credentials contain an invalid item")
            service.credentials[_int(bucket.get("id"), "bucket ID")] = bucket
        if bucket_id not in service.credentials:
            raise CloudError("V380 did not grant credentials for this recording's storage bucket")
        return service.credentials[bucket_id]

    async def async_source(self, device_id: str, recording_id: str) -> RecordingSource:
        self.validate_recording(device_id, recording_id)
        record = self._records.get(recording_id)
        if record is None or record.device_id != device_id:
            raise CloudError("Cloud recording is no longer in the catalog")
        service = await self._service(device_id)
        row = record.row
        if record.backend == "ucloud":
            bucket = service.metadata.get("bucket")
            if (not isinstance(bucket, dict)
                    or bucket.get("bucket_index") != row.get("bucket_index")):
                # Fetch the app's bucket-aware lookup, rather than downloading
                # a recording from whichever bucket happened to be selected.
                stamp = int(time.time())
                bucket_id = _int(row.get("bucket_index"), "bucket ID")
                payload = {"accesstoken": self.api.access_token, "bucketindex": bucket_id,
                           "timestamp": stamp,
                           "sign": md5(f"accesstoken={self.api.access_token}&bucketindex={bucket_id}"
                                       f"&timestamp={stamp}hsshop2016")}
                host_info = await self.api._request_json(
                    self.api.billing_url.rstrip("/") + "/disk/get-host-list", payload)
                self.api._check_result(host_info)
                candidate = host_info.get("data")
                if (not isinstance(candidate, dict)
                        or candidate.get("bucket_index") != row.get("bucket_index")):
                    raise UnsupportedError("V380 did not return a download host for this recording's bucket")
                bucket = candidate
            endpoint = self._cloud_url(bucket.get("download")) + "/download.php"
            parameter = ucloud_download_parameter(self.api.access_token, row)
            return RecordingSource(record.public.mime_type, row["file_name"],
                                   endpoint + "?" + urlencode({"param": parameter}))
        bucket_id = _int(row.get("bucket_index"), "bucket ID")
        credential = await self._credential(service, bucket_id)
        # Check scheme and public host before signing temporary credentials.
        self._cloud_url(_s3_endpoint(credential))
        prefix = row.get("prefix_path")
        if not isinstance(prefix, str):
            raise CloudError("V380 recording object prefix is missing")
        segments: list[RecordingSegment] = []
        for item in row["slice_inf"]:
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                raise CloudError("V380 recording segment filename is missing")
            start = _int(item.get("start_time"), "segment start")
            end = _int(item.get("end_time"), "segment end")
            if end <= start:
                raise CloudError("V380 recording segment duration is invalid")
            segments.append(RecordingSegment(s3_presigned_url(credential, prefix + filename),
                                             (end - start) / 1000, _mime(filename)))
        return RecordingSource(HLS_MIME, "recording.m3u8", segments=tuple(segments))
