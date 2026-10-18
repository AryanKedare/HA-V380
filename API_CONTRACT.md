# Home Assistant adapter contract

This adapter targets Home Assistant Core 2026.10.2. It is experimental and does
not establish that live video or recordings work against an actual V380 account.
The protocol client must supply genuine playable sources; unsupported variants
must raise `UnsupportedError` rather than return guessed or placeholder URLs.

`custom_components/v380_cloud/api.py` supplies these public names:

```python
class V380CloudClient:
    def __init__(self, session, username: str, password: str, *, allow_insecure_http: bool = False): ...
    async def async_login(self) -> str: ...  # immutable cloud account ID
    async def async_devices(self) -> list[CloudDevice]: ...
    async def async_live_source(self, device_id: str) -> str: ...
    async def async_recording_dates(self, device_id: str) -> list[str]: ...
    async def async_recordings(self, device_id: str, day: str) -> list[CloudRecording]: ...
    async def async_recording_source(self, device_id: str, recording_id: str) -> str: ...
    async def async_close(self) -> None: ...
```

- `CloudDevice` has `device_id: str`, `name: str`, and `online: bool | None`.
  An unknown status is not represented as offline. Camera metadata is polled
  every two minutes without starting live video.
- `CloudRecording` has `recording_id: str`, `title: str`, and `mime_type: str`.
  Recording dates use canonical `YYYY-MM-DD` strings in the service's recording
  calendar. The adapter does not change their timezone or invent missing dates.
- `AuthError` identifies rejected or expired account authorization; `CloudError`
  identifies service/network failures; `UnsupportedError` identifies protocols
  not implemented. Error messages and exception causes must exclude credentials,
  authentication tokens, private keys, and token-bearing URLs.
- Login and device enumeration must validate response bodies. An empty account
  is permitted; fabricated camera entities are not.
- The optional `allow_insecure_http` preference defaults to false. The UI
  explicitly explains that legacy HTTP recording access sends cloud credentials
  or tokens unencrypted to the vendor. The client must enforce this preference
  for both API requests and resulting playback URLs; it cannot silently enable
  HTTP or bypass the setting through a redirect.
- The caller owns the shared aiohttp session. `async_close` must be idempotent,
  release only resources owned by this client, and stop any relay/refresh tasks.
  It is called after flow validation, on setup failure, on unload, and on stop.
- Source URLs must work with their consumers without the Android runtime.
  A camera source must be consumable by Home Assistant's stream integration.
  A recording source must be playable by the browser/media player, include only
  narrowly scoped playback authorization, and require no private session header.
  If those requirements need an authenticated proxy or media conversion, the
  protocol layer must provide it; raw encrypted objects are not playable URLs.
- The camera requests a source when Home Assistant creates a stream. Any live
  vendor-session keepalive or expiry handling needed while that source remains
  open belongs to the client/relay. A future replacement source must be pushed
  into HA's active stream with `Stream.update_source` if renewal changes its URL.
- The media browser stores opaque account/camera/date/recording IDs, then checks
  the selected recording and resolves a fresh URL when playback is requested.
  Passwords and playback URLs are never put into entity state or diagnostics.

The account runtime is `entry.runtime_data: V380RuntimeData`, also accessible as
`hass.data["v380_cloud"][entry_id]`. It contains `client`, `coordinator`, and
`account_id`. The account-specific metadata lives in `coordinator.data` keyed by
device ID. A removed device becomes unavailable; newly shared cameras are added
when the next metadata update arrives.

API references: [camera entities](https://developers.home-assistant.io/docs/core/entity/camera),
[media sources](https://developers.home-assistant.io/docs/core/platform/media_source),
[config flows](https://contributors.home-assistant.io/core/integrations/config-flow/).
