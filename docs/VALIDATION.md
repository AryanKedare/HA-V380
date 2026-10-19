# Validation status — 19 October 2026

Target Home Assistant release: **2026.10.2**. The official release endpoint
reported this as the latest stable release during the final check:
[release](https://github.com/home-assistant/core/releases/tag/2026.10.2).
The supplied APK is V380 Pro2.2.90, not a Home Assistant package.

## Cleanly completed checks

The following combined run completes with **exit status0**:

```sh
python -m pytest -q tests/test_account.py tests/test_live.py tests/test_mpegts.py \
  tests/test_recordings.py tests/test_proxy.py tests/test_shared.py
```

Result: **41 tests passed, plus 3 subtests**. Coverage includes:

- Actual localhost HTTP login/device/token fixtures. Password vectors are
  independently generated with OpenSSL, including the ver2 AES device branch.
- Dispatcher, cloud login and encrypted fragmented-video fixtures through both
  in-memory duplex streams and actual localhost TCP sockets. No vendor endpoint
  is used. Real TCP test ran successfully with the root process's network grant.
- An H.264 stream produced by the MPEGTS packetizer is independently demuxed and
  decoded by FFmpeg/PyAV, with15 frames and increasing presentation timestamps.
- UCloud and S3 catalog/retrieval fixtures, exact Gson wire aliases, pagination,
  ownership checks, HTTP preference and independent botocore SigV4 comparison.
- Real Home Assistant authentication, signatures and HTTP request middleware:
  unauthorized/expired/tampered URLs are rejected, HLS contains no vendor tokens,
  byte ranges preserve206/Content-Range, and HTTP200 error documents are rejected
  before being served as video.
- Shared-camera credential normalization and newer-platform classification.

Python syntax and JSON/translation parity are checked before packaging. ZIP
contents are checked against the source and include no APKs, native libraries,
credentials, recordings, bytecode caches or test environments.

The standalone diagnostic has a separate clean run of **12 tests passed**:

```sh
python -m pytest -q tests/test_full_probe.py
python v380_full_test.py --self-test
```

Its self-test decodes 15 synthetic video frames and checks encryption vectors
and the Home Assistant token-origin guard. Both checks pass from the published
repository layout. The standalone script's embedded protocol sources also match
the integration's corresponding modules. See [the testing guide](../V380_TESTING.md).

After the 0.1.1 diagnostic changes, the six protocol/proxy groups above plus
`tests/test_full_probe.py` complete together with **53 passed, plus 3 subtests**,
exit status 0. This includes real localhost TCP/HTTP fixtures. The extra tests
cover preserving a synthetic device-login rejection after trying all relays,
distinguishing dispatcher connection failures from truncated responses, bounding
diagnostic fields, and skipping direct/HA recording requests in live-only mode.
An initial attempt without permission to bind localhost sockets failed during
fixture setup; repeating with that permission completed cleanly.

One user-run report now confirms cloud account login and one camera's discovery
against the actual service. Live playback failed with a generic CloudError in
the old tester, so it establishes no failing protocol stage. The user confirms
live video works in V380 Pro and no saved cloud recordings are available. Tester
1.1.0 adds safe stage/result diagnostics and `--live-only` for the next run;
this change does not establish that the camera's live playback is fixed.

## Unresolved adapter-test failure

`tests/test_ha.py` reports **15 passed**, then the interpreter exits with
status139 while garbage-collecting during shutdown. It is **not** a successful
completed test run. All tests together report54 assertions passed before the
same shutdown failure (the total can change as tests are added).

The shutdown failure reproduces on this macOS ARM64 host with Python3.14.0,
3.14.2 and3.14.8. Python3.14.2 and3.14.8 satisfy the HA package's declared minimum.
A debug allocator changes the signal to a bus error without isolating its cause.
Faulthandler's C stack includes `dict_dealloc`, `subtype_clear` or
`PyObject_ClearManagedDict`, and module finalization; no responsible Python frame
is identified. A standalone invocation of the media test also reaches its final
statement and then fails at shutdown.

Explicit ConfigEntry unload callbacks, a plain reauth observer in place of a
class-level MagicMock, and exception classes instead of reused exception
instances do not resolve it. Two smaller HA lifecycle probes complete normally.
These observations do not establish whether the issue belongs to the integration,
its fixture lifecycle, a dependency, or interpreter behavior. No GC disabling,
forced exit, skipped failing assertion or other bypass is used.

## Required real-account verification

No V380 credentials or camera runtime were available to the developer. The user's
report confirms account login/discovery, but neither live vendor playback nor a
saved vendor recording has passed this implementation. Before this can be called a
working integration for the user's camera, it needs:

1. Successful regular account sign-in and discovery through the actual service.
2. Supported camera-platform classification and regional cloud routing.
3. Actual live keyframe decoding, picture timing, reconnect and source renewal.
4. Actual UCloud MP4 or S3 TS download and browser playback/seek verification.
5. Resolution of the adapter shutdown-test failure and repetition on the
   intended Home Assistant host.

Newer key-exchange live video remains unimplemented; this is an explicit
compatibility limit independent of the test failure. The included compatibility
probe prompts locally for credentials and can provide sanitized evidence for
the next investigation step.
