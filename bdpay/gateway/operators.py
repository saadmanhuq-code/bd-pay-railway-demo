"""Operator authentication — TOTP 2FA step + sessions (spec/01 §A, §FSM 2).

Flow: the operator presents a pre-2FA password token (validated by the IAM
directory, spec/15) plus a TOTP code. Three consecutive TOTP failures inside
the failure window lock the account for 30 minutes (BB ICT §6.2.6). Success
issues an opaque 8-hour session token; the session enforces the 15-minute
inactivity timeout and records ``totp_verified_at`` for downstream freshness
gates (high-value refunds, spec/01 §E).

At rest: TOTP secrets are AES-256-GCM sealed under the gateway master key;
backup codes are stored as SHA-256 digests; session tokens are stored as
their SHA-256 (``jti``) — the raw token exists only in the issue response.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from bdpay.gateway import totp as totp_mod
from bdpay.gateway.config import GatewaySettings
from bdpay.gateway.credentials import constant_time_equals, decrypt_blob, encrypt_blob, sha256_hex
from bdpay.gateway.ids_ext import make_gateway_id
from bdpay.gateway.principals import Principal
from bdpay.platform.clock import Clock
from bdpay.platform.errors import AuthenticationError

__all__ = [
    "OPERATOR_ROLES",
    "InMemoryOperatorDirectory",
    "InMemoryOperatorStore",
    "OperatorAuthService",
    "OperatorDirectoryPort",
    "OperatorFailureState",
    "OperatorSession",
    "OperatorStore",
    "OperatorTotpEnrollment",
    "TotpEnrollmentResult",
]

#: Closed role catalogue (spec/01 DDL ``operator_role`` enum).
OPERATOR_ROLES: frozenset[str] = frozenset(
    {
        "SUPER_ADMIN",
        "MERCHANT_ADMIN",
        "PAYMENT_OPS",
        "KYC_REVIEWER",
        "CAMLCO",
        "FINANCE",
        "TREASURY",
        "NOTIFICATION_ADMIN",
        "IAM_ADMIN",
        "READ_ONLY",
        "BB_INSPECTOR_READ_ONLY",
    }
)

_SESSION_STATUSES = ("ACTIVE", "TERMINATED", "LOCKED")
_BACKUP_CODE_COUNT = 8
_BACKUP_CODE_DIGITS = 10


@dataclass(frozen=True)
class OperatorTotpEnrollment:
    """One ``operator_totp_enrollments`` row (migration 0081)."""

    operator_id: str
    totp_secret_enc: str
    backup_code_hashes: tuple[str, ...]
    enrolled_at: datetime
    last_used_at: datetime | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class OperatorSession:
    """One ``operator_sessions`` row (migration 0081)."""

    session_id: str
    operator_id: str
    jti: str
    roles: tuple[str, ...]
    ip_hash: str
    user_agent_hash: str
    status: str
    created_at: datetime
    expires_at: datetime
    last_activity_at: datetime
    totp_verified_at: datetime
    terminated_at: datetime | None = None
    termination_reason: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _SESSION_STATUSES:
            raise ValueError(f"status must be one of {_SESSION_STATUSES}")


@dataclass(frozen=True)
class OperatorFailureState:
    """Consecutive TOTP failure tracking (``operator_auth_failures``)."""

    operator_id: str
    failed_attempts: int
    window_started_at: datetime | None
    locked_until: datetime | None


@runtime_checkable
class OperatorStore(Protocol):
    """Storage contract for enrollments, sessions, and failure state."""

    def get_enrollment(self, operator_id: str) -> OperatorTotpEnrollment | None: ...

    def save_enrollment(self, enrollment: OperatorTotpEnrollment) -> None: ...

    def insert_session(self, session: OperatorSession) -> None: ...

    def save_session(self, session: OperatorSession) -> None: ...

    def get_session_by_jti(self, jti: str) -> OperatorSession | None: ...

    def get_failure_state(self, operator_id: str) -> OperatorFailureState | None: ...

    def save_failure_state(self, state: OperatorFailureState) -> None: ...


class InMemoryOperatorStore:
    """Deterministic in-memory operator store for unit tests."""

    def __init__(self) -> None:
        self._enrollments: dict[str, OperatorTotpEnrollment] = {}
        self._sessions: dict[str, OperatorSession] = {}
        self._failures: dict[str, OperatorFailureState] = {}

    def get_enrollment(self, operator_id: str) -> OperatorTotpEnrollment | None:
        return self._enrollments.get(operator_id)

    def save_enrollment(self, enrollment: OperatorTotpEnrollment) -> None:
        self._enrollments[enrollment.operator_id] = enrollment

    def insert_session(self, session: OperatorSession) -> None:
        if session.jti in self._sessions:
            raise ValueError("session jti already exists")
        self._sessions[session.jti] = session

    def save_session(self, session: OperatorSession) -> None:
        if session.jti not in self._sessions:
            raise ValueError("unknown session")
        self._sessions[session.jti] = session

    def get_session_by_jti(self, jti: str) -> OperatorSession | None:
        return self._sessions.get(jti)

    def get_failure_state(self, operator_id: str) -> OperatorFailureState | None:
        return self._failures.get(operator_id)

    def save_failure_state(self, state: OperatorFailureState) -> None:
        self._failures[state.operator_id] = state


@runtime_checkable
class OperatorDirectoryPort(Protocol):
    """Pre-2FA credential check, owned by the spec/15 IAM surface.

    Returns the operator's roles when the password token is valid, else
    ``None`` (the gateway fails closed on ``None``).
    """

    def verify_password_token(
        self, operator_id: str, password_token: str
    ) -> tuple[str, ...] | None: ...


class InMemoryOperatorDirectory:
    """Test/dev directory: operators registered with hashed password tokens."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, tuple[str, ...]]] = {}

    def register(self, operator_id: str, password_token: str, roles: Sequence[str]) -> None:
        unknown = set(roles) - OPERATOR_ROLES
        if unknown:
            raise ValueError(f"unknown roles {sorted(unknown)}; catalogue is closed")
        self._rows[operator_id] = (sha256_hex(password_token), tuple(roles))

    def verify_password_token(
        self, operator_id: str, password_token: str
    ) -> tuple[str, ...] | None:
        row = self._rows.get(operator_id)
        if row is None:
            return None
        token_hash, roles = row
        if not constant_time_equals(sha256_hex(password_token), token_hash):
            return None
        return roles


@dataclass(frozen=True)
class TotpEnrollmentResult:
    """Returned once at enrollment; nothing here is ever stored raw."""

    operator_id: str
    totp_secret: str
    qr_url: str
    backup_codes: tuple[str, ...]


class OperatorAuthService:
    """Enroll TOTP, run the 2FA step, verify/terminate sessions."""

    def __init__(
        self,
        store: OperatorStore,
        directory: OperatorDirectoryPort,
        *,
        clock: Clock,
        settings: GatewaySettings,
    ) -> None:
        self._store = store
        self._directory = directory
        self._clock = clock
        self._settings = settings

    # -- enrollment ---------------------------------------------------------

    def enroll_totp(self, operator_id: str) -> TotpEnrollmentResult:
        if not operator_id:
            raise AuthenticationError("operator_id required", code="credential_invalid")
        now = self._clock.now()
        secret = totp_mod.generate_totp_secret()
        codes = tuple(
            str(int.from_bytes(os.urandom(8), "big") % (10**_BACKUP_CODE_DIGITS)).zfill(
                _BACKUP_CODE_DIGITS
            )
            for _ in range(_BACKUP_CODE_COUNT)
        )
        self._store.save_enrollment(
            OperatorTotpEnrollment(
                operator_id=operator_id,
                totp_secret_enc=encrypt_blob(
                    self._settings.hmac_master_key, secret.encode("ascii")
                ),
                backup_code_hashes=tuple(sha256_hex(code) for code in codes),
                enrolled_at=now,
            )
        )
        return TotpEnrollmentResult(
            operator_id=operator_id,
            totp_secret=secret,
            qr_url=totp_mod.provisioning_uri(operator_id, secret),
            backup_codes=codes,
        )

    # -- failure / lockout bookkeeping ---------------------------------------

    def _check_locked(self, operator_id: str, now: datetime) -> None:
        state = self._store.get_failure_state(operator_id)
        if state is not None and state.locked_until is not None and now < state.locked_until:
            raise AuthenticationError(
                "operator account is locked after repeated 2FA failures",
                code="operator_account_locked",
            )

    def _record_failure(self, operator_id: str, now: datetime) -> None:
        state = self._store.get_failure_state(operator_id)
        window = timedelta(seconds=self._settings.totp_failure_window_seconds)
        if (
            state is None
            or state.window_started_at is None
            or now - state.window_started_at > window
        ):
            attempts, started = 1, now
        else:
            attempts, started = state.failed_attempts + 1, state.window_started_at
        locked_until = None
        if attempts >= self._settings.totp_max_failures:
            locked_until = now + timedelta(seconds=self._settings.operator_lock_seconds)
        self._store.save_failure_state(
            OperatorFailureState(
                operator_id=operator_id,
                failed_attempts=attempts,
                window_started_at=started,
                locked_until=locked_until,
            )
        )
        if locked_until is not None:
            raise AuthenticationError(
                "operator account is locked after repeated 2FA failures",
                code="operator_account_locked",
            )

    def _reset_failures(self, operator_id: str) -> None:
        self._store.save_failure_state(
            OperatorFailureState(
                operator_id=operator_id,
                failed_attempts=0,
                window_started_at=None,
                locked_until=None,
            )
        )

    # -- the 2FA step --------------------------------------------------------

    def verify_totp(
        self,
        *,
        operator_id: str,
        totp_code: str,
        password_token: str,
        client_ip: str = "",
        user_agent: str = "",
    ) -> tuple[str, OperatorSession]:
        """Run the 2FA step; on success issue an opaque session token."""
        now = self._clock.now()
        self._check_locked(operator_id, now)
        roles = self._directory.verify_password_token(operator_id, password_token)
        if roles is None:
            raise AuthenticationError(
                "operator credentials rejected", code="credential_invalid"
            )
        enrollment = self._store.get_enrollment(operator_id)
        if enrollment is None:
            raise AuthenticationError(
                "operator has no 2FA enrollment", code="totp_not_enrolled"
            )
        secret = decrypt_blob(
            self._settings.hmac_master_key, enrollment.totp_secret_enc
        ).decode("ascii")
        ok = totp_mod.verify_totp(
            secret,
            totp_code,
            at=now,
            step_seconds=self._settings.totp_step_seconds,
            digits=self._settings.totp_digits,
            window_steps=self._settings.totp_window_steps,
        )
        if not ok:
            # backup code path: constant-time match against stored digests;
            # a matched code is consumed (removed) before the save below
            code_hash = sha256_hex(totp_code)
            remaining = tuple(
                h for h in enrollment.backup_code_hashes if not constant_time_equals(h, code_hash)
            )
            if len(remaining) != len(enrollment.backup_code_hashes):
                enrollment = replace(enrollment, backup_code_hashes=remaining)
                ok = True
        if not ok:
            self._record_failure(operator_id, now)
            raise AuthenticationError("2FA code rejected", code="totp_invalid")
        self._reset_failures(operator_id)
        self._store.save_enrollment(replace(enrollment, last_used_at=now))

        token = os.urandom(32).hex()
        jti = sha256_hex(token)
        session = OperatorSession(
            session_id=make_gateway_id("osess", {"operator_id": operator_id, "jti": jti}),
            operator_id=operator_id,
            jti=jti,
            roles=tuple(roles),
            ip_hash=sha256_hex(client_ip),
            user_agent_hash=sha256_hex(user_agent),
            status="ACTIVE",
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.operator_session_ttl_seconds),
            last_activity_at=now,
            totp_verified_at=now,
        )
        self._store.insert_session(session)
        return token, session

    # -- session verification -------------------------------------------------

    def authenticate_session(self, session_token: str) -> Principal:
        """Verify an opaque operator session token (spec/01 §C algorithm)."""
        now = self._clock.now()
        session = self._store.get_session_by_jti(sha256_hex(session_token))
        if session is None or session.status != "ACTIVE":
            raise AuthenticationError("operator session rejected", code="session_invalid")
        if now >= session.expires_at:
            self._store.save_session(
                replace(
                    session,
                    status="TERMINATED",
                    terminated_at=now,
                    termination_reason="session_expired",
                )
            )
            raise AuthenticationError("operator session expired", code="session_expired")
        idle = timedelta(seconds=self._settings.operator_inactivity_timeout_seconds)
        if now - session.last_activity_at > idle:
            self._store.save_session(
                replace(
                    session,
                    status="TERMINATED",
                    terminated_at=now,
                    termination_reason="inactivity_timeout",
                )
            )
            raise AuthenticationError(
                "operator session timed out from inactivity", code="session_inactive"
            )
        session = replace(session, last_activity_at=now)
        self._store.save_session(session)
        return Principal(
            kind="operator",
            principal_id=session.session_id,
            operator_id=session.operator_id,
            roles=frozenset(session.roles),
            totp_verified_at=session.totp_verified_at,
        )

    def logout(self, session_token: str) -> None:
        now = self._clock.now()
        session = self._store.get_session_by_jti(sha256_hex(session_token))
        if session is None or session.status != "ACTIVE":
            return
        self._store.save_session(
            replace(
                session, status="TERMINATED", terminated_at=now, termination_reason="logout"
            )
        )
