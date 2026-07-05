"""map_api_error classification: these drive retry / login-guidance /
remediation behaviour, so each class of pyicloud failure must map precisely."""

from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudServiceNotActivatedException,
)

from icloud_photo_sync.errors import (
    AccountPreconditionError,
    AuthenticationError,
    ICloudSyncError,
    LibraryIndexingError,
    ServiceUnavailableError,
    SessionExpiredError,
    TransientError,
)
from icloud_photo_sync.icloud_client import map_api_error


def test_missing_webservice_is_precondition_not_indexing():
    exc = PyiCloudServiceNotActivatedException("Webservice not available: ckdatabasews")
    assert isinstance(map_api_error(exc), AccountPreconditionError)


def test_indexing_is_indexing():
    exc = PyiCloudServiceNotActivatedException(
        "iCloud Photo Library not finished indexing. Please try again in a few minutes."
    )
    assert isinstance(map_api_error(exc), LibraryIndexingError)


def test_auth_required_code_is_session_expired():
    exc = PyiCloudAPIResponseException("Authentication required for Account.", 421)
    mapped = map_api_error(exc)
    assert isinstance(mapped, SessionExpiredError)
    assert "login" in str(mapped)


def test_2fa_required_is_session_expired():
    exc = PyiCloud2FARequiredException("t@e.com", None)
    assert isinstance(map_api_error(exc), SessionExpiredError)


def test_503_is_transient():
    exc = PyiCloudAPIResponseException("Service Temporarily Unavailable", 503)
    mapped = map_api_error(exc)
    assert isinstance(mapped, ServiceUnavailableError)
    assert isinstance(mapped, TransientError)


def test_access_denied_is_precondition():
    exc = PyiCloudAPIResponseException("ACCESS_DENIED", "ACCESS_DENIED")
    assert isinstance(map_api_error(exc), AccountPreconditionError)


def test_failed_login_is_authentication_error():
    exc = PyiCloudFailedLoginException("Invalid email/password combination.")
    mapped = map_api_error(exc)
    assert isinstance(mapped, AuthenticationError)
    assert not isinstance(mapped, SessionExpiredError)


def test_unknown_api_error_is_generic():
    exc = PyiCloudAPIResponseException("weird", 400)
    mapped = map_api_error(exc)
    assert type(mapped) is ICloudSyncError
