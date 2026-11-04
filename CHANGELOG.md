# Changes

## 0.1.5 — 4 November 2026

- Keep V380 cameras on Home Assistant's HLS stream pipeline by declining
  automatically selected WebRTC providers. The local authenticated MPEG-TS URL
  requires its source lifecycle to be managed by the HA stream path; native
  WebRTC is not implemented. Streaming and stream-derived stills remain enabled.
- Distinguish an absent camera, an uninitialized local playback proxy, and
  unreadable camera credentials from generic cloud response failures. Preserve
  genuine account reauthentication and bounded renewal retries.
- Log only bounded diagnostic fields for source failures, never exception text,
  credentials, URLs or server responses. Pin the installer to 0.1.5.
- Offline validation: 20 source/route lifecycle checks, 20 auth/relay recovery
  checks and eight installer checks pass. A separate contract check runs the
  exact Core 2026.10.4 provider/capability methods with a synthetic backend.

The user reports Core 2026.10.4 playback stops with a WebRTC source-preparation
error after 0.1.4 worked initially. This change uses HLS and improves diagnosis;
it does not establish that a separate cloud or metadata failure is resolved.
Installed HA/browser playback still needs verification. The portable protocol
and standalone tester 1.3.0 are unchanged.

## 0.1.4 — 4 November 2026

- Fix signed Home Assistant live URL renewal: repeated source requests now
  reuse the active URL without postponing its eight-minute renewal deadline.
  Issuing and applying a replacement source happen together under one lock.
- Refresh an existing HA stream before reuse when its renewal deadline passed
  while idle. Cancel renewal and reject late results during entity unload.
- Return retryable HTTP 503 for a known integration temporarily unloaded or
  unavailable camera metadata after a failed poll. Preserve HTTP 404 for unknown
  accounts and cameras, and keep all media routes authenticated.
- Pin the installer to the 0.1.4 component. Add 14 offline source/route lifecycle
  regressions; all pass. Eight isolated installer tests also pass.

The user reports playback starts in HA on 0.1.3, then fails with local 404/401
responses. URL lifecycle defects are covered offline; stable real HA playback
after this fix still needs verification. The standalone protocol tester remains
1.3.0 because the portable cloud transport is unchanged.

## 0.1.3 — 3 November 2026

- Recover expired account tokens automatically using saved credentials, with
  one retry and a shared login for concurrent requests. Fall back to saved
  credentials if token renewal is rejected; retain real password reauthentication.
- Reconnect interrupted live sessions with a fresh device login, three bounded
  retries, backoff and keyframe resumption. Detect video stalls even when audio
  continues. Close old sessions before retrying and stop recovery on unload.
- Refresh camera credentials once after device-login rejection. Persistent
  device rejection no longer starts an unrelated cloud-password prompt.
- Standalone tester 1.3.0 uses the same recovery and reports live reconnect count.
- Add `install.sh` with a pinned 0.1.3 default, configurable Home Assistant
  path/ref, offline source support, staged replacement, backups and rollback.
- Add isolated installer tests for fresh installs, upgrades, failure recovery,
  config preservation, download failures, and curl/wget download paths.
- Offline checks: 41 forwarding/API/video/recovery tests passed, one loopback
  test skipped; eight installer tests and the standalone self-test passed.

Automatic recovery has synthetic coverage; real Home Assistant playback and
long-running reconnection still need validation. The historical adapter-test
shutdown crash remains unresolved.

## 0.1.2 — 3 November 2026

- Use binary device login and mode-1022 cloud forwarding for Home Assistant
  live sessions. The previous JSON/mode-1002 session failed the user's camera.
- Keep device-login protocol versions separate from the opaque forwarding
  reply body; retain bounded diagnostics and close sessions on failure/unload.
- Support bounded variable-length relay replies and both selective video-AES
  modes. Device username/password variants are tried only for specific errors.
- Standalone tester 1.2.0 uses forwarding by default; `--live-transport legacy`
  retains the old path for comparison. Remove the unconfirmed code-100 bypass.
- User-run validation on one AWT3710D camera: protocol 31, 201 decoded H.264
  frames, 640×360, 10.599 seconds. This establishes direct-cloud sampling only.
- Current forwarding/API/video checks: 21 passed, one loopback test skipped by
  sandbox restrictions; standalone self-test decoded 15 synthetic frames.

Home Assistant/browser playback and cloud recordings remain unverified. The
previously documented adapter shutdown-test crash was not resolved or rerun.

## 0.1.1 — 19 October 2026

- Preserve the failed live-protocol stage and bounded server result/error codes
  when every cloud relay fails. The previous wrapper discarded these details.
- Distinguish dispatcher DNS/connect/read, device-login, relay-handshake and
  media failures without exposing raw server responses or private identifiers.
- Standalone tester 1.1.0 adds `--live-only` for cameras without cloud recordings;
  it skips recording requests in both direct-cloud and Home Assistant checks.
- Add regression tests for diagnostics privacy, relay failure propagation and
  live-only scope. Protocol/proxy/diagnostic suite: 53 passed plus 3 subtests.

Real-camera live playback remains unresolved. This release improves diagnosis;
it does not claim to correct the unknown transport failure in the user's report.
The separately documented Home Assistant adapter shutdown-test crash remains.
