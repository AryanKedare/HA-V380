# Test V380 cloud video and the installed Home Assistant integration

Download **v380_full_test.py** (tester version **1.1.0**). It is a standalone script; the integration ZIP
is not needed on the computer running it. Use Python3.11 or newer and an
interactive terminal. Passwords and the optional Home Assistant token are
prompted locally, without echo or command-line arguments.

## Install dependencies and run

From the folder containing the downloaded script:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install aiohttp cryptography av certifi
python v380_full_test.py --self-test
python v380_full_test.py
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` instead.

The self-test needs no account and makes no vendor requests. It verifies the
account/device encryption vectors, decodes15 frames from an embedded synthetic
video, and checks the Home Assistant token-origin guard.

The normal run asks for your regular V380 Pro cloud account and password. It
tests every discovered camera in sequence:

1. Cloud login and device enumeration.
2. Legacy cloud relay authentication, video decryption, and a10-second live
   sample. FFmpeg/PyAV must actually decode video frames for this check to pass.
3. Cloud recording dates and files; the newest recording on the newest date.
4. UCloud MP4 download/decode, or up to3 S3 segments downloaded and decoded.

If V380 Pro offers live video but no saved cloud footage for this camera, run:

```sh
python v380_full_test.py --live-only
```

This skips cloud-recording requests in both direct-cloud and Home Assistant
checks. The report marks recordings `not_requested`; a live-only PASS covers
only login, discovery and the requested live samples.

When live video fails, version 1.1.0 retains a fixed protocol `stage` and
`detail`, plus bounded numeric `code`/`error_id` values when returned by the
service. For example, `device_login_response; login_rejected; code=1000` identifies
the device-login response rather than the account login. Dispatcher, login,
relay-handshake and media failures are distinguished. These fields contain no
camera IDs, endpoints, session handles, credentials or raw response text.
The report retains up to 14 relay-attempt summaries, so a later connection
failure does not hide an earlier server rejection.
Share the new failure line and sanitized report to locate the remaining issue.

The decoder checks up to300 frames per sample. Each live/recording sample is capped at32MiB. A larger required download is
reported as incomplete rather than a playback failure or a pass.

## Also check Home Assistant end to end

Install the integration in Home Assistant first, then run:

```sh
python v380_full_test.py --ha-url http://homeassistant.local:8123
```

Replace the URL with the address you use to reach Home Assistant. The script
prompts separately for a Home Assistant long-lived access token. Create one in
your Home Assistant user profile's security/token settings. Use an account that
can access the integration's entity registry and cameras.

This adds authenticated REST/WebSocket checks, discovers enabled `v380_cloud`
camera entities, requests each camera's HLS stream and decodes its media. It
also follows the integration's recording browser to a representative cloud
clip, resolves it through Home Assistant, and decodes the returned media.

To check only an existing Home Assistant installation:

```sh
python v380_full_test.py --ha-url http://homeassistant.local:8123 --ha-only
```

Home Assistant tokens are sent only to the supplied Home Assistant origin.
External HLS targets and cross-origin WebSocket redirects are refused before
credentials are forwarded. TLS certificate verification stays enabled.

## Deeper or more targeted sampling

```sh
python v380_full_test.py --camera 1 --live-seconds 20 --recordings-per-camera 3 --segments-per-recording 5 --max-download-mib 128
```

`--camera 1` selects the first camera in the returned list; omitting it tests
all discovered cameras. Increasing the sample counts and byte limits increases
the video transferred from the camera/cloud. It does not alter camera settings.

If the recording service requires HTTP, the report says `legacy_http_required`.
The explicit `--allow-insecure-http` flag permits unencrypted vendor recording
requests carrying cloud tokens/video. It does not change Home Assistant's
separate legacy-HTTP preference; that must be configured in the integration.

Footage stays in memory by default. To keep private samples locally, add:

```sh
python v380_full_test.py --save-media ./private-test-footage
```

Previously saved media files are not overwritten. Filenames use camera/sample
indexes rather than vendor camera IDs or names.

## Report and results

The script writes **v380-test-report.json** in the current folder. Choose a
location with `--report ./reports/camera-test.json`.

The report excludes account/camera names and IDs, passwords, tokens, private
keys, signed playback URLs, raw server responses, and footage. It retains only
sample results, counts, dimensions/codecs, numeric error codes and test limits.
You can share this JSON file to continue compatibility debugging. Do not share
saved private footage or credentials unless you independently intend to do so.

| Result | Meaning |
| --- | --- |
| PASS / exit0 | Every requested sampled check passed, including actual video decoding. |
| FAIL / exit1 | At least one authentication, connection, API or decode check failed. |
| INCOMPLETE / exit2 | A check was blocked/unsupported, no recording existed, or a sampling limit was reached. |
| Exit130 | You stopped the script with Ctrl+C. |

The report states which checks were requested. A cloud-only PASS does not
claim that Home Assistant was tested. Home Assistant recording playback samples
one representative clip; direct cloud tests sample each selected camera.

The script cannot prove every historical recording, browser rendering,
long-running reconnect/renewal, all firmware variants, or interpreter shutdown
behavior. In particular it does not resolve the integration's previously
reported HA adapter shutdown-test failure. Newer key-exchange live cameras are
reported as unsupported, even when their saved recordings work.

## Development verification

Twelve offline diagnostic tests pass, covering real video decoding, download/error
bounds, relative and low-latency HLS objects, mocked HA REST/WebSocket workflows,
cross-origin token protection, report privacy, preserved protocol errors,
live-only selection and honest incomplete statuses.
The distributable's embedded self-test also passes with exit0. No real V380
account/camera or installed user Home Assistant was accessed during development.
