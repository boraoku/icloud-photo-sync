"""Program-specific exception types.

Everything that talks to iCloud lives behind :mod:`icloud_photo_sync.icloud_client`
and raises these types, so the rest of the code never has to know about
``pyicloud``'s exception hierarchy. This keeps the engine swappable (see the
fallback strategy in the README).
"""

from __future__ import annotations


class ICloudSyncError(Exception):
    """Base class for all errors raised by this program."""


# --- Authentication / session ------------------------------------------------


class AuthenticationError(ICloudSyncError):
    """Login failed (bad password, rejected code, etc.)."""


class SessionExpiredError(AuthenticationError):
    """The persisted session is no longer valid; the user must run ``login``."""


class TwoFactorRequiredError(AuthenticationError):
    """A 2FA/2SA code is required but none was supplied (non-interactive runs)."""


class AcceptTermsError(AuthenticationError):
    """Apple requires accepting new terms & conditions before continuing."""


class AccountPreconditionError(ICloudSyncError):
    """The account is configured in a way that blocks web/CloudKit access.

    Typically: Advanced Data Protection is ON, "Access iCloud Data on the Web"
    is OFF, or a hardware security key is the only 2FA factor. Carries a
    human-readable remediation message.
    """


# --- Library / enumeration ---------------------------------------------------


class LibraryIndexingError(ICloudSyncError):
    """iCloud is still indexing the photo library; retry later."""


# --- Transfer ----------------------------------------------------------------


class TransientError(ICloudSyncError):
    """A retryable error (connection reset, timeout, HTTP 503, etc.)."""


class ServiceUnavailableError(TransientError):
    """Apple returned HTTP 503 / rate-limited the request."""


class IntegrityError(ICloudSyncError):
    """A finished transfer did not match the expected size."""


class DownloadError(ICloudSyncError):
    """A non-retryable download problem (no original URL, 4xx, etc.)."""


class OperationCancelled(ICloudSyncError):
    """The user asked to stop (SIGINT/SIGTERM)."""
