# V380 Cloud for Home Assistant — experimental 0.1.8

A custom Home Assistant integration built from static analysis of the supplied
V380 Pro **2.2.90** APKs. It signs in to the V380 cloud account, creates camera
entities, and exposes saved cloud footage through Home Assistant's Media browser.
A local camera IP address or Android runtime is not required.

**Direct cloud live decoding is confirmed on one AWT3710D camera.** A user-run
test decoded 201 H.264 frames at 640×360 over 10.599 seconds, using device
protocol 31. The camera runs `AppEV2W_VA3_V1.3.6.1_20231113` software and
`Hw_AWT3710D_XHR_V1.0_WF_20231113` firmware. Stable long-running Home Assistant/
browser playback, recordings and other firmware remain unverified.

Live video now uses binary cloud forwarding (mode 1022), which passed that test.
The earlier JSON/1002 path failed this camera's stream handshake. Newer cameras using
MQTT/key exchange (`type >= 2`) are explicitly unsupported for live video.
Their saved cloud recordings can still be supported independently.

The current full suite passes on Home Assistant Core 2026.11.0 and Python
3.14.10. Real camera, network and browser longevity still need verification.

## Install

Download [install.sh](install.sh) and run it in your Home Assistant terminal:

```sh
sh install.sh --config-dir /config
```

The installer downloads the pinned 0.1.8 commit by default. It needs `curl`
or `wget` and `unzip`. Change `--config-dir` for a different configuration path;
the directory must already contain `configuration.yaml`. Existing component
files are backed up under `.v380-cloud-backups` in that config directory.
Installation is staged before replacement, and a failed replacement restores
the previous component. Restart Home Assistant afterward and keep the existing
integration entry.

For an offline install from an extracted repository, use
`sh install.sh --source . --config-dir /config`. To choose another GitHub branch,
tag or commit, use `--ref REF` instead of `--source`. The installer does not
request credentials or change `configuration.yaml` or saved integration entries.

To install manually:

1. Download and extract this repository using **Code → Download ZIP**, or clone it:

   ```sh
   git clone https://github.com/AryanKedare/HA-V380.git
   ```
2. Copy `custom_components/v380_cloud` into your Home Assistant configuration
   directory, so the manifest is at `/config/custom_components/v380_cloud/manifest.json`
   on a typical Home Assistant OS installation.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → V380 Cloud**.
5. Enter the same email/phone account and password used for regular V380 Pro
   sign-in. For a phone account, use the country-code/account format used by the
   app. Enter the account password directly in Home Assistant.

This is a manual custom-component installation from
[AryanKedare/HA-V380](https://github.com/AryanKedare/HA-V380).

## Test cloud and Home Assistant playback

Use the standalone [v380_full_test.py](v380_full_test.py) to test cloud login,
camera discovery, actual live-video decoding and saved-recording decoding.
It can also check an installed Home Assistant integration with `--ha-url`.
See [V380_TESTING.md](V380_TESTING.md) for setup, sampling limits and results.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install aiohttp cryptography av certifi
.venv/bin/python v380_full_test.py --self-test
.venv/bin/python v380_full_test.py
```

If the camera has no cloud recordings, use
`.venv/bin/python v380_full_test.py --live-only`. Tester 1.5.0 uses the same
forwarding transport as the integration by default. For protocol comparison,
`--live-transport legacy` selects the older JSON/1002 path. Failures retain
bounded stage/result diagnostics; no response bodies or credentials are printed.

Credentials are prompted locally. The default report contains sanitized results;
footage is saved only when `--save-media` is supplied. The script reports newer,
unsupported live transports as incomplete.

## Watch live video

Open the camera entity created under the integration, or add a Picture Entity
card to a dashboard. Replace the example entity with your actual camera entity:

```yaml
type: picture-entity
entity: camera.your_camera
camera_view: live
show_name: true
show_state: false
```

Home Assistant fetches the remote cloud relay, decrypts supported compressed
video, and serves it through its normal streaming component. Live audio is
currently omitted. H.264 is the best-supported browser format; H.265 depends on
the browser and device. Multi-lens panorama reconstruction is not implemented.

Playback uses Home Assistant's HLS pipeline. Version 0.1.5 declines automatic
WebRTC providers for this authenticated MPEG-TS proxy; matching an HTTP URL scheme
does not establish compatibility with its expiring signatures or shared stream.
Native WebRTC is not implemented. After updating, restart HA and refresh the
dashboard so the camera capabilities are reloaded. A custom card explicitly
forcing WebRTC must use HLS instead, or open the camera's built-in entity view.

Automatic authentication recovery is enabled without an extra setting. When
an account token expires, the integration signs in once with the credentials
already saved in Home Assistant and retries the request. Concurrent requests
share that login. If the saved password is rejected, Home Assistant still asks
you to reauthenticate.

Live sessions reconnect after temporary network failures and a 15-second video
stall, using a fresh device login. Recovery allows three consecutive retries
with 1/2/4-second delays and resumes video only from a keyframe. Thirty seconds
of healthy video restores the retry budget. A device-password rejection refreshes
cloud camera credentials once; it does not immediately prompt for your cloud
account password. Long outages, codec changes and unsupported firmware still
stop playback. Real Home Assistant recovery needs testing on your installation.

Version 0.1.4 also fixes local Home Assistant playback URL renewal. Signed URLs
last ten minutes; the active stream receives a new URL after eight minutes.
Repeated source requests reuse that URL without postponing its renewal timer,
and an idle cached stream is refreshed before reuse. This prevents those paths
from retrying an expired signature. A configured account temporarily unavailable
during reload returns HTTP 503 with `Retry-After`, rather than a permanent 404.
Unknown accounts and cameras still return 404. No authentication checks are bypassed.

Version 0.1.6 fixes camera loss during cloud device-list refreshes. Missing or
partial owned/shared sections update known cameras without treating omissions
as removals. An apparent removal is checked with one additional request. If a
previously populated catalog becomes empty, one saved-credential session refresh
is performed before confirmation, providing the recovery that a manual reload
can otherwise trigger. Confirmed removals are still honored. Concurrent refreshes
share a committed result, and playback routes use that same client snapshot.

Version 0.1.7 refreshes the reconnect URL without stopping active HTTP video.
Idle streams are retired before a new viewer adds HLS outputs, isolating old
worker cleanup. A temporary metadata-poll error no longer hides a known camera
while video is healthy; confirmed removal, auth rejection and video failure
remain unavailable. Startup is bounded to 25 seconds within Core's 30-second
reader budget, and a disconnected viewer promptly cancels its upstream session.
Diagnostics include sanitized `live_stream_failures` and `last_live_failure`.

Version 0.1.8 retains per-camera relay history across HTTP reconnects. A recent
working relay is tried before discovery; failed or cancelled startup attempts
move behind alternatives. Cached routes expire after 15 minutes, and failed
routes are deprioritized for 60 seconds. Fresh discovery remains available.

After a startup network timeout, the next request can refresh the saved account
session and camera catalog automatically, at most once per account every five
minutes. An account API outage can still use cached device credentials; a real
password rejection starts normal reauthentication. Diagnostics retain the actual
dispatcher/login/relay/media stage and bounded progress counters, plus
`live_session_recovery_attempts`. Relay addresses and credentials stay private.

## Browse saved cloud footage

Open **Media → V380 Cloud recordings → account → camera → date → recording**.
The integration uses the account's existing cloud-storage access and retention.
A cloud recording must already exist and be accessible in V380 Pro.

Supported implementations:

- Legacy UCloud date/file catalogs and MP4 downloads.
- S3 date/file catalogs, temporary credentials, and MPEG-TS segments presented
  as an HLS recording. SigV4 requests are signed inside Home Assistant.

Vendor credentials and vendor download URLs stay on the backend. The browser
receives temporarily signed Home Assistant playback URLs. MP4 requests retain
byte-range support for seeking.

Some V380 recording servers use HTTP. The integration defaults to refusing
those requests. If the Media browser reports that legacy HTTP is required, open
**Settings → Devices & services → V380 Cloud → Reconfigure** and enable
**Allow unencrypted legacy cloud recording access** only if you accept sending
cloud tokens and video over those vendor HTTP connections. This preference does
not change the camera's settings.

## Compatibility and troubleshooting

| Result | Meaning / next step |
| --- | --- |
| Login rejected | Check the regular cloud account/password and phone account format. Social login is not implemented. |
| Camera appears but live transport is unsupported | The account may contain a newer key-exchange camera. Its live protocol needs further work; check recordings separately. |
| Live fails to start | Regional routing, firmware framing or device authentication may differ. Capture the error and camera software/firmware version for diagnosis. |
| Live repeatedly times out / reload restores video | Install 0.1.8 and restart Home Assistant. Relay history survives reconnects, and startup timeouts allow a bounded automatic session refresh. If it persists, share the sanitized last_live_failure diagnostic. |
| HA live URL returns 401 / 404 | A local playback authorization or route lookup failed. Version 0.1.4 fixes renewal and cached-stream reuse, and makes temporary reload failures retryable. A persistent 404 still needs account/camera availability checks. |
| Failed to start WebRTC stream | Install 0.1.8, restart HA and refresh the dashboard. V380 advertises HLS only. Use the built-in camera view if a custom card explicitly forces WebRTC. |
| Camera missing / playback proxy not ready | Version 0.1.6 recovers incomplete catalogs automatically. If a camera remains absent, confirm its account membership in V380 Pro and check integration diagnostics. An uninitialized local proxy still needs the entry to finish setup. |
| Camera works after reload, then disappears | Install 0.1.8. Partial catalog replies retain known cameras, and a suddenly empty catalog gets one automatic session refresh. Integration diagnostics include catalog completeness, response result and recovery counts. |
| Immediate exit / HLS format not configured | Install 0.1.8 and restart HA. Renewal keeps active video running, and old-worker cleanup is isolated from new viewers. Persistent upstream failure is reported with sanitized phase/stage/result diagnostics. |
| Recording requires legacy HTTP | Use the explicit Reconfigure preference described above. |
| No dates | Confirm this account can view saved cloud recordings in V380 Pro. |
| Recording opens but browser cannot decode it | Try a browser/device supporting its codec. Recordings are not transcoded. |

Integration diagnostics contain aggregate counts and sanitized stage/result enums. Do not share passwords,
access tokens, Home Assistant signed playback URLs, or raw credential responses.
For troubleshooting, the displayed error and camera firmware/software versions
are usually enough to identify the next protocol branch to investigate.

## Optional account compatibility probe

`tools/probe.py` can check the account from a terminal without Home Assistant.
It prompts for credentials, does not save them, and prints a sanitized JSON
report without camera IDs or names. It contacts the account's V380 cloud
services; it does not change device settings or save footage.

Use Python 3.11+ in a virtual environment with `aiohttp` and `cryptography`:

```sh
python3 -m venv .venv
.venv/bin/pip install aiohttp cryptography
.venv/bin/python tools/probe.py
```

To check one remote keyframe and the newest recording's container:

```sh
.venv/bin/python tools/probe.py --test-live --test-recordings
```

Select another camera with `--camera 2`. The separate
`--allow-insecure-http` flag permits the legacy HTTP recording requests described
above. No password is accepted as a command-line argument.

## Validation and remaining work

The test suite includes real loopback dispatcher/login/relay and account
fixtures, independent video decoding with FFmpeg/PyAV, an independent botocore
signature comparison, and real Home Assistant authentication/proxy middleware.
These checks make no requests to a real vendor account.

The full suite passes **179 tests and 14 subtests**, exit status 0, on Core
2026.11.0 / Python 3.14.10. It includes real HA authentication, a live local HTTP
video fixture, Core's worker, HLS generation and independent fragment decoding.
Renewal, retry, idle reuse, disconnect cleanup, metadata outage and privacy are
covered. Sixteen new regressions cover relay cache reuse, cancelled startup
failover, discovery outages, expiry, account recovery cooldown/concurrency and
genuine password rejection. The HTTP startup test verifies the actual failed
stage and records the recovery trigger. The exact missing-HLS error reproduces against 0.1.6 during renewal in
worker backoff; the patched worker retains its HLS output.

The historical Core 2026.10.2 adapter shutdown issue did not occur in this current
run; its original cause remains undetermined. See the validation notes for scope
and history. The standalone tester is 1.5.0, with matching protocol hashes
and a passing 15-frame video self-test.

The user-run report establishes direct cloud playback on the camera listed
above. Home Assistant/browser playback, reconnect behavior, long-running source
renewal, recordings and other camera models still need verification.
See [validation notes](docs/VALIDATION.md) and
[Home Assistant implementation notes](docs/HOME_ASSISTANT.md).

Development target: Home Assistant Core **2026.11.0**, Python **3.14.2+**;
checked with **3.14.10**.
Install `requirements-dev.txt` in an isolated environment to run `pytest tests`.
The standalone protocol tests do not require importing Home Assistant.

## Analysis evidence

- [Investigation ledger and account protocol](research/INVESTIGATION.md)
- [Legacy live relay protocol](research/LIVE_PROTOCOL.md)
- [Saved-recording protocol](research/RECORDINGS_PROTOCOL.md)
- [Account JSON schema audit](research/apk_account_schema.json)
- [Recording JSON schema audit](research/apk_recording_schema.json)

The supplied APKs, extracted native libraries, account credentials, and vendor
recordings are not included. The integration contains a Python reimplementation;
no Android native code is redistributed.
