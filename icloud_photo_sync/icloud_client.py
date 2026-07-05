"""iCloud adapter (C3) — the ONLY module that imports ``pyicloud``.

Everything else depends on this thin interface so the engine can be swapped
(e.g. for the ``icloudpd`` fallback) without touching the rest of the program.

Verified against pyicloud 2.6.5 (CloudKit backend). Notable facts this code
relies on, established by reading the installed source:

* ``PyiCloudSession.request`` (and therefore ``session.get``) normalizes every
  non-2xx response — and every connection error — into
  ``PyiCloudAPIResponseException`` via ``raise_for_status``. Raw status codes
  are only observable through ``session.request_raw``, which skips the
  normalization but still persists cookies. Asset downloads therefore go
  through ``request_raw`` so byte-range / signed-URL-expiry handling can see
  the actual 200/206/403/416/503.
* ``api.photos.all`` is sorted newest-first **by capture date**
  (``CPLAssetAndMasterByAssetDate…``), NOT by the date an asset was added to
  iCloud. Incremental early-stop therefore uses a separate album built on
  ``ListTypeEnum.ADDED`` (``CPLAssetAndMasterByAddedDate``, descending), which
  pyicloud supports natively via ``_iter_added_desc_photos``.
* ``PhotoAsset.download()`` reads the entire body into memory in this version,
  so it is **not** used; we stream ``request_raw`` against
  ``asset.download_url('original')`` with a ``Range`` header instead.
* ``asset.created`` == ``asset.asset_date`` (capture time, UTC-aware). Missing
  dates come back as the Unix epoch sentinel, which we treat as ``None``.
* ``asset._refresh_from_library()`` re-fetches the asset's records (fresh signed
  URLs) but does not clear the cached ``_resources``; we reset that ourselves.
* ``PyiCloudServiceNotActivatedException`` is raised both for a library that is
  still indexing AND for a missing ``ckdatabasews`` webservice ("Webservice not
  available"), which is the Advanced-Data-Protection / web-access-off symptom —
  the two must be told apart by message.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from threading import Event
from typing import Iterator

import requests
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloud2FARequiredException,
    PyiCloud2SARequiredException,
    PyiCloudAcceptTermsException,
    PyiCloudAPIResponseException,
    PyiCloudAuthRequiredException,
    PyiCloudException,
    PyiCloudFailedLoginException,
    PyiCloudServiceNotActivatedException,
    PyiCloudServiceUnavailable,
)
from pyicloud.services.photos import (
    DirectionEnum,
    ListTypeEnum,
    ObjectTypeEnum,
    SmartAlbumEnum,
    SmartPhotoAlbum,
)
from pyicloud.utils import delete_password_in_keyring

from .config import AppConfig
from .errors import (
    AccountPreconditionError,
    AcceptTermsError,
    AuthenticationError,
    DownloadError,
    ICloudSyncError,
    LibraryIndexingError,
    OperationCancelled,
    ServiceUnavailableError,
    SessionExpiredError,
    TransientError,
)
from .logutil import get_logger
from .models import AssetRef

logger = get_logger(__name__)

PRECONDITION_REMEDIATION = (
    "iCloud denied web/CloudKit access. This tool needs the account configured "
    "so the iCloud web services are reachable:\n"
    "  1. Turn ON  'Access iCloud Data on the Web'  "
    "(iPhone/iPad: Settings → [your name] → iCloud → Access iCloud Data on the Web).\n"
    "  2. Turn OFF 'Advanced Data Protection' (ADP). ADP blocks web access to "
    "iCloud data and there is no workaround.\n"
    "  3. The account must have a trusted device or SMS for 2FA — a hardware "
    "security key as the only factor is not supported."
)

SESSION_EXPIRED_GUIDANCE = (
    "iCloud session expired or re-authentication is required.\n"
    "Run:  icloud-photo-sync login"
)

# Apple status codes pyicloud surfaces for an expired/re-auth-required session.
_AUTH_REQUIRED_CODES = {421, 450}
_TRANSIENT_CODES = {429, 500, 502, 503, 504}

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-")


def _exc_text(exc: Exception) -> str:
    return f"{getattr(exc, 'reason', '')} {getattr(exc, 'code', '')} {exc}".lower()


def _exc_code(exc: Exception) -> int | None:
    try:
        return int(getattr(exc, "code", None))
    except (TypeError, ValueError):
        return None


def map_api_error(exc: Exception) -> ICloudSyncError:
    """Translate any pyicloud exception into one of ours.

    The classification drives real behaviour downstream: ``TransientError`` is
    retried, ``SessionExpiredError`` stops with "run login" guidance,
    ``LibraryIndexingError`` is waited out, ``AccountPreconditionError`` prints
    remediation. Everything unknown becomes a plain ``ICloudSyncError``.
    """
    if isinstance(exc, PyiCloudServiceNotActivatedException):
        # Same exception type for "still indexing" and "webservice missing"
        # (the ADP / web-access-off symptom); only the message differs.
        if "not available" in _exc_text(exc):
            return AccountPreconditionError(PRECONDITION_REMEDIATION)
        return LibraryIndexingError(str(exc))
    if isinstance(exc, PyiCloudAcceptTermsException):
        return AcceptTermsError(
            "Apple requires accepting updated iCloud terms & conditions. Sign in "
            "at https://www.icloud.com to accept them, then re-run."
        )
    if isinstance(
        exc,
        (
            PyiCloud2FARequiredException,
            PyiCloud2SARequiredException,
            PyiCloudAuthRequiredException,
        ),
    ):
        return SessionExpiredError(SESSION_EXPIRED_GUIDANCE)
    if isinstance(exc, PyiCloudFailedLoginException):
        return AuthenticationError(f"Login failed: {exc}")
    if isinstance(exc, PyiCloudServiceUnavailable):
        return ServiceUnavailableError(str(exc))
    if isinstance(exc, PyiCloudAPIResponseException):
        text = _exc_text(exc)
        code = _exc_code(exc)
        if "access_denied" in text or "access denied" in text:
            return AccountPreconditionError(PRECONDITION_REMEDIATION)
        if code in _AUTH_REQUIRED_CODES or "authentication required" in text:
            return SessionExpiredError(SESSION_EXPIRED_GUIDANCE)
        if code in _TRANSIENT_CODES or "temporarily unavailable" in text:
            return ServiceUnavailableError(f"iCloud temporarily unavailable: {exc}")
        return ICloudSyncError(f"iCloud API error: {exc}")
    return ICloudSyncError(str(exc))


def _dt_or_none(dt: datetime | None) -> datetime | None:
    """pyicloud returns the Unix epoch (exactly 0) for missing dates.

    Only that sentinel is treated as unknown — genuinely pre-1970 capture
    dates (negative timestamps; scanned photos) are kept.
    """
    if dt is None:
        return None
    try:
        if int(dt.timestamp()) == 0:
            return None
    except (OverflowError, OSError, ValueError):
        return None
    return dt


# --- Service construction & auth primitives (used by auth.py) -----------------


def create_service(
    apple_id: str,
    password: str | None,
    cookie_dir,
    *,
    accept_terms: bool = False,
) -> PyiCloudService:
    """Construct (and authenticate) a PyiCloudService, mapping errors."""
    try:
        return PyiCloudService(
            apple_id,
            password,
            cookie_directory=str(cookie_dir),
            accept_terms=accept_terms,
        )
    except PyiCloudException as exc:
        raise map_api_error(exc) from exc


def requires_2fa(service: PyiCloudService) -> bool:
    return bool(service.requires_2fa)


def requires_2sa(service: PyiCloudService) -> bool:
    return bool(service.requires_2sa)


def request_2fa_code(service: PyiCloudService) -> bool:
    try:
        return bool(service.request_2fa_code())
    except Exception:  # noqa: BLE001 - resend is best-effort
        return False


def validate_2fa_code(service: PyiCloudService, code: str) -> bool:
    return bool(service.validate_2fa_code(code))


def is_trusted(service: PyiCloudService) -> bool:
    return bool(service.is_trusted_session)


def trust_session(service: PyiCloudService) -> bool:
    try:
        return bool(service.trust_session())
    except Exception:  # noqa: BLE001
        return False


def trusted_devices(service: PyiCloudService) -> list[dict]:
    try:
        return list(service.trusted_devices)
    except Exception:  # noqa: BLE001
        return []


def send_verification_code(service: PyiCloudService, device: dict) -> bool:
    return bool(service.send_verification_code(device))


def validate_verification_code(service: PyiCloudService, device: dict, code: str) -> bool:
    return bool(service.validate_verification_code(device, code))


def account_name(service: PyiCloudService) -> str:
    return getattr(service, "account_name", "") or ""


def clear_engine_credentials(apple_id: str) -> bool:
    """Clear pyicloud's OWN keychain entry (service ``pyicloud://icloud-password``).

    pyicloud silently falls back to that entry when no password is given, so
    ``--reset-keyring`` must clear it too or a stale password stored by another
    pyicloud-based tool keeps being replayed against Apple.
    """
    try:
        delete_password_in_keyring(apple_id)
        return True
    except Exception:  # noqa: BLE001 - absent entry / locked keychain
        return False


# --- The client --------------------------------------------------------------


class ICloudClient:
    """Enumeration + streaming download over an authenticated session."""

    def __init__(
        self,
        service: PyiCloudService,
        config: AppConfig,
        cancel_event: Event | None = None,
    ) -> None:
        self._service = service
        self._config = config
        self._cancel = cancel_event or Event()

    def set_cancel_event(self, event: Event) -> None:
        self._cancel = event

    def _wait(self, seconds: float) -> None:
        """Cancellation-aware sleep; raises OperationCancelled when stopped."""
        if self._cancel.wait(seconds):
            raise OperationCancelled()

    # -- enumeration ----------------------------------------------------------

    def _all_album(self):
        """Return the 'All Photos' album, waiting out library indexing.

        Accessing ``.photos.all`` is lazy — errors only surface when a query
        runs. We force that here with ``len()`` so the indexing wait and
        precondition detection happen up front rather than mid-enumeration.
        """
        deadline = time.monotonic() + self._config.indexing_max_wait
        while True:
            try:
                album = self._service.photos.all
                len(album)  # trigger activation / surface errors
                return album
            except PyiCloudException as exc:
                mapped = map_api_error(exc)
                if not isinstance(mapped, LibraryIndexingError):
                    raise mapped from exc
                if time.monotonic() >= deadline:
                    raise LibraryIndexingError(
                        "iCloud is still indexing the photo library; gave up "
                        f"after {self._config.indexing_max_wait:.0f}s. Try again later."
                    ) from exc
                logger.info(
                    "iCloud library still indexing; retrying in %.0fs…",
                    self._config.indexing_retry,
                )
                self._wait(self._config.indexing_retry)
            except requests.exceptions.RequestException as exc:
                raise TransientError(f"connection error reaching iCloud: {exc}") from exc

    def count(self) -> int | None:
        """Best-effort total asset count for progress display."""
        try:
            return len(self._all_album())
        except ICloudSyncError:
            raise
        except PyiCloudException as exc:
            raise map_api_error(exc) from exc
        except Exception:  # noqa: BLE001 - count is optional
            return None

    def iter_all_assets(self) -> Iterator[AssetRef]:
        """Yield every asset in 'All Photos', newest-capture-first."""
        return self._iter_album(self._all_album())

    def iter_added_desc(self) -> Iterator[AssetRef] | None:
        """Yield assets newest-ADDED-first, or None if unavailable.

        This is the ordering the incremental early-stop needs: brand-new
        additions come first even when their capture date is old (imports,
        AirDrops, scans). Returns None when the added-date album cannot be
        built (engine change); callers must then fall back to a full scan
        WITHOUT early-stop, or new-but-old photos would be missed silently.
        """
        self._all_album()  # surface indexing/precondition errors first
        try:
            photos = self._service.photos
            library = getattr(photos, "_root_library", None)
            if library is None:
                logger.warning("pyicloud exposes no _root_library; added-date listing unavailable.")
                return None
            album = SmartPhotoAlbum(
                library=library,
                name=SmartAlbumEnum.ALL_PHOTOS,
                obj_type=ObjectTypeEnum.ALL,
                list_type=ListTypeEnum.ADDED,
                direction=DirectionEnum.DESCENDING,
                client=getattr(library, "_client", None),
                zone_id=library.zone_id,
            )
        except Exception as exc:  # noqa: BLE001 - engine drift → honest fallback
            logger.warning("Could not build added-date album (%s); falling back.", exc)
            return None
        return self._iter_album(album)

    def _iter_album(self, album) -> Iterator[AssetRef]:
        try:
            for raw in album:
                yield self._build_asset_ref(raw)
        except PyiCloudException as exc:
            raise map_api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            raise TransientError(f"Enumeration interrupted: {exc}") from exc

    def _build_asset_ref(self, raw) -> AssetRef:
        # PhotoAsset.size is a single record-field read; .versions would
        # materialize every rendition, so it is only a fallback.
        try:
            size = raw.size
        except Exception:  # noqa: BLE001
            size = None
        if size is None:
            try:
                original = raw.versions.get("original")
                if original:
                    size = original.get("size")
            except Exception:  # noqa: BLE001
                size = None
        return AssetRef(
            id=raw.id,
            filename=raw.filename,
            capture_dt=_dt_or_none(getattr(raw, "created", None)),
            added_dt=_dt_or_none(getattr(raw, "added_date", None)),
            size=size,
            raw=raw,
        )

    # -- download URL handling ------------------------------------------------

    def refresh_asset(self, asset: AssetRef) -> bool:
        """Re-fetch the asset's records so ``download_url`` yields a fresh URL."""
        raw = asset.raw
        ok = False
        try:
            ok = bool(raw._refresh_from_library())
        except Exception:  # noqa: BLE001
            ok = False
        # _refresh_from_library updates the records but leaves the cached
        # resources (and their now-stale signed URLs) in place — reset them.
        try:
            raw._resources = None
        except Exception:  # noqa: BLE001
            pass
        return ok

    def _original_url(self, asset: AssetRef, *, allow_refresh: bool = True) -> str:
        try:
            url = asset.raw.download_url("original")
        except Exception as exc:  # noqa: BLE001
            url = None
            if not allow_refresh:
                raise DownloadError(f"cannot resolve URL for {asset.filename}: {exc}")
        if not url and allow_refresh and self.refresh_asset(asset):
            url = asset.raw.download_url("original")
        if not url:
            raise DownloadError(f"no 'original' download URL for {asset.filename}")
        return url

    def _get_raw(self, url: str, headers: dict | None):
        """Streaming GET that sees real status codes.

        ``session.get`` normalizes every non-2xx into
        ``PyiCloudAPIResponseException`` (see module docstring), which would
        make all status handling below unreachable — so use ``request_raw``
        when the session provides it. ``request_raw`` still wraps connection
        errors in ``PyiCloudAPIResponseException``; both flavours map to
        ``TransientError`` here because a CDN GET has no API-error meaning.
        """
        session = self._service.session
        kwargs = dict(headers=headers or None, stream=True, timeout=self._config.timeout)
        try:
            if hasattr(session, "request_raw"):
                return session.request_raw("GET", url, **kwargs)
            return session.get(url, **kwargs)
        except (requests.exceptions.RequestException, PyiCloudAPIResponseException) as exc:
            raise TransientError(f"connection error: {exc}") from exc

    @staticmethod
    def _content_range_start(resp) -> int | None:
        m = _CONTENT_RANGE_RE.match(resp.headers.get("Content-Range", ""))
        return int(m.group(1)) if m else None

    @staticmethod
    def _total_size(resp, byte_offset: int, status: int) -> int | None:
        if status == 206:
            cr = resp.headers.get("Content-Range", "")
            if "/" in cr:
                tail = cr.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    return int(tail)
        cl = resp.headers.get("Content-Length")
        if cl and cl.isdigit():
            return int(cl) + (byte_offset if status == 206 else 0)
        return None

    def open_stream(self, asset: AssetRef, byte_offset: int = 0):
        """Open a streaming GET for the original.

        Returns ``(response, range_ok, total_size)``. ``range_ok`` is True only
        when a non-zero offset was honoured (HTTP 206 whose Content-Range start
        matches the requested offset); when False the response body starts from
        byte 0 and the caller must restart the file.
        """
        url = self._original_url(asset)
        headers = {}
        if byte_offset > 0:
            headers["Range"] = f"bytes={byte_offset}-"

        resp = self._get_raw(url, headers)
        status = resp.status_code

        # Expired/forbidden signed URL → refresh once and retry.
        if status in (401, 403, 410):
            resp.close()
            if self.refresh_asset(asset):
                url = self._original_url(asset, allow_refresh=False)
                resp = self._get_raw(url, headers)
                status = resp.status_code

        if status == 503:
            resp.close()
            raise ServiceUnavailableError("HTTP 503 from iCloud (temporarily unavailable)")

        if status == 416 and byte_offset > 0:
            # Offset past EOF (e.g. a bogus/oversized .part) — restart from 0.
            resp.close()
            return self.open_stream(asset, byte_offset=0)

        if status not in (200, 206):
            resp.close()
            if status in _TRANSIENT_CODES:
                raise TransientError(f"HTTP {status} from iCloud")
            raise DownloadError(f"unexpected HTTP {status} downloading {asset.filename}")

        if byte_offset > 0 and status == 206:
            start = self._content_range_start(resp)
            if start != byte_offset:
                # Server honoured *a* range, but not ours (or sent no/garbled
                # Content-Range). Appending this body would corrupt the file —
                # restart from zero instead.
                resp.close()
                return self.open_stream(asset, byte_offset=0)

        range_ok = byte_offset > 0 and status == 206
        total = self._total_size(resp, byte_offset, status)
        return resp, range_ok, total
