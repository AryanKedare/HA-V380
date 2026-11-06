# Validation status — 6 November 2026

## Current 0.1.8 results

The user reports a startup network timeout and confirms that V380 Pro still
plays and an integration reload restores HA video. The 0.1.7 outer timeout
always reported media_stream, so that log does not identify which upstream
step stalled. No new vendor response or account credentials were captured.

Inspection found that each HTTP request discarded its working relay hint.
Repeated 25-second HTTP startup deadlines could cancel sequential relay attempts
and restart with the same slow first routes indefinitely. Synthetic binary-media
regressions reproduce this deadline sequence and verify that shared route history
advances successive HTTP attempts to an available alternative.

Version 0.1.8 remembers a working relay for 15 minutes, tries it before discovery,
deprioritizes failed/cancelled starts for 60 seconds, and retains recent alternate
routes if discovery fails. A startup network timeout allows one saved account
session/catalog recovery on the next request, limited to once per account per
five minutes. Temporary account API failure can retain cached camera credentials;
real saved-password rejection remains an auth failure. Unload clears route state.

Failure diagnostics now preserve dispatcher/login/relay/media progress instead
of inventing a media stage for every outer timeout. Counters and enums are
allowlisted and bounded; endpoints, sessions, passwords and device IDs are
excluded. Normal downstream cancellation does not count as a relay failure.

Full suite: **179 passed, 14 subtests passed, exit status 0** on Core 2026.11.0,
Python 3.14.10, PyAV 19.0.0 and pytest 9.2.0 on macOS ARM64. Nine dependency/HA
warnings remain. Sixteen new tests cover shared route state, preferred-route
failure, dispatcher outage, cache expiry, audio-only cancellation, privacy,
account recovery cooldown/concurrency and genuine rejection. The real HTTP
startup test also checks exact stage propagation and the recovery trigger.
The seven real HA/HLS pipeline tests continue to pass. Standalone tester 1.5.0
embeds the matching protocol and its self-test decodes 15 frames successfully.

This establishes the offline routing/recovery behavior, not the exact cause
of the user's upstream stall or long-running camera/browser reliability.
No vendor account or installed HA was accessed.

Primary routing reference:
[OpenV380 cached-relay connection flow](https://github.com/cimplesid/v380-mac/blob/c3f5c5d17f4930fb90546be5d6c335c850975b86/Sources/V380/V380Client.swift).
The retry ordering and cache bounds here are integration policy; no new wire
commands or fragment acknowledgements were inferred from the timeout.

## Previous 0.1.7 results

The user provides a Core 2026.11.0 log with opening/demuxing errors and
`Stream is not configured for format 'hls'`. Every logged camera URL still has
over 100 seconds of signature lifetime. Metadata refresh failures also occur.
These observations do not establish another expired-signature/password failure.

Core's `update_source` stops the worker. During retry backoff, that can finish
the loop and remove HLS outputs in queued cleanup. An isolated 0.1.6 replay with
actual Core 2026.11.0 reproduces the exact ValueError. The patched camera refreshes
the public reconnect URL without interrupting the current response and retires
idle objects before a new viewer attaches.

Transient metadata errors retain cached video availability; auth rejection,
removal and failed video remain unavailable. Startup is limited to 25 seconds
within Core's 30-second reader budget. Disconnect cancels the producer promptly.
Sanitized live failure phase/stage/codes are exposed in logs and diagnostics.

Full suite: **163 passed, 14 subtests passed, exit status 0** on Python 3.14.10,
Core 2026.11.0, PyAV 19.0.0 and pytest 9.2.0 on macOS ARM64. Nine dependency/HA
deprecation or AppKey warnings are reported. Seven pipeline tests use actual HA
auth, local HTTP, Core's worker, HLS generation and independent fMP4 decoding;
they cover renewal, retry, idle reuse, slow startup, disconnect and privacy.
All account/relay/recording/proxy/adapter fixtures also complete. The unchanged
standalone 1.4.0 self-test decodes 15 frames and embedded sources match.

No vendor account or installed HA was accessed. The new runtime/configurations
stay outside the released source tree. The historical 2026.10.2 shutdown failure
did not occur in this current target run; its original cause is undetermined.
Real camera/network/browser longevity still needs confirmation.

Primary Core 2026.11.0 references:

- [Source updates, worker retry and cleanup](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/stream/__init__.py)
- [HLS endpoint startup](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/camera/helper.py)
- [Source timeout](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/stream/const.py)

## Previous 0.1.6 results

The user reports local 404 followed by the new missing-camera message, and says
reloading the integration restores playback. The actual failing vendor response
has not been captured. Inspection found that missing/null sections and recognized
partial results could replace the entire device map with an empty/subset map.
An isolated replay against 0.1.5 reproduces that cache loss; the patched source
retains the previously known camera for the same partial response.

Version 0.1.6 preserves known entries for non-authoritative sections and confirms
removals with one additional request. A suddenly empty catalog performs one
saved-credential login before confirmation. Confirmed removals still take effect;
failed login/request/parse cannot erase the committed map. Concurrent refreshes
share a result, and routes use that client map rather than stale coordinator data.

Checks completed successfully: 20 catalog regressions, 24 source/route lifecycle,
20 auth/relay recovery, six forwarding, four shared parsing, 12 standalone report
and eight installer checks: 94 total. Catalog coverage includes partial result
codes, missing sections, corrupt shares, true removal, transient omissions, empty
catalog login recovery, failed confirmation, token expiry, concurrency and unload.
Route tests cover snapshot ordering, removed-camera rejection and retryable
incomplete lookup. Diagnostics/report privacy is checked with synthetic secrets.

Tester 1.4.0 embeds the matching account implementation and reports aggregate
catalog status/counters. Its self-test decodes 15 synthetic frames. Python/JSON,
translation parity, embedded hashes and ZIP/source equality are checked. No vendor
requests or installed HA changes were made. Target HA/browser longevity and the
previously documented full adapter-suite shutdown failure remain unverified.

## Previous 0.1.5 results

The user confirms playback initially worked after 0.1.4, then stopped with
`Failed to start WebRTC stream` and the generic `cloud_unavailable` translation.
Their screenshot identifies Core 2026.10.4 on HA OS. The exact underlying source
failure is not established by that translated message.

The official Core 2026.10.4 camera and go2rtc sources confirm that automatic
providers are matched by source URL scheme; go2rtc calls `stream_source()` again
on an offer and forwards its HomeAssistantError to the frontend. Version 0.1.5
declines automatic providers for the authenticated MPEG-TS source so Core uses
its shared HLS stream. It also reports absent camera metadata, an uninitialized
proxy and unreadable camera credentials separately. Generic cloud failures and
real account authentication errors still propagate.

Current checks pass: 20 offline source/route lifecycle tests with synthetic HA
interfaces, 20 portable auth/relay recovery tests, and eight installer tests.
The six added source checks cover provider opt-out without source access,
source-error classification, sanitized logging, retry after missing metadata,
and the two API error subtypes.

An additional compatibility harness extracts and executes the exact Core
2026.10.4 provider/capability methods with a synthetic backend. It reproduces
automatic WebRTC/HLS capability selection on the prior camera and confirms
HLS-only capabilities after the patch, without provider/source lookup. This is
not a full HA installation or browser test. The real HA fixture now includes an
HLS capability assertion; its target environment remains unavailable. No vendor
requests or installed HA changes were made. The prior adapter shutdown failure
remains unresolved. The portable protocol and tester 1.3.0 are unchanged.

Primary references:

- [Core 2026.10.4 camera provider/capability selection](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/camera/__init__.py)
- [Core 2026.10.4 go2rtc source preparation](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/go2rtc/__init__.py)

## Previous 0.1.4 results

The user confirms HA 0.1.3 playback starts, then fails with local HTTP 404/401.
Both log lines use the same HA live path and a signature with a ten-minute
lifetime. Those errors do not establish a new V380 account or relay rejection.

Inspection found two URL lifecycle defects: repeated calls to `stream_source`
could reset the timer without replacing an existing Stream's source, and HA's
cached Stream bypasses `stream_source` on reuse. Version 0.1.4 keeps a fixed
renewal deadline for the active URL, applies replacements under the source lock,
and checks freshness on cached-stream reuse. Known entries missing a runtime
during reload now return retryable HTTP 503. Unknown entry/camera lookup remains
404, so a persistent missing-camera error still needs runtime evidence.

Fourteen offline source/route lifecycle tests pass with synthetic HA interfaces.
Coverage includes repeated source requests, timer renewal, idle reuse, concurrent
access, failed renewal, unload races, real account-error propagation, retryable
reload/metadata failures, and unknown-camera/entry rejection. Eight installer
tests pass using local ZIP fixtures and mock downloaders. No vendor requests or
changes to an installed HA were made.

Real HA fixture tests were extended for the two URL regressions. A camera test
attempt in an available secondary HA 2026.2.3 environment stopped during collection
because PyTurboJPEG is missing; no camera assertions ran. The target HA 2026.10.2
environment is unavailable. A secondary proxy run on 2026.2.3 completed with four
download tests passing and five fixture-setup failures: that older release lacks
`device_registry.async_setup`, used by the target-version fixtures. These are not
a clean HA-suite result. The historical shutdown failure below is unresolved.
Real HA playback beyond the ten-minute signature lifetime and after idle/reload
still needs validation. The portable protocol is unchanged, and its 1.3.0 tester
and previous checks were not rerun for this adapter-only change.

## Previous 0.1.3 results

The user reports repeated timeouts in Home Assistant live playback. Version
0.1.3 adds saved-credential recovery for expired account tokens and rejected
renewal, shared login across concurrent requests, and one camera-credential
refresh after device-login rejection. Live transport retries temporary network
failures three times with backoff and resumes only at a keyframe; a video
watchdog prevents audio/control traffic from hiding a stalled picture.

Twenty isolated recovery tests pass using synthetic account and relay sessions.
They cover rejected passwords, account identity, concurrent requests, unload,
device/account error separation, retry limits, healthy-session budget reset,
video stalls and codec changes. Eight installer tests pass with mocked downloads.
These checks make no vendor requests. Recovery is not yet validated in an
installed Home Assistant; the real-camera result below was obtained with 0.1.2.

The user ran the binary-forwarding tester locally against a real account and
one AWT3710D camera. The sanitized report records successful account login and
discovery, protocol 31, 201 decoded H.264 frames at 640×360, and a 10.599-second
sample. Software is `AppEV2W_VA3_V1.3.6.1_20231113`; firmware is
`Hw_AWT3710D_XHR_V1.0_WF_20231113`. Cloud recordings were not requested and
Home Assistant/browser playback was not tested. The developer received no
account credentials and made no vendor requests.

The same binary/1022 transport is now the Home Assistant adapter's default.
The JSON/1002 path remains available in the standalone tester for comparison.
Its earlier code-100/status-1/byte-13 reply is not accepted by relaxing the
legacy check; the working forwarding path has separate reply semantics.

Current offline checks complete with 41 tests passed and one loopback test
skipped because the sandbox cannot bind sockets. Coverage includes binary
packet offsets and credentials, fragmented 26/256/412-byte replies, real H.264
decoding with both selective-AES modes, corrupt-media and rejection handling,
bounded fragments, and the actual Home Assistant client's frame/session path.
The standalone self-test decodes 15 synthetic frames successfully. No full
pytest or Home Assistant suite was rerun because those packages are unavailable
in this environment; the historical shutdown crash below remains unresolved.

## Historical 0.1.1 validation — 19 October 2026

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

## Remaining real-account verification

The user-run report now confirms account login/discovery and direct cloud live
decoding for the camera above. Before claiming complete Home Assistant support,
the remaining checks are:

1. Installed Home Assistant live playback and browser rendering.
2. Long-running picture timing, reconnect and source renewal.
3. Actual UCloud MP4 or S3 TS download and browser playback/seek verification.
4. Resolution of the adapter shutdown-test failure and repetition on the
   intended Home Assistant host.

Newer key-exchange live video remains unimplemented; this is an explicit
compatibility limit independent of the test failure. The included compatibility
probe prompts locally for credentials and can provide sanitized evidence for
the next investigation step.
