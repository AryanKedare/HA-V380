# Changes

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
