"""Public errors; messages deliberately exclude server responses and credentials."""

class CloudError(Exception):
    """V380 service request failed or returned an unsupported response."""

    _STAGES = frozenset({
        "device_credentials", "dispatcher_connect", "dispatcher_response",
        "device_login_connect", "device_login_response", "relay_connect",
        "relay_handshake", "media_stream",
    })
    _REASONS = frozenset({
        "dns_failed", "connection_failed", "network_timeout", "incomplete_response",
        "dispatcher_rejected", "no_relays", "invalid_framing", "invalid_json",
        "request_mismatch", "invalid_result", "login_rejected", "missing_session",
        "media_version_unsupported", "relay_rejected", "media_packet_invalid",
        "video_key_invalid", "packet_type_unsupported", "invalid_metadata",
        "session_failed",
    })

    def __init__(self, message: str = "Cloud service request failed", *, stage: str | None = None,
                 reason: str | None = None, code: int | None = None,
                 error_id: int | None = None, protocol_version: int | None = None,
                 packet_type: int | None = None) -> None:
        super().__init__(message)
        # Only fixed enums and bounded protocol numbers may enter a report.
        # Messages, endpoints, handles, sessions and device IDs never do.
        self.diagnostic = {}
        self.relay_attempts: list[dict] = []
        if stage in self._STAGES:
            self.diagnostic["stage"] = stage
        if reason in self._REASONS:
            self.diagnostic["detail"] = reason
        for name, value, low, high in (
            ("code", code, -65535, 65535),
            ("error_id", error_id, -65535, 65535),
            ("protocol_version", protocol_version, 0, 255),
            ("packet_type", packet_type, 0, 255),
        ):
            if type(value) is int and low <= value <= high:
                self.diagnostic[name] = value

class AuthError(CloudError):
    """Account authentication expired or was rejected."""

class UnsupportedError(CloudError):
    """A protocol variant has not been implemented."""


class LegacyHttpRequired(UnsupportedError):
    """A vendor recording endpoint needs the explicit HTTP preference."""
