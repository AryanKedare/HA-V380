# V380 Cloud for Home Assistant — experimental 0.1.5

A custom Home Assistant integration built from static analysis of the supplied
V380 Pro **2.2.90** APKs. It signs in to the V380 cloud account, creates camera
entities, and exposes saved cloud footage through Home Assistant's Media browser.
A local camera IP address or Android runtime is not required.

**Direct cloud live decoding is confirmed on one AWT3710D camera.** A user-run
test decoded 201 H.264 frames at 640×360 over 10.599 seconds, using device
protocol 31. The camera runs `AppEV2W_VA3_V1.3.6.1_20231113` software and
`Hw_AWT3710D_XHR_V1.0_WF_20231113` firmware. Home Assistant/browser playback,
long-running sessions, recordings and other firmware remain unverified.

Live video now uses binary cloud forwarding (mode 1022), which passed that test.
The earlier JSON/1002 path failed this camera's stream handshake. Newer cameras using
MQTT/key exchange (`type >= 2`) are explicitly unsupported for live video.
Their saved cloud recordings can still be supported independently.

The Home Assistant adapter tests also have an unresolved interpreter shutdown
crash, described below. This is a development build for controlled testing.

## Install

Download [install.sh](install.sh) and run it in your Home Assistant terminal:

```sh
sh install.sh --config-dir /config
```

The installer downloads the pinned 0.1.5 commit by default. It needs `curl`
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
`.venv/bin/python v380_full_test.py --live-only`. Tester 1.3.0 uses the same
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
| Live repeatedly times out | Install 0.1.4 and restart Home Assistant. Automatic token recovery, relay reconnects and playback URL renewal are enabled. If playback still stops, capture the exact Home Assistant error line with the URL query removed. |
| HA live URL returns 401 / 404 | A local playback authorization or route lookup failed. Version 0.1.4 fixes renewal and cached-stream reuse, and makes temporary reload failures retryable. A persistent 404 still needs account/camera availability checks. |
| Failed to start WebRTC stream | Install 0.1.5, restart HA and refresh the dashboard. V380 advertises HLS only. Use the built-in camera view if a custom card explicitly forces WebRTC. |
| Camera missing / playback proxy not ready | The new source-specific message identifies a local lookup/setup issue. Confirm the camera is in V380 Pro and reload the integration. A cloud outage or invalid camera credentials still require recovery of that underlying condition. |
| Recording requires legacy HTTP | Use the explicit Reconfigure preference described above. |
| No dates | Confirm this account can view saved cloud recordings in V380 Pro. |
| Recording opens but browser cannot decode it | Try a browser/device supporting its codec. Recordings are not transcoded. |

Integration diagnostics contain only aggregate counts. Do not share passwords,
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

The protocol, video and proxy tests complete successfully. All 15 Home Assistant
adapter assertions also pass, but that test process crashes during interpreter
shutdown/garbage collection. The crash reproduces with Python 3.14.0, 3.14.2 and
3.14.8 on this macOS host; its cause is unresolved. It must not be presented as
a clean full-suite pass or attributed to an outdated Python runtime.

Version 0.1.5 passes 20 offline source/route lifecycle tests, 20 authentication
and relay recovery tests, and eight installer tests. Synthetic HA interfaces
cover provider opt-out, HLS streaming support, source-specific errors, diagnostic
privacy, renewal, idle reuse, unload and routing. An additional contract check
executes the exact Core 2026.10.4 provider/capability methods with a synthetic
backend and confirms HLS-only capabilities after the change. This is not a full
installed HA or browser run. See the validation notes for the separate HA test
limitations and previous protocol results.

The user-run report establishes direct cloud playback on the camera listed
above. Home Assistant/browser playback, reconnect behavior, long-running source
renewal, recordings and other camera models still need verification.
See [validation notes](docs/VALIDATION.md) and
[Home Assistant implementation notes](docs/HOME_ASSISTANT.md).

Development target: Home Assistant Core **2026.10.2**, Python **3.14.2+**.
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
