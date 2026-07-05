"""Authentication / session management (C2).

Owns the *policy* of logging in — reusing a persisted session, driving the
2FA/2SA flow, trusting the device, and detecting account preconditions — while
delegating every actual ``pyicloud`` call to :mod:`icloud_photo_sync.icloud_client`.
Interactive prompts are injected as callables so this module stays IO-free and
testable.
"""

from __future__ import annotations

from typing import Callable

from . import icloud_client as ic
from .config import AppConfig
from .errors import (
    AuthenticationError,
    ICloudSyncError,
    SessionExpiredError,
    TwoFactorRequiredError,
)
from .logutil import get_logger
from .models import AssetRef  # noqa: F401  (re-exported convenience)

logger = get_logger(__name__)

CodeProvider = Callable[[str], str]


class SessionManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    # -- non-interactive resume (used by `sync` / `status`) -------------------

    def resume(self) -> tuple[object, ic.ICloudClient]:
        """Reuse the persisted session. Raise SessionExpiredError otherwise.

        Every authentication-shaped failure here (no stored password, expired
        cookies, rejected token) means the same thing to the user: run `login`.
        Mapping them all to SessionExpiredError keeps the CLI's exit-code and
        guidance contract intact.
        """
        password = self._stored_password()
        try:
            service = ic.create_service(
                self.config.apple_id, password, self.config.cookie_dir
            )
        except SessionExpiredError:
            raise
        except AuthenticationError as exc:
            raise SessionExpiredError(
                f"No valid iCloud session ({exc}).\nRun:  icloud-photo-sync login"
            ) from exc
        if ic.requires_2fa(service) or ic.requires_2sa(service):
            raise SessionExpiredError(
                "iCloud session expired or two-factor authentication is required.\n"
                "Run:  icloud-photo-sync login"
            )
        logger.debug("Reused persisted session for %s", self.config.apple_id)
        return service, ic.ICloudClient(service, self.config)

    # -- interactive login (used by `login`) ----------------------------------

    def login(
        self,
        password: str,
        code_provider: CodeProvider,
        *,
        trust: bool = True,
    ) -> tuple[object, ic.ICloudClient]:
        service = ic.create_service(self.config.apple_id, password, self.config.cookie_dir)

        if ic.requires_2fa(service):
            self._do_2fa(service, code_provider, trust=trust)
        elif ic.requires_2sa(service):
            self._do_2sa(service, code_provider, trust=trust)
        else:
            logger.info("Already authenticated; no 2FA needed.")

        return service, ic.ICloudClient(service, self.config)

    def verify_access(self, client: ic.ICloudClient) -> int | None:
        """Cheap authenticated call to surface preconditions / indexing early."""
        return client.count()

    # -- internals ------------------------------------------------------------

    def _stored_password(self) -> str | None:
        from .config import get_password

        return get_password(self.config.apple_id)

    def _do_2fa(self, service, code_provider: CodeProvider, *, trust: bool) -> None:
        logger.info("Two-factor authentication required.")
        code = (code_provider("Enter the 6-digit code Apple sent to your devices: ") or "").strip()
        if not code:
            raise TwoFactorRequiredError("No 2FA code provided.")
        if not ic.validate_2fa_code(service, code):
            raise AuthenticationError("Invalid 2FA code.")
        logger.info("2FA code accepted.")
        if trust and not ic.is_trusted(service):
            if ic.trust_session(service):
                logger.info("Session trusted (no 2FA needed for ~60 days).")
            else:
                logger.warning("Could not persist a trust token; 2FA may be needed again.")

    def _do_2sa(self, service, code_provider: CodeProvider, *, trust: bool) -> None:
        logger.info("Two-step authentication required (older account).")
        devices = ic.trusted_devices(service)
        if not devices:
            raise AuthenticationError("No trusted devices available for two-step auth.")
        device = devices[0]
        label = device.get("phoneNumber") or device.get("deviceType") or "trusted device"
        if not ic.send_verification_code(service, device):
            raise AuthenticationError("Failed to send verification code.")
        code = (code_provider(f"Enter the code sent to {label}: ") or "").strip()
        if not code or not ic.validate_verification_code(service, device, code):
            raise AuthenticationError("Invalid verification code.")
        logger.info("Verification code accepted.")
        if trust and not ic.is_trusted(service):
            ic.trust_session(service)
