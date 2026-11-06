# Home Assistant boundary

The integration targets **Home Assistant Core 2026.11.0**. Current tests use
actual HA authentication, HTTP and the stream worker to generate HLS from
synthetic camera video. Direct cloud decoding is user-confirmed on one AWT3710D.
Real camera/network/browser longevity and recordings still need verification.

## Account and entities

The config flow authenticates and enumerates cameras before saving an entry.
The immutable cloud account ID prevents duplicates and ensures reauthentication
cannot silently switch accounts. Credentials stay in the config entry and its
client; diagnostics contain only aggregate counts. An account can be
reconfigured to change its credentials or the optional legacy HTTP preference.
That preference is off by default and its form explains the unencrypted access.

A coordinator refreshes camera metadata every two minutes. The camera API does
not establish online status from the device list, so an unknown status is kept
distinct from an explicit offline status. Newly discovered cameras are added
and removed cameras become unavailable.

Catalog refreshes commit atomically. Missing sections and recognized partial
responses retain known cameras, while valid owned/shared sections can update
their respective entries. An apparent removal needs two observations. A
suddenly empty catalog performs one saved-credential login before confirmation,
mirroring the session reset provided by an entry reload. Auth/network failures
leave the committed list intact, and unload cannot restore a late response.

Camera availability uses the client catalog and video-worker state. Transient
metadata errors retain access to known cameras. Closed clients, rejected auth,
confirmed removal, explicit offline state and failed video remain unavailable.

Each entity subclasses `Camera` and returns an authenticated HA proxy source
from `stream_source`. The native HA stream integration handles playback and
still images through HLS. The camera overrides Core's supported-provider hook
to decline automatic WebRTC selection for this expiring authenticated MPEG-TS
source. Core therefore advertises HLS without WebRTC; native WebRTC is not
implemented. This does not suppress cloud or source-preparation failures.
Missing camera metadata, an unbound playback proxy and unreadable camera
credentials have separate error messages.

Ten-minute proxy signatures are refreshed after eight minutes. Renewal updates
the public Stream `source` for its next connection, preserving the current HTTP
response. It does not call the restart-inducing `update_source`; Core reads the
fresh URL on a genuine retry.
Source calls share the active URL and do not move its deadline. Reusing an
existing HA stream checks freshness too, because Core only calls `stream_source`
when it first creates a Stream. Source replacement and renewal scheduling happen
under the same lock, with a removal guard after the awaited source request.
Idle/lost-output streams are retired before new viewers can attach, so old
worker cleanup cannot remove the new object's HLS output. Retirement copies
preload settings before stopping, preserving camera preferences. Unloading an
entity cancels renewal and joins active/retiring workers. Account unload and
Home Assistant shutdown close the client's transport resources.

The proxy resolves the current runtime and client catalog on each request. A known entry temporarily
without a runtime returns HTTP 503 and `Retry-After: 5`. Missing camera metadata
after a failed/incomplete poll is also retryable. Known cameras use the same
client snapshot as source resolution even if the coordinator has older data.
Unknown entries/cameras after a complete snapshot remain HTTP 404;
HA's signed-path authentication still runs before the view.

The first-video budget is 25 seconds, below Core's 30-second source-open/read
budget. A disconnect watcher cancels the producer even before headers exist;
both tasks finish before the iterator closes. This prevents abandoned readers
from leaving overlapping sessions. Failures record only revalidated enums and
numbers with startup/stream phase; normal viewer closure is not a relay failure.

## Recording browser and media security

`media_source.py` exposes account → camera → date → recording. Its identifiers
contain opaque IDs, and recording selection requests a fresh HA playback URL.
The server resolves vendor download authorization; it does not place vendor
tokens or vendor object URLs in the browser's HLS playlist.

HA's authentication middleware protects live, recording and segment routes.
Playback URLs use temporary signatures tied to the exact request path. HLS
playlists contain separately signed HA segment routes. MP4 proxy responses
forward byte ranges and retain status 206 and `Content-Range` so seeking works.
Cloud protocol or codec variants not implemented by the client produce an
explicit error, without guessed sources or local-camera fallback.

## Verification

For 0.1.8, relay state belongs to the account client and survives HTTP transport
replacement. Recent working routes precede dispatcher discovery; cancelled
starts and failed routes move behind alternatives. Route hints expire and stay
in memory. Startup timeout progress triggers one saved-session/catalog refresh
on the next request with a shared five-minute cooldown. Real auth rejection
still propagates. The full Core 2026.11.0 / Python 3.14.10 suite completes with
179 tests and 14 subtests, exit 0, including 16 new routing/recovery regressions.
Actual failed-stage propagation is covered through authenticated local HTTP.
No installed HA or real vendor account was accessed.

For 0.1.7 the full Core 2026.11.0 / Python 3.14.10 run completes with **163 tests
and 14 subtests passed**, exit 0. Seven real-HA pipeline tests cover local HTTP,
Core demux/HLS, fMP4 decoding, uninterrupted renewal, retry, idle replacement,
slow startup, disconnect cleanup and failure privacy. The old camera reproduces
`Stream is not configured for format 'hls'` during worker backoff. The historical
2026.10.2 shutdown failure is not reproduced in this current target run; its
original cause is not identified. Real vendor/browser longevity is separate.


For 0.1.6, 94 offline checks pass, including 20 new catalog refresh regressions
and 24 source/route lifecycle checks. The 0.1.5 partial-reply cache erasure is
reproduced against its source. Confirmed removals, failed confirmation, concurrent
refreshes, token expiry and unload are covered. The standalone tester is rebuilt
from matching protocol sources and its 15-frame video self-test passes. The
installed HA/browser run remains outside these checks.

For 0.1.5, 20 source/route contracts, 20 auth/relay recovery checks and eight
installer checks pass. A separate harness executes the exact Core 2026.10.4
`async_refresh_providers`, supported-provider hook and camera-capability methods
with synthetic dependencies. It reproduces automatic HTTP provider selection
on 0.1.4 and confirms only HLS afterward, without provider/source lookups. A real
HA fixture test was also added for HLS-only capabilities, but the full target HA
suite and browser session remain unavailable in this environment.

For 0.1.4, 14 offline source/route lifecycle contracts pass with synthetic HA
interfaces. Real HA fixture tests were extended for non-postponed renewal and
cached Stream reuse. The available secondary HA 2026.2.3 environment cannot
collect the camera tests because PyTurboJPEG is missing. The target HA 2026.10.2
environment is unavailable, so this is not a clean target-version suite pass.
The secondary proxy run has four passing download tests and five fixture failures
because that older HA lacks the target's device-registry setup API.

For 0.1.3, synthetic recovery fixtures exercise the account and portable live
API: expired-token relogin, concurrent request sharing, one device-credential
refresh, bounded relay reconnects, video-stall detection and keyframe resumption.
Invalid camera credentials remain separate from cloud-account reauthentication.
Real Home Assistant timeout recovery has not yet been verified.

For 0.1.2, the adapter's portable API streams independently encrypted H.264
through binary login and mode-1022 forwarding in an offline fixture, then
releases its session. This uses the actual `V380CloudClient.async_live_frames`
path without importing Home Assistant. The full HA suite was not rerun.

Historical 0.1.1 validation:

Nine proxy tests complete with exit0 using real HA authentication, signing and
request middleware. They cover scoped playback authorization, account/camera
isolation, vendor-token concealment, range responses and HTTP200 error bodies.
The combined account/live/video/recordings/proxy/shared run completes with39
tests passing plus3 subtests.

All15 adapter assertions pass before an unresolved interpreter shutdown crash.
The crash reproduces on Python3.14.0,3.14.2 and3.14.8 on this macOS host; it cannot
be attributed to the older runtime. The responsible interaction has not been
isolated, and it is not represented as a clean adapter-suite pass. See
[validation details](VALIDATION.md) for the observed boundary and remaining work.
Those fixtures make no vendor requests. Separate user-run direct-cloud evidence
and its limits are recorded in the validation notes.

## Primary API references

- [Camera entity API](https://developers.home-assistant.io/docs/core/entity/camera)
- [HA 2026.10.2 camera implementation](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/camera/__init__.py)
- [Media source API](https://developers.home-assistant.io/docs/core/platform/media_source)
- [HA 2026.10.2 media source models](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/media_source/models.py)
- [Config flow API](https://contributors.home-assistant.io/core/integrations/config-flow/)
- [HA HTTP signing and authentication](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/http/auth.py)
- [Expiring camera source pattern in Nest](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/components/nest/camera.py)
