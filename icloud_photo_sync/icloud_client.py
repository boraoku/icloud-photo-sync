"""iCloud adapter (C3) — the ONLY module that imports ``pyicloud``.

Everything else depends on this thin interface so the engine can be swapped
(e.g. for the ``icloudpd`` fallback) without touching the rest of the program.

Verified against pyicloud 2.6.5 (CloudKit backend). Notable facts this code
relies on, established by reading the installed source:

* ``api.photos.all`` is the "All Photos" smart album, sorted **newest-first**
  (``DirectionEnum.DESCENDING``) — required for the incremental early-stop.
* ``PhotoAsset.download()`` reads the entire body into memory in this version,
  so it is **not** used. Instead we stream ``api.session.get(url, stream=True)``
  against ``asset.download_url('original')`` — byte-for-byte the same auth path
  pyicloud's own ``_CloudKitHTTP.get_stream`` uses, but with a ``Range`` header
  so large videos can resume.
* ``asset.created`` == ``asset.asset_date`` (capture time, UTC-aware). Missing
  dates come back as the Unix epoch sentinel, which we treat as ``None``.
* ``asset._refresh_from_library()`` re-fetches the asset's records (fresh signed
  URLs) but does not clear the cached ``_resources``; we reset that ourselves.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Iterator

import requests
from pyicloud import PyiCloudService
from pyicloud.exceptions import (
    PyiCloudAcceptTermsException,
    PyiCloudAPIResponseException,
    PyiCloudFailedLoginException,
    PyiCloudServiceNotActivatedException,
)

from .config import AppConfig
from .errors import (
    AccountPreconditionError,
    AcceptTermsError,
    AuthenticationError,
    DownloadError,
    ICloudSyncError,
    LibraryIndexingError,
    ServiceUnavailableError,
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


def _looks_like_access_denied(exc: PyiCloudAPIResponseException) -> bool:
    blob = f"{getattr(exc, 'reason', '')} {getattr(exc, 'code', '')}".upper()
    return "ACCESS_DENIED" in blob or "ACCESS DENIED" in blob


def map_api_error(exc: Exception) -> ICloudSyncError:
    """Translate a pyicloud exception into one of ours."""
    if isinstance(exc, PyiCloudServiceNotActivatedException):
        return LibraryIndexingError(str(exc))
    if isinstance(exc, PyiCloudAcceptTermsException):
        return AcceptTermsError(
            "Apple requires accepting updated iCloud terms & conditions. Sign in "
            "at https://www.icloud.com to accept them, then re-run."
        )
    if isinstance(exc, PyiCloudFailedLoginException):
        return AuthenticationError(f"Login failed: {exc}")
    if isinstance(exc, PyiCloudAPIResponseException):
        if _looks_like_access_denied(exc):
            return AccountPreconditionError(PRECONDITION_REMEDIATION)
        return ICloudSyncError(f"iCloud API error: {exc}")
    return ICloudSyncError(str(exc))


def _dt_or_none(dt: datetime | None) -> datetime | None:
    """pyicloud returns the Unix epoch for missing dates; treat that as unknown."""
    if dt is None:
        return None
    try:
        if int(dt.timestamp()) <= 0:
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
    except (PyiCloudFailedLoginException, PyiCloudAPIResponseException,
            PyiCloudAcceptTermsException) as exc:
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


# --- The client --------------------------------------------------------------


class ICloudClient:
    """Enumeration + streaming download over an authenticated session."""

    def __init__(self, service: PyiCloudService, config: AppConfig) -> None:
        self._service = service
        self._config = config

    # -- enumeration ----------------------------------------------------------

    def _all_album(self):
        """Return the 'All Photos' album, waiting out library indexing.

        Accessing ``.photos.all`` is lazy — the not-activated error and any
        ``ACCESS_DENIED`` precondition only surface when a query runs. We force
        that here with ``len()`` so the indexing wait and precondition detection
        happen up front rather than mid-enumeration.
        """
        deadline = time.monotonic() + self._config.indexing_max_wait
        while True:
            try:
                album = self._service.photos.all
                len(album)  # trigger activation / surface ACCESS_DENIED + indexing
                return album
            except PyiCloudServiceNotActivatedException as exc:
                if time.monotonic() >= deadline:
                    raise LibraryIndexingError(
                        "iCloud is still indexing the photo library; gave up "
                        f"after {self._config.indexing_max_wait:.0f}s. Try again later."
                    ) from exc
                logger.info(
                    "iCloud library still indexing; retrying in %.0fs…",
                    self._config.indexing_retry,
                )
                time.sleep(self._config.indexing_retry)
            except PyiCloudAPIResponseException as exc:
                raise map_api_error(exc) from exc
            except requests.exceptions.RequestException as exc:
                raise TransientError(f"connection error reaching iCloud: {exc}") from exc

    def count(self) -> int | None:
        """Best-effort total asset count for progress display."""
        try:
            return len(self._all_album())
        except ICloudSyncError:
            raise
        except Exception:  # noqa: BLE001 - count is optional
            return None

    def iter_all_assets(self) -> Iterator[AssetRef]:
        """Yield every asset in 'All Photos', newest-first."""
        album = self._all_album()
        try:
            for raw in album:
                yield self._build_asset_ref(raw)
        except PyiCloudServiceNotActivatedException as exc:
            raise LibraryIndexingError(str(exc)) from exc
        except PyiCloudAPIResponseException as exc:
            raise map_api_error(exc) from exc
        except requests.exceptions.RequestException as exc:
            raise TransientError(f"Enumeration interrupted: {exc}") from exc

    def _build_asset_ref(self, raw) -> AssetRef:
        size = None
        try:
            original = raw.versions.get("original")
            if original:
                size = original.get("size")
        except Exception:  # noqa: BLE001
            original = None
        if size is None:
            try:
                size = raw.size
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
        when a non-zero offset was honoured (HTTP 206); when False the response
        body starts from byte 0 and the caller must restart the file. The
        response is always positioned to be read from where the caller will
        continue writing (offset if range_ok else 0).
        """
        url = self._original_url(asset)
        headers = {}
        if byte_offset > 0:
            headers["Range"] = f"bytes={byte_offset}-"

        try:
            resp = self._service.session.get(
                url, stream=True, headers=headers, timeout=self._config.timeout
            )
        except requests.exceptions.RequestException as exc:
            raise TransientError(f"connection error: {exc}") from exc

        status = resp.status_code

        # Expired/forbidden signed URL → refresh once and retry.
        if status in (401, 403, 410):
            resp.close()
            if self.refresh_asset(asset):
                url = self._original_url(asset, allow_refresh=False)
                try:
                    resp = self._service.session.get(
                        url, stream=True, headers=headers,
                        timeout=self._config.timeout,
                    )
                except requests.exceptions.RequestException as exc:
                    raise TransientError(f"connection error: {exc}") from exc
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
            if status in (429, 500, 502, 504):
                raise TransientError(f"HTTP {status} from iCloud")
            raise DownloadError(f"unexpected HTTP {status} downloading {asset.filename}")

        range_ok = byte_offset > 0 and status == 206
        total = self._total_size(resp, byte_offset, status)
        return resp, range_ok, total
