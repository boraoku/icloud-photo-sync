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
* **``api.photos`` is a property that re-runs the whole PCS (Protected Cloud
  Storage) handshake on every single access**, above its own cache — so reading
  it in a loop is not free, it is one-to-two POSTs to Apple's setup endpoint
  each time. Do enough of them and Apple revokes the device's PCS consent
  mid-run and refuses to re-grant it ("Unable to request PCS access!"). Every
  access in this module therefore goes through :meth:`ICloudClient._photos_raw`,
  which resolves it once per session.

This module is also the **only** one that mutates anything in iCloud (see
:meth:`ICloudClient.delete_assets`). Two more verified 2.6.5 facts govern that:

* ``PhotoAsset.delete()`` issues the modify and then ``return True``
  unconditionally, discarding the response — a per-record CloudKit failure
  (a stale ``recordChangeTag``, say) is indistinguishable from success. We
  therefore never call it, and build the ``records/modify`` operation here so
  the per-record outcome can be read, and re-checked afterwards.
* ``album.get(id)`` runs a targeted ``recordName EQUALS`` query but falls back
  to enumerating the whole album on a miss (``_get_photo``), so one stale id
  would walk 25k assets. Lookups here go through ``records/lookup`` instead,
  which is batched and cannot degrade that way.

Deletion sets ``isDeleted = 1``: iCloud's "Recently Deleted", recoverable for
~30 days. The permanent state is ``isExpunged``, which this tool never writes.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Iterator, Sequence

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
from pyicloud.common.cloudkit.models import (
    CKErrorItem,
    CKModifyOperation,
    CKRecord,
    CKWriteRecord,
    CKZoneID,
    CKZoneIDReq,
)
from pyicloud.services.photos import (
    DirectionEnum,
    ListTypeEnum,
    ObjectTypeEnum,
    SmartAlbumEnum,
    SmartPhotoAlbum,
)
from pyicloud.services.photos_cloudkit.mappers import (
    decode_encrypted_text,
    record_change_tag,
    record_field_value,
    record_name,
    record_zone,
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

# --- deletion ----------------------------------------------------------------

_PRIMARY_ZONE = {"zoneName": "PrimarySync", "zoneType": "REGULAR_CUSTOM_ZONE"}
_SHARED_ZONE_PREFIX = "SharedSync-"   # a shared library: someone else's photos too
_LOOKUP_CHUNK = 100                   # record names per records/lookup

# Only what the corroboration checks need — asking for resource blobs would drag
# megabytes across the wire for a few hundred records.
_ASSET_KEYS = ["recordName", "recordType", "recordChangeTag", "masterRef",
               "assetDate", "isDeleted", "isExpunged", "zoneID"]
# resJPEGThumbRes is a URL token, not a blob — a few hundred bytes per record —
# so asking for it costs nothing and lets a retrospective run show the user what
# they are about to delete, even though the local file is long gone.
_MASTER_KEYS = ["recordName", "recordType", "filenameEnc", "resOriginalRes",
                "resJPEGThumbRes"]


@dataclass(frozen=True)
class RemoteAsset:
    """What iCloud currently says about one asset.

    Carries the ``change_tag`` it was read with: a modify must quote a tag only
    seconds old, because a stale one is what CloudKit rejects.
    """

    asset_id: str
    record_type: str
    change_tag: str | None
    zone: dict
    filename: str | None
    size: int | None
    capture_dt: datetime | None
    is_deleted: bool
    is_expunged: bool
    thumb_url: str | None = None

    @property
    def zone_name(self) -> str:
        return str(self.zone.get("zoneName") or "")

    @property
    def in_shared_library(self) -> bool:
        """Shared-library assets belong to other people too — never deleted."""
        return self.zone_name.startswith(_SHARED_ZONE_PREFIX)


@dataclass(frozen=True)
class DeleteResult:
    asset_id: str
    ok: bool
    error: str | None = None
    already_deleted: bool = False


def _truthy(value) -> bool:
    """CloudKit spells booleans as INT64 0/1."""
    return bool(value) and value not in ("0", 0)


def _master_ref(record: CKRecord) -> str | None:
    """The CPLMaster record name an asset points at."""
    value = record_field_value(record, "masterRef")
    if isinstance(value, dict):
        return value.get("recordName")
    return getattr(value, "recordName", None)


def _original_size(master: CKRecord | None) -> int | None:
    if master is None:
        return None
    token = record_field_value(master, "resOriginalRes")
    if isinstance(token, dict):
        return token.get("size")
    return getattr(token, "size", None)


def _thumb_url(master: CKRecord | None) -> str | None:
    """The CDN URL of iCloud's own JPEG thumbnail, if the master carries one."""
    if master is None:
        return None
    token = record_field_value(master, "resJPEGThumbRes")
    if isinstance(token, dict):
        return token.get("downloadURL")
    return getattr(token, "downloadURL", None)


def _asset_date(record: CKRecord) -> datetime | None:
    value = record_field_value(record, "assetDate")
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return _dt_or_none(datetime.fromtimestamp(value / 1000.0, timezone.utc))
    return None


def _remote_asset(record: CKRecord, master: CKRecord | None) -> RemoteAsset:
    return RemoteAsset(
        asset_id=record_name(record),
        record_type=record.recordType,
        change_tag=record_change_tag(record),
        zone=record_zone(record) or dict(_PRIMARY_ZONE),
        filename=decode_encrypted_text(master, "filenameEnc") if master is not None else None,
        size=_original_size(master),
        capture_dt=_asset_date(record),
        is_deleted=_truthy(record_field_value(record, "isDeleted")),
        is_expunged=_truthy(record_field_value(record, "isExpunged")),
        thumb_url=_thumb_url(master),
    )


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


def account_dsid(service: PyiCloudService) -> str:
    """Apple's numeric id for the signed-in account.

    The ground truth for *whose library am I about to mutate*: cookies are kept
    per Apple ID, but the dsid is what the server itself resolved the session to.
    """
    data = getattr(service, "data", None) or {}
    dsid = (data.get("dsInfo") or {}).get("dsid")
    if not dsid:
        dsid = (getattr(service, "params", None) or {}).get("dsid")
    return str(dsid or "")


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
        # Resolved lazily and reused — see _photos_raw. Never touch directly.
        self._photos_service = None

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
                album = self._photos_raw().all
                len(album)  # trigger activation / surface errors
                return album
            except PyiCloudException as exc:
                self._forget_photos()
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
            photos = self._photos_raw()
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

    # -- deletion (the only mutation this program performs) --------------------

    def supports_delete(self) -> bool:
        """True when this pyicloud build exposes the typed CloudKit client.

        Probed before the user is allowed to trash anything, so engine drift is
        an honest up-front refusal rather than a surprise after the work is
        done. There is deliberately no fallback to ``PhotoAsset.delete()``:
        that path cannot report whether it worked.
        """
        # A session or account problem is NOT "this build cannot delete" — saying
        # so would send the user off fixing the wrong thing, so _photos() raises
        # a mapped error rather than being swallowed into a False here.
        client = getattr(self._photos(), "_private_client", None)
        return all(callable(getattr(client, name, None)) for name in ("lookup", "modify"))

    def _photos_raw(self):
        """``service.photos``, resolved **once** and reused.

        This cache is not an optimisation, it is a correctness fix. pyicloud's
        ``photos`` property runs a full PCS (Protected Cloud Storage) handshake
        on *every* attribute access, above its own cache::

            @property
            def photos(self):
                self._request_pcs_for_service("photos")   # every time
                if not self._photos:                       # cache is below it

        Each handshake is at least two POSTs to Apple's setup endpoint. Touching
        the property once per batch meant ~24 handshakes for a thumbnail fetch
        and ~360 for a full delete run; partway through, Apple revokes the
        device's PCS consent and refuses to re-grant it ("Unable to request PCS
        access!"). Resolving it once makes that one handshake per session.

        Raises pyicloud's own exceptions, so callers that need to inspect them
        (the indexing-wait loop) still can. See :meth:`_photos` for the mapped
        flavour, and :meth:`_forget_photos` for invalidation.
        """
        if self._photos_service is None:
            self._photos_service = self._service.photos
        return self._photos_service

    def _forget_photos(self) -> None:
        """Drop the cached handle so the next call re-authenticates.

        Called whenever a request fails: the cheap handshake is worth repeating
        when something has actually gone wrong, and a session that expired
        mid-run must not be papered over by a stale handle.
        """
        self._photos_service = None

    def _photos(self):
        """:meth:`_photos_raw` with pyicloud's errors translated to ours.

        Reaching the underlying attribute fails for session reasons far more
        often than for engine-capability reasons — the caller's exit code
        depends on telling those apart.
        """
        try:
            return self._photos_raw()
        except PyiCloudException as exc:
            self._forget_photos()
            raise map_api_error(exc) from exc

    def _private_client(self):
        client = getattr(self._photos(), "_private_client", None)
        if client is None:
            raise ICloudSyncError(
                "This pyicloud build exposes no CloudKit client; deletion is unavailable."
            )
        return client

    def _zone(self) -> tuple[dict, CKZoneIDReq]:
        """The library's private zone, as both a dict (records) and a request."""
        library = getattr(self._photos(), "_root_library", None)
        zone = dict(getattr(library, "zone_id", None) or _PRIMARY_ZONE)
        return zone, CKZoneIDReq(
            zoneName=zone["zoneName"],
            ownerRecordName=zone.get("ownerRecordName"),
            zoneType=zone.get("zoneType"),
        )

    def lookup_assets(
        self, asset_ids: Sequence[str], *, chunk: int = _LOOKUP_CHUNK
    ) -> tuple[dict[str, RemoteAsset], list[str]]:
        """Fetch what iCloud currently says about each asset id.

        Returns ``({asset_id: RemoteAsset}, missing_ids)``. Two batched round
        trips per chunk: the CPLAsset records, then the CPLMaster records they
        point at — filename and original size live on the *master*, so an asset
        lookup alone could not corroborate either.
        """
        client = self._private_client()
        _, zone_req = self._zone()
        found: dict[str, RemoteAsset] = {}
        missing: list[str] = []

        ids = list(asset_ids)
        for start in range(0, len(ids), chunk):
            batch = ids[start:start + chunk]
            assets, absent = self._lookup_records(client, zone_req, batch, _ASSET_KEYS)
            missing.extend(absent)
            masters, _ = self._lookup_records(
                client, zone_req,
                [m for m in (_master_ref(r) for r in assets.values()) if m],
                _MASTER_KEYS,
            )
            for asset_id, record in assets.items():
                found[asset_id] = _remote_asset(record, masters.get(_master_ref(record) or ""))
        return found, missing

    def thumbnail_bytes(self, url: str, *, max_bytes: int = 2 * 1024 * 1024) -> bytes | None:
        """Fetch one iCloud thumbnail. Returns None rather than raising.

        A thumbnail that will not load is a cosmetic problem in a review page,
        never a reason to abandon a run — the item simply shows without one.
        ``max_bytes`` is a sanity bound: these are tens of KB, so anything
        larger means the URL is not what we think it is.
        """
        try:
            resp = self._get_raw(url, None)
            if getattr(resp, "status_code", 0) != 200:
                return None
            data = resp.raw.read(max_bytes + 1) if hasattr(resp, "raw") else resp.content
        except (TransientError, ICloudSyncError, OSError) as exc:
            logger.debug("thumbnail fetch failed for %s: %s", url[:80], exc)
            return None
        if not data or len(data) > max_bytes:
            return None
        return data

    # -- upload: not possible any more -----------------------------------------
    #
    # There is deliberately no upload method here. Apple has closed both routes
    # to web-authenticated third-party clients, verified against a live account:
    #
    #   uploadimagews /upload   HTTP 410 "Gone" for every request shape (raw
    #                           body under three content types, multipart), every
    #                           media type, down to a 247-byte JPEG. The 413s a
    #                           caller sees on larger files are a front proxy
    #                           bouncing the body before the app server answers.
    #   CloudKit assets/upload  A static policy refusal: QUOTA_EXCEEDED with a
    #                           retryAfter that never decreases, on an account
    #                           with 21.8 GiB of 200 GiB free. Payload variants
    #                           and current icloud.com build numbers change
    #                           nothing.
    #
    # The same session's records/lookup and records/modify keep working, which is
    # what the deletion path runs on — so this is upload-specific, not an auth or
    # session problem, and no amount of header-fiddling will fix it. pyicloud's
    # own ``upload_file`` targets the same dead endpoint.
    #
    # ``video-optimise`` therefore hands the files to the user to upload through
    # Apple's own clients and finds them again with
    # :func:`icloud_photo_sync.optimise.reconcile`, which is what
    # :meth:`verify_present` below exists to serve.

    def verify_present(self, asset_id: str) -> RemoteAsset | None:
        """Read the asset back. ``None`` unless it exists and is not deleted.

        This is what turns "Apple accepted the POST" into "the replacement is in
        the library", and no original may be deleted without it — the same rule
        :meth:`verify_deleted` enforces from the other side.
        """
        found, _ = self.lookup_assets([asset_id])
        asset = found.get(asset_id)
        if asset is None or asset.is_deleted or asset.is_expunged:
            return None
        return asset

    def _lookup_records(
        self, client, zone_req: CKZoneIDReq, record_names: Sequence[str], keys: list[str]
    ) -> tuple[dict[str, CKRecord], list[str]]:
        """One ``records/lookup``. Tombstones and NOT_FOUND count as missing."""
        if not record_names:
            return {}, []
        try:
            resp = client.lookup(
                record_names=list(record_names), zone_id=zone_req, desired_keys=keys
            )
        except PyiCloudException as exc:
            self._forget_photos()
            raise map_api_error(exc) from exc
        records: dict[str, CKRecord] = {}
        missing: list[str] = []
        for entry in getattr(resp, "records", []) or []:
            if isinstance(entry, CKRecord):
                records[entry.recordName] = entry
            elif isinstance(entry, CKErrorItem):
                if entry.recordName:
                    missing.append(entry.recordName)
                    logger.debug("lookup: %s -> %s", entry.recordName, entry.serverErrorCode)
            else:  # tombstone: the record is gone entirely
                name = getattr(entry, "recordName", None)
                if name:
                    missing.append(name)
        return records, missing

    def delete_assets(self, assets: Sequence[RemoteAsset]) -> list[DeleteResult]:
        """Move ``assets`` to Recently Deleted, reporting each one honestly.

        Callers pass records straight from :meth:`lookup_assets` — the change
        tag must be seconds old, since a stale one is exactly what CloudKit
        rejects. ``atomic=False`` so one bad record cannot veto the batch and
        every outcome is attributable.

        A response saying "fine" is not evidence: the caller must still call
        :meth:`verify_deleted`.
        """
        if not assets:
            return []
        client = self._private_client()
        zone_dict, zone_req = self._zone()

        operations = []
        for asset in assets:
            zone = asset.zone or zone_dict
            operations.append(
                CKModifyOperation(
                    operationType="update",
                    record=CKWriteRecord(
                        recordName=asset.asset_id,
                        recordType=asset.record_type,
                        recordChangeTag=asset.change_tag,
                        fields={"isDeleted": {"type": "INT64", "value": 1}},
                        zoneID=CKZoneID(**zone),
                    ),
                )
            )
        try:
            resp = client.modify(operations=operations, zone_id=zone_req, atomic=False)
        except PyiCloudException as exc:
            self._forget_photos()
            raise map_api_error(exc) from exc

        outcomes: dict[str, DeleteResult] = {}
        for entry in getattr(resp, "records", []) or []:
            if isinstance(entry, CKErrorItem):
                name = entry.recordName or ""
                outcomes[name] = DeleteResult(
                    asset_id=name, ok=False,
                    error=f"{entry.serverErrorCode}: {entry.reason or 'no reason given'}",
                )
            elif isinstance(entry, CKRecord):
                applied = _truthy(record_field_value(entry, "isDeleted"))
                outcomes[entry.recordName] = DeleteResult(
                    asset_id=entry.recordName, ok=applied,
                    error=None if applied else "server did not apply isDeleted",
                )
            else:  # tombstone — gone rather than trashed, but gone
                name = getattr(entry, "recordName", "")
                outcomes[name] = DeleteResult(asset_id=name, ok=True, already_deleted=True)

        return [
            outcomes.get(
                a.asset_id,
                DeleteResult(asset_id=a.asset_id, ok=False,
                             error="no per-record outcome in the response"),
            )
            for a in assets
        ]

    def verify_deleted(self, asset_ids: Sequence[str]) -> dict[str, bool]:
        """Re-read each asset and report whether iCloud really has it deleted.

        This is the only thing that turns "the API accepted it" into "it
        happened". An id that has vanished entirely counts as deleted.
        """
        found, missing = self.lookup_assets(asset_ids)
        verified = {asset_id: True for asset_id in missing}
        for asset_id in asset_ids:
            if asset_id in found:
                verified[asset_id] = found[asset_id].is_deleted or found[asset_id].is_expunged
        return verified
