# Cloud recording protocol evidence

This integration implements the two recording backends found in the supplied V380 Pro APK: legacy UCloud MP4 downloads and S3 object storage containing TS segments. The implementation and tests are based on static APK evidence and synthetic responses. No V380 account, cloud recording, or vendor endpoint was exercised during development. Actual camera/account compatibility remains to be checked after installation.

## Artifact identity and evidence boundary

| Artifact | SHA-256 |
| --- | --- |
| `base.apk` | `1cb63bc47ec36f46b9764ecdd6cbdf9a535d3ce772b7a02b94e7f2f3a00cb91f` |
| `split_config.arm64_v8a.apk` | `0dd840fea287f1fb2900f01ad02383e4d00d3515146681a0398b821074650f2e` |
| Extracted `libhsMediaLibrary.so` | `01c15be44bb841f38e2ea72cbcbee4914a21720bab25744c6bfc7e61cb62b558` |

REA artifact inventory evidence: `ev_7d8ce962dd584a93f1d79eeabc886dcb75be7d95466a60150eb26667e12bee99`. That record establishes artifact identity and inventory. It does **not** establish the code behavior below. REA's Android provider was unavailable because no caller-supplied JADX MCP JAR was configured; Android findings below came from Androguard DEX parsing, instruction inspection, and field annotations. Native findings came from static ELF disassembly with the host's LLVM objdump. No APK or native library was executed.

Instruction offsets below are byte offsets from the start of the named DEX method. Native addresses are ELF virtual addresses in `libhsMediaLibrary.so`. [The schema evidence file](apk_recording_schema.json) preserves the relevant Gson `SerializedName` mappings, DEX hashes, field indexes, and annotation offsets. Response keys must follow those annotations rather than Java getter/property names.

## Discovery and legacy UCloud

The following classes are in `classes10.dex` under `com.macrovideo.v380pro` unless another package is stated.

| Observation | Static source |
| --- | --- |
| Service discovery posts to billing `device/get-service-info`, using `accesstoken`, `deviceid`, `timestamp`, `user_id`, `user_position`, `ver` and a lowercase MD5 signature. The canonical string uses that order, followed by `hsshop2016`. | `utils.OkHttpUtil.getDeviceUCloudInfo`: `0x002a–0x00a6` canonical string; `0x00b2–0x0110` payload; `0x014c–0x0154` endpoint. |
| `ver=1008` is selected when pre-recorded cloud support is enabled; the shipped SDK initializes that flag to true. | `OkHttpUtil.getDeviceUCloudInfo` at `0x0014–0x0022`; `com.macrovideo.sdk.GlobalConfiguration.<clinit>` in `classes9.dex`, `0x0018` sets true and `0x0094` stores the flag. |
| The service response has top-level `bucket`, `s3_url`, `buck_list`, `bind_status`, `result`, and `error_code`. Legacy `bucket` includes `list`, `download`, `bucket_index`, `video`, and `video_port`. | `json.DeviceUCloudInfoParse` and nested `BucketData` field declarations. No alternative Gson names are attached to these fields. |
| Legacy listing is a GET to `http://<bucket.list>/list.php?param=<base64 JSON>`. The JSON has `access_token`, `user_id`, numeric `type`, `device_id`, optional `date`, `channel`, `page`, and camel-case `pageSize`. | `OkHttpUtil.getUCloudRecData`: `0x0016–0x00aa` fields; `0x00ea–0x011c` URL and encoding. |
| `NVCryptor.Encode` is standard Android Base64 with flags `0`: line-wrapped output and trailing LF. It is not encryption. | `com.macrovideo.sdk.tools.NVCryptor.Encode` in `classes9.dex`, `0x0000–0x0010`. |
| Dates use `type=2`, `page=1`, `pageSize=365`; recordings use `type=4`, `date=YYYY-MM-DD`, `channel=0`, `page=1`, `pageSize=5000`. | `fragments.ReplayCloudFragment.getRecDateList` at `0x0032–0x0046`; `startGetRecData` at `0x00e0–0x0118`. |
| Listing succeeds when `result=200`. Date rows have a string `date`; recording rows have integer `date` and `time`, plus `id`, `user_id`, `device_id`, `channel`, `rand_num`, `file_name`, and `bucket_index`. | `ReplayCloudFragment$74.onResponse` at `0x00a8–0x00b4`; `$71.onResponse` at `0x00b8–0x00e6`; `json.UCloudRecCatalogJsonParse$DataBean` and `UCloudRecFileJsonParse$DataBean` declarations. |
| A recording on another bucket uses billing `disk/get-host-list`, with `bucketindex`. The signature is MD5 of `accesstoken=…&bucketindex=…&timestamp=…hsshop2016`. | `OkHttpUtil.accordingBucketIndexGetHostList`: `0x0022–0x0066` canonical string; `0x0068–0x009a` fields; `0x00d6–0x00de` endpoint. |
| Download is GET `http://<bucket.download>/download.php?param=<base64 JSON>`. The exact field order is `access_token,user_id,device_id,channel,date,time,rand_num`. Download `device_id` is a **string**. Download Base64 uses flags `2`, without line wrapping. | `utils.httpDownload.HttpDownloader.downloadUCloudRecordFile`: `0x0092–0x0122` JSON; `0x00ca` string conversion; `0x0136–0x014e` encoding. `activities.UCloudRecNormalPlayerActivity.downloadUCloudRec`: `0x0098–0x00b2` URL. |
| The HTTP download handler writes the returned `application/octet-stream` bytes directly to the recording file. The completion callback then adds app-specific MP4 metadata. | `utils.httpDownload.DownloadTask.download`: `0x0250–0x0288` content-type checks and stream; `0x02ce–0x02e2` read/write; `0x0372–0x037e` close. `UCloudRecNormalPlayerActivity$5.onDownload` at `0x027c`: `HSMediaLibrary.AddUserInfoToMP4File`. |

**Inference:** the legacy HTTP download endpoint supplies MP4 media without a client decryption step in this Java path. This is supported by the direct file write and subsequent MP4 metadata operation. It has not been verified against an actual response. The app also has proprietary cloud playback transports; implementing this direct download path avoids needing to reproduce them for saved footage.

The legacy listing response's `key` field is retained by the Android playback fragment. The HTTP download path does not pass that key to its downloader or completion callback. The integration therefore does not invent a decryption operation for downloaded MP4 files.

## S3 catalog and temporary credentials

`classes10.dex` `utils.OkHttpUtil.getS3DateList` and `getS3RecList` POST JSON to `/replay/list`. The shipped default is `http://hscsearch.av380.net:8080`, established in `defines.HttpUrlDefines.<clinit>` at `0x0070–0x0074`. The app also supports separately assigned/dynamic search hosts; no live assignment was observed.

The request fields and signature strings are:

```text
dates:
access_token=…&cursor=…&device_id=…&firm_list=…&page_size=…&timestamp=…&type=date&user_id=…hongshi2025s3

files:
access_token=…&date=YYYYMMDD&device_id=…&firm_list=…&timestamp=…&type=list&user_id=…hongshi2025s3
```

`firm_list` is compact JSON, e.g. `[{"bucket_index":11}]`, with duplicate bucket IDs removed while retaining order. The request's `sign` is lowercase MD5 of the appropriate string. Literal request names differ from some abbreviated response names.

| Observation | Static source |
| --- | --- |
| Date-list fields, signing suffix, and `/replay/list` endpoint. | `OkHttpUtil.getS3DateList`: `0x00d0–0x00dc` bucket request key; `0x0100–0x0198` canonical string; `0x01d2–0x0246` payload/sign; `0x02ae–0x02c4` endpoint. |
| File-list fields and signature. | `OkHttpUtil.getS3RecList`: `0x009c–0x00a8` request bucket key; `0x00cc–0x0144` canonical string; `0x017e–0x01d8` payload; `0x0240–0x0256` endpoint. |
| File-list date is normalized from `YYYY-MM-DD` to `YYYYMMDD`. | `fragments.ReplayCloudFragment.getS3RecFileCatalogByService`: `0x0040–0x0088`. |
| Date pagination begins at cursor `1`; a returned cursor of `-1` ends pagination. | `fragments.UCloudRecDateCatalogFragment.newInstance` at `0x0004–0x0006`; `onLoadMore` at `0x0016–0x001c`, `0x0096–0x00a8`. |
| Catalog success is `result=200`; no-data is `-1002`, also represented by unsigned `64534`. | `ReplayCloudFragment$72.onResponse`; `com.macrovideo.sdk.json.S3RecListJsonParse.RESULT_NO_DATA` field; `UCloudRecDateCatalogFragment$3.onResponse` at `0x0168–0x0174`. |
| Date response entries use `fne` for `YYYYMMDD`, `flt` for bucket references, and nested `bdx` for bucket ID. | `classes9.dex` Gson annotations on `S3DateListJsonParse$Dates` and `$FirmList`. |
| File response rows use `bdx` bucket ID, `bnm` bucket name, `ep` endpoint, `pth` key prefix, `fth` playlist filename, `slf` slices, `zn` time-zone offset, `dst` daylight saving. Slices use `ste`, `ede`, `fne`, `rte`. | `classes9.dex` Gson annotations on `S3RecListJsonParse$Data` and `$SliceInf`. |
| Temporary credentials are requested from the discovered `s3_url` plus `/v1/app/gettoken`. Body: compact `{"user_token":"…","buck_id":[…]}`. URL signature: `MD5(MD5(body)+"GzHong^&*2025"+timestamp)`. | `classes9.dex` `com.macrovideo.sdk.tools.OkHttpUtils.a(String,List,List)` at `0x00b6–0x00d6`; `a(String,JSONObject)` at `0x001e–0x005c` signature and `0x006e–0x008e` query path; `getS3TokenSync` at `0x0000–0x0022`. |
| Credential response success is `code=7800`. Its array is `data.bucket`; each item has `id`, `ak`, `sk`, `token`, `exp`, `host`, `name`, `region`, `scheme`, `dir`, `btype`. | `OkHttpUtils.parseS3TokenResp` at `0x008a–0x010a`; Gson annotations on `S3TokenJsonParse$DataBean` and `$BucketConfig`. Java properties `bucketList`, `bucketId`, `type` do **not** name those JSON keys. |
| The Android SDK retrieves ordinary S3 objects using the supplied temporary credentials. Object key is exactly `prefix_path + slice.filename`, without inserting or normalizing slashes. | `classes9.dex` `cloudStorage.S3StorageClient.a` at `0x002e–0x0082` credentials/client/endpoint; `doDownloadFile` at `0x0028–0x0042`; `media.HSMediaPlayer$9.run` at `0x00b2–0x00dc`, `0x012e–0x0140`. |

The integration uses standard [AWS SigV4 query authentication](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-query-string-auth.html) to perform GETs for those S3 objects through the Home Assistant backend. Presigning, path addressing and the generated HLS manifest are implementation choices; they are not claimed to be Android wire behavior. Tests compare the signature against the independent botocore implementation. A particular S3-compatible provider's acceptance of path addressing and SigV4 remains a runtime compatibility check.

An explicit scheme in `host` is retained. If `host` has no scheme, the credential's `scheme` must specify `http` or `https`; the integration does not guess one. HTTP requires the integration's explicit legacy-HTTP option. The known default HTTP search service is not silently upgraded or tried over HTTPS.

## S3 media format and remaining playback limits

`HSReplayDataUCloudS3Transmitter::dataParseThreadFunc` begins at native VA `0x69e50`. It calls `avformat_open_input` at `0x6a830`, `avformat_find_stream_info` at `0x6a938`, `TsParser::parseESInfoAtTsPath` at `0x6a95c`, and `av_read_frame` at `0x6b2bc`. It passes the AVPacket data buffer onward through callbacks at `0x6b5a8` and `0x6b770`. This function's inspected body contains no call to the library's media-decryption functions. The Android M3U8 parser also recognizes standard `#EXTM3U`, `#EXTINF`, program-date/time and date-range tags.

**Inference:** these objects are MPEG transport streams demuxed by ordinary FFmpeg. The integration uses the returned slice boundaries to generate HLS and proxies each object. This does not establish codec support in a particular browser, audio support, panorama rendering, proprietary multi-camera descriptors, or whether the user's camera uses this backend. There was no real TS or MP4 sample for decode verification. The integration does not transcode recordings or implement panorama/multiple-eye reconstruction.

## Implementation and validation

`custom_components/v380_cloud/recordings.py` provides service discovery, date/file catalogs, bounded pagination, opaque recording IDs bound to one camera, fresh internal download sources, and S3 credentials/signing. Vendor URLs and credentials are only used behind Home Assistant's authenticated media routes. The public Media browser receives Home Assistant URLs. Sharing endpoints were inspected but are not called or needed for playback.

`tests/test_recordings.py` covers synthetic full UCloud and S3 flows; actual abbreviated response keys; Base64 flags and string/numeric types; MD5 canonical strings; SigV4 comparison against botocore; page/cursor progression; bucket-specific host lookup; camera ownership; explicit HTTP opt-in; and service failures remaining errors. Those tests verify the recovered protocol implementation and integration boundaries, not successful access to the vendor's current service.
