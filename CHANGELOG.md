# Changes

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
