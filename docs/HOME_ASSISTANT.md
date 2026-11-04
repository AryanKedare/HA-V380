# Home Assistant boundary

The integration targets **Home Assistant Core 2026.10.2**. The Home Assistant
adapter has been exercised with fixture clients. Direct cloud decoding is now
user-confirmed on one AWT3710D camera, while end-to-end Home Assistant/browser
playback and recording playback remain unverified.

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

Each entity subclasses `Camera` and returns an authenticated HA proxy source
from `stream_source`. The native HA stream integration handles playback and
still images through HLS. The camera overrides Core's supported-provider hook
to decline automatic WebRTC selection for this expiring authenticated MPEG-TS
source. Core therefore advertises HLS without WebRTC; native WebRTC is not
implemented. This does not suppress cloud or source-preparation failures.
Missing camera metadata, an unbound playback proxy and unreadable camera
credentials have separate error messages.

Ten-minute proxy signatures are refreshed after eight minutes;
the existing stream receives the renewed source through `Stream.update_source`.
Source calls share the active URL and do not move its deadline. Reusing an
existing HA stream checks freshness too, because Core only calls `stream_source`
when it first creates a Stream. Source replacement and renewal scheduling happen
under the same lock, with a removal guard after the awaited source request.
Unloading an entity cancels renewal and stops its stream. Account unload and
Home Assistant shutdown close the client's transport resources.

The proxy resolves the current runtime on each request. A known entry temporarily
without a runtime returns HTTP 503 and `Retry-After: 5`. Missing camera metadata
after a failed poll is also retryable. Unknown entries/cameras remain HTTP 404;
HA's signed-path authentication still runs before the view.

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
