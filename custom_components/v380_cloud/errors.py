"""Public errors; messages deliberately exclude server responses and credentials."""

class CloudError(Exception):
    """V380 service request failed or returned an unsupported response."""

class AuthError(CloudError):
    """Account authentication expired or was rejected."""

class UnsupportedError(CloudError):
    """A protocol variant has not been implemented."""


class LegacyHttpRequired(UnsupportedError):
    """A vendor recording endpoint needs the explicit HTTP preference."""
