"""Public simulator sandbox — signup FSM, provisioning, isolation guards
(spec/16 LR-4 §API F + §FSM 4; SANDBOX_PUBLIC deployments only).

States: ``CREATED``, ``VERIFIED``, ``PROVISIONED``/``EXPIRED``/``REVOKED``
(terminal, immutable; refusal-first — invalid transitions raise, never
coerce):

- ``CREATED -> VERIFIED``     — OTP match; attempts <= 5; not expired;
  aud ``SANDBOX_SIGNUP_VERIFIED``.
- ``VERIFIED -> PROVISIONED`` — the spec/08 KYB FSM reached ``ACTIVE``
  (simulator-backed, UNMODIFIED) and a ``bdpk_test_`` key was minted;
  aud ``SANDBOX_SIGNUP_PROVISIONED`` + event ``sandbox_signup.activated``.
- ``CREATED -> EXPIRED``      — ``clock >= created_at + SANDBOX_SIGNUP_TTL_HOURS``
  (default 24); aud ``SANDBOX_SIGNUP_EXPIRED``.
- ``CREATED/VERIFIED -> REVOKED`` — failed-OTP cap / abuse / operator;
  aud ``SANDBOX_SIGNUP_REVOKED`` (reason); minted keys (if any) revoked via
  the spec/01 ApiKey FSM.

OTP-attempt semantics (pinned by test; errata material): the spec FSM guard
says "attempts <= 5" while the route table says "5 attempts max, then
REVOKED" — resolved as: the FIFTH failed attempt revokes the signup.

PII posture: the store carries ONLY ``sha256(lowercased email)``; the raw
email exists exclusively on the notification dispatch path, delivered through
the injected ``bind_recipient_email`` hook (the deployment's recipient
directory used by the email connector). The opaque ``recipient_id`` handed to
the NotificationService is the ``sbxs_`` id itself — a raw email as a
template var would be refused by the PII screen, by construction. The OTP is
stored only as a sha256 digest in a separate short-lived vault (deployment:
Redis) because the spec DDL deliberately has no ``otp_hash`` column (errata
material; pinned by test); it is never logged.

Provisioning failure leaves the signup ``VERIFIED`` with no merchant/key
binding (no partial tenant) and fires the ops-alert hook; ``provision`` is
re-invokable from ``VERIFIED`` once the cause clears.

SANDBOX_PUBLIC isolation invariants (spec/16 §API F, each gate-tested):

1. Separate deployment/DB/OpenBao namespace — a deployment property; this
   module's contribution is that nothing here ever reads rail credentials.
2. :class:`SandboxPublicGuard` — refuse boot / disable + CRITICAL page on any
   connector whose ``active_mode`` is not SIMULATOR. ``DISABLED`` rows are
   tolerated (judgment call, errata material: the literal "NOT IN
   ('SIMULATOR')" wording would deadlock with the sweep's own DISABLED rows;
   a disabled connector is unreachable, which is the invariant's intent).
3. :class:`SandboxKeyMinter` — ``bdpk_test_`` only; the live-key path raises
   :class:`SandboxIsolationError`; ``boot_selftest`` proves the refusal at
   boot.
4. :class:`EgressAllowlist` — HTTPS merchant webhook URLs + simulator
   loopback only.

Events ride topic ``payment.events`` via the injected OutboxPort, producer
``gateway``; every transition writes exactly one audit event (spec/00 §8).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import urlsplit

from bdpay.gateway.credentials import constant_time_equals, sha256_hex
from bdpay.gateway.ids_ext import make_gateway_id
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthenticationError,
    ConflictError,
    InternalError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.notifications import NotificationService, NotificationTemplate
from bdpay.platform.outbox import build_event

if TYPE_CHECKING:
    from bdpay.connectors.registry import ConnectorRegistry
    from bdpay.gateway.apikeys import ApiKeyService, CreatedApiKey
    from bdpay.platform.notifications import TemplateRegistry
    from bdpay.platform.observability import MetricsRegistry

__all__ = [
    "DEFAULT_SANDBOX_SIGNUP_TTL_HOURS",
    "EgressAllowlist",
    "InMemoryOtpVault",
    "InMemorySandboxSignupStore",
    "OtpVault",
    "PostgresSandboxSignupStore",
    "SANDBOX_OTP_TEMPLATE",
    "SandboxIsolationError",
    "SandboxKeyMinter",
    "SandboxPublicGuard",
    "SandboxSignupRecord",
    "SandboxSignupService",
    "SandboxSignupStore",
]

#: spec/16 §Config ``SANDBOX_SIGNUP_TTL_HOURS`` default.
DEFAULT_SANDBOX_SIGNUP_TTL_HOURS = 24

#: spec/16 route table: "5 attempts max, then signup REVOKED".
MAX_OTP_ATTEMPTS = 5

_STATES = ("CREATED", "VERIFIED", "PROVISIONED", "EXPIRED", "REVOKED")
_TERMINAL_STATES = frozenset({"PROVISIONED", "EXPIRED", "REVOKED"})

_TOPIC = "payment.events"
_SUBJECT_TYPE = "sandbox_signup"

#: Dedicated sandbox system operators satisfying the two-eyes activation gate
#: (spec/16 §API F verify route; recorded in ApprovalRequest rows exactly as
#: in production).
PROVISIONER_1 = "system:sandbox-provisioner-1"
PROVISIONER_2 = "system:sandbox-provisioner-2"
SWEEP_ACTOR = "system:sandbox-isolation-sweep"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SandboxIsolationError(ConflictError):
    """A SANDBOX_PUBLIC isolation invariant would be violated (refuse)."""


def _rfc3339_ms(dt: datetime) -> str:
    """RFC3339 UTC string, millisecond precision, ``Z`` suffix (E12 form)."""
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f".{dt.microsecond // 1000:03d}Z"
    )


def _require_aware(value: datetime, what: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(f"{what} must be timezone-aware (naive rejected, spec 00 §7)")
    return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxSignupRecord:
    """One ``sandbox_signups`` row (migration 0092)."""

    sandbox_signup_id: str
    email_hash: str
    display_name: str
    created_at: datetime
    state: str = "CREATED"
    otp_attempts: int = 0
    merchant_id: str | None = None
    api_key_id: str | None = None
    revoke_reason: str | None = None
    provisioned_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"state must be one of {_STATES}, got {self.state!r}")
        if isinstance(self.otp_attempts, bool) or not isinstance(self.otp_attempts, int):
            raise ValueError("otp_attempts must be an integer")
        if self.otp_attempts < 0:
            raise ValueError("otp_attempts must be >= 0")

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


@runtime_checkable
class SandboxSignupStore(Protocol):
    """Storage contract for sandbox signups (migration 0092)."""

    def insert(self, record: SandboxSignupRecord) -> None: ...

    def save(self, record: SandboxSignupRecord) -> None: ...

    def get(self, sandbox_signup_id: str) -> SandboxSignupRecord | None: ...

    def list_for_email(self, email_hash: str) -> Sequence[SandboxSignupRecord]: ...

    def list_created_before(self, cutoff: datetime) -> Sequence[SandboxSignupRecord]: ...


class InMemorySandboxSignupStore:
    """Deterministic in-memory store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, SandboxSignupRecord] = {}
        self._order: list[str] = []

    def insert(self, record: SandboxSignupRecord) -> None:
        if record.sandbox_signup_id in self._rows:
            raise ValueError(f"sandbox signup {record.sandbox_signup_id!r} already exists")
        self._rows[record.sandbox_signup_id] = record
        self._order.append(record.sandbox_signup_id)

    def save(self, record: SandboxSignupRecord) -> None:
        if record.sandbox_signup_id not in self._rows:
            raise ValueError(f"unknown sandbox signup {record.sandbox_signup_id!r}")
        self._rows[record.sandbox_signup_id] = record

    def get(self, sandbox_signup_id: str) -> SandboxSignupRecord | None:
        return self._rows.get(sandbox_signup_id)

    def list_for_email(self, email_hash: str) -> Sequence[SandboxSignupRecord]:
        rows = [r for r in self._rows.values() if r.email_hash == email_hash]
        rows.sort(key=lambda r: (r.created_at, r.sandbox_signup_id), reverse=True)
        return rows

    def list_created_before(self, cutoff: datetime) -> Sequence[SandboxSignupRecord]:
        cutoff = _require_aware(cutoff, "cutoff")
        return [
            self._rows[key]
            for key in self._order
            if self._rows[key].state == "CREATED" and self._rows[key].created_at <= cutoff
        ]


_SBXS_COLUMNS = """
    sandbox_signup_id, email_hash, display_name, state, otp_attempts,
    merchant_id, api_key_id, revoke_reason, created_at, provisioned_at,
    schema_version
"""


def _signup_from_row(row: Sequence[Any]) -> SandboxSignupRecord:
    return SandboxSignupRecord(
        sandbox_signup_id=row[0],
        email_hash=row[1],
        display_name=row[2],
        state=row[3],
        otp_attempts=row[4],
        merchant_id=row[5],
        api_key_id=row[6],
        revoke_reason=row[7],
        created_at=row[8],
        provisioned_at=row[9],
        schema_version=row[10],
    )


class PostgresSandboxSignupStore:
    """psycopg3 store over migration 0092 (gateway pg style: small txns)."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connect = connection_factory

    def _run(self, fn: Callable[[Any], Any]) -> Any:
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    result = fn(cur)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    def insert(self, record: SandboxSignupRecord) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                INSERT INTO sandbox_signups (
                    sandbox_signup_id, email_hash, display_name, state,
                    otp_attempts, merchant_id, api_key_id, revoke_reason,
                    created_at, provisioned_at, schema_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.sandbox_signup_id,
                    record.email_hash,
                    record.display_name,
                    record.state,
                    record.otp_attempts,
                    record.merchant_id,
                    record.api_key_id,
                    record.revoke_reason,
                    record.created_at,
                    record.provisioned_at,
                    record.schema_version,
                ),
            )

        self._run(op)

    def save(self, record: SandboxSignupRecord) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE sandbox_signups SET
                    state = %s, otp_attempts = %s, merchant_id = %s,
                    api_key_id = %s, revoke_reason = %s, provisioned_at = %s
                WHERE sandbox_signup_id = %s
                """,
                (
                    record.state,
                    record.otp_attempts,
                    record.merchant_id,
                    record.api_key_id,
                    record.revoke_reason,
                    record.provisioned_at,
                    record.sandbox_signup_id,
                ),
            )
            if cur.rowcount != 1:
                raise ValueError(f"unknown sandbox signup {record.sandbox_signup_id!r}")

        self._run(op)

    def get(self, sandbox_signup_id: str) -> SandboxSignupRecord | None:
        def op(cur: Any) -> SandboxSignupRecord | None:
            cur.execute(
                f"SELECT {_SBXS_COLUMNS} FROM sandbox_signups WHERE sandbox_signup_id = %s",
                (sandbox_signup_id,),
            )
            row = cur.fetchone()
            return None if row is None else _signup_from_row(row)

        return self._run(op)

    def list_for_email(self, email_hash: str) -> Sequence[SandboxSignupRecord]:
        def op(cur: Any) -> Sequence[SandboxSignupRecord]:
            cur.execute(
                f"""
                SELECT {_SBXS_COLUMNS} FROM sandbox_signups
                WHERE email_hash = %s
                ORDER BY created_at DESC, sandbox_signup_id
                """,
                (email_hash,),
            )
            return [_signup_from_row(row) for row in cur.fetchall()]

        return self._run(op)

    def list_created_before(self, cutoff: datetime) -> Sequence[SandboxSignupRecord]:
        cutoff = _require_aware(cutoff, "cutoff")

        def op(cur: Any) -> Sequence[SandboxSignupRecord]:
            cur.execute(
                f"""
                SELECT {_SBXS_COLUMNS} FROM sandbox_signups
                WHERE state = 'CREATED' AND created_at <= %s
                ORDER BY created_at, sandbox_signup_id
                """,
                (cutoff,),
            )
            return [_signup_from_row(row) for row in cur.fetchall()]

        return self._run(op)


# ---------------------------------------------------------------------------
# OTP vault — the spec DDL deliberately has no otp_hash column; the digest
# lives in a separate short-lived vault (deployment: Redis), never the row.
# ---------------------------------------------------------------------------


@runtime_checkable
class OtpVault(Protocol):
    """Short-lived storage of the sha256(OTP) digest, keyed by signup id."""

    def put(self, sandbox_signup_id: str, otp_hash: str) -> None: ...

    def get(self, sandbox_signup_id: str) -> str | None: ...

    def drop(self, sandbox_signup_id: str) -> None: ...


class InMemoryOtpVault:
    """Deterministic in-memory vault for unit tests."""

    def __init__(self) -> None:
        self._digests: dict[str, str] = {}

    def put(self, sandbox_signup_id: str, otp_hash: str) -> None:
        self._digests[sandbox_signup_id] = otp_hash

    def get(self, sandbox_signup_id: str) -> str | None:
        return self._digests.get(sandbox_signup_id)

    def drop(self, sandbox_signup_id: str) -> None:
        self._digests.pop(sandbox_signup_id, None)


# ---------------------------------------------------------------------------
# Notification template (spec/16 §Notifications — registered additively;
# bilingual per the spec/15 no-late-translation rule)
# ---------------------------------------------------------------------------

SANDBOX_OTP_TEMPLATE = NotificationTemplate(
    template_id="sandbox_signup_otp",
    body_en=(
        "Your BD-PAY sandbox verification code is {otp_code}. "
        "It expires in {ttl_hours} hours."
    ),
    body_bn=(
        "আপনার BD-PAY স্যান্ডবক্স যাচাইকরণ কোড {otp_code}। "
        "এটি {ttl_hours} ঘণ্টার মধ্যে মেয়াদোত্তীর্ণ হবে।"
    ),
    required_vars=("otp_code", "ttl_hours"),
)


def _default_otp_generator() -> str:
    """Six decimal digits from the OS CSPRNG (``secrets``; never ``random``)."""
    return f"{secrets.randbelow(1_000_000):06d}"


# ---------------------------------------------------------------------------
# Isolation invariant 3: bdpk_test_ only
# ---------------------------------------------------------------------------


class SandboxKeyMinter:
    """The ONLY key-minting path in a SANDBOX_PUBLIC deployment.

    Wraps the spec/01 :class:`ApiKeyService` and structurally refuses any
    request for a live key (spec/16 §API F invariant 3): the guard runs
    before the underlying service is touched, so a ``bdpk_live_`` secret can
    never be generated. ``boot_selftest`` exercises the refusal at startup.
    """

    def __init__(self, api_keys: ApiKeyService) -> None:
        self._api_keys = api_keys

    def mint(
        self,
        *,
        merchant_id: str,
        key_name: str,
        scopes: Sequence[str],
        env: str = "test",
    ) -> CreatedApiKey:
        if env != "test":
            raise SandboxIsolationError(
                "SANDBOX_PUBLIC deployments mint bdpk_test_ keys only; the live "
                "key path is refused by construction (spec/16 invariant 3)",
                code="sandbox_isolation_violation",
            )
        return self._api_keys.create_key(
            merchant_id=merchant_id, key_name=key_name, scopes=scopes, env="test"
        )

    def revoke(self, key_id: str) -> None:
        """Revoke a minted key via the spec/01 ApiKey FSM."""
        self._api_keys.revoke_key(key_id)

    def boot_selftest(self) -> None:
        """Prove at boot that the live path raises (spec/16 invariant 3 gate).

        The guard fires before any secret material is generated, so the
        self-test creates nothing.
        """
        try:
            self.mint(
                merchant_id="mrch_boot_selftest",
                key_name="boot-selftest",
                scopes=("payment:read",),
                env="live",
            )
        except SandboxIsolationError:
            return
        raise SandboxIsolationError(
            "live-key mint path failed to refuse during the boot self-test; "
            "refusing to boot (spec/16 invariant 3)",
            code="sandbox_isolation_violation",
        )


# ---------------------------------------------------------------------------
# Isolation invariant 2: SIMULATOR-only connector registry
# ---------------------------------------------------------------------------


class SandboxPublicGuard:
    """Boot assert + registry sweep for SANDBOX_PUBLIC (spec/16 invariant 2).

    ``DISABLED`` rows are tolerated in both checks: a disabled connector is
    unreachable, and the sweep itself produces DISABLED rows — the literal
    spec wording ("NOT IN ('SIMULATOR')") would otherwise refuse its own
    remediation output (judgment call; errata material).
    """

    def __init__(
        self,
        *,
        metrics: MetricsRegistry | None = None,
        page: Callable[[str], None] | None = None,
    ) -> None:
        self._metrics = metrics
        self._page = page

    @staticmethod
    def _offenders(registry: ConnectorRegistry) -> list[str]:
        return [
            registration.connector_id
            for registration in registry.list_all()
            if registration.active_mode.value not in ("SIMULATOR", "DISABLED")
        ]

    def assert_boot_invariants(
        self, registry: ConnectorRegistry, *, key_minter: SandboxKeyMinter
    ) -> None:
        """Refuse boot on any non-SIMULATOR connector; then run the live-key
        refusal self-test (invariants 2 + 3, both release-blocking gates)."""
        offenders = self._offenders(registry)
        if offenders:
            raise SandboxIsolationError(
                f"SANDBOX_PUBLIC boot refused: connectors {offenders} are not in "
                "SIMULATOR mode (spec/16 invariant 2)",
                code="sandbox_isolation_violation",
            )
        key_minter.boot_selftest()

    def sweep(self, registry: ConnectorRegistry) -> list[str]:
        """Runtime sweep: disable offenders + CRITICAL metric/page hook.

        Returns the connector ids disabled by this pass.
        """
        disabled: list[str] = []
        for connector_id in self._offenders(registry):
            registry.disable(
                connector_id,
                actor_id=SWEEP_ACTOR,
                reason="SANDBOX_PUBLIC isolation sweep: non-SIMULATOR mode (spec/16 inv 2)",
            )
            if self._metrics is not None:
                self._metrics.increment(
                    "sandbox_isolation_violations_total",
                    labels={"connector_id": connector_id},
                )
            if self._page is not None:
                self._page(connector_id)
            disabled.append(connector_id)
        return disabled


# ---------------------------------------------------------------------------
# Isolation invariant 4: egress allowlist
# ---------------------------------------------------------------------------


class EgressAllowlist:
    """Outbound HTTP policy: HTTPS merchant webhooks + simulator loopback
    only (spec/16 invariant 4). Everything else is refused — there is no
    network route to any production system by construction."""

    def __init__(
        self,
        *,
        webhook_hosts: Iterable[str],
        simulator_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
    ) -> None:
        self._webhook_hosts = frozenset(h.strip().lower() for h in webhook_hosts if h.strip())
        self._simulator_hosts = frozenset(
            h.strip().lower() for h in simulator_hosts if h.strip()
        )

    def is_allowed(self, url: str) -> bool:
        if not isinstance(url, str) or not url:
            return False
        parts = urlsplit(url)
        netloc = parts.netloc.lower()
        host = parts.hostname.lower() if parts.hostname else ""
        if not host:
            return False
        # Simulator loopback: any scheme, matched with or without the port.
        if netloc in self._simulator_hosts or host in self._simulator_hosts:
            return True
        # Merchant webhooks: HTTPS only, exact host match.
        if parts.scheme == "https" and (
            netloc in self._webhook_hosts or host in self._webhook_hosts
        ):
            return True
        return False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SandboxSignupService:
    """Self-serve sandbox registrations: signup -> OTP -> provision."""

    def __init__(
        self,
        store: SandboxSignupStore,
        *,
        onboarding: Any,
        key_minter: SandboxKeyMinter,
        notifications: NotificationService,
        template_registry: TemplateRegistry,
        audit: AuditPort,
        outbox: OutboxPort,
        token_secret: str,
        sandbox_base_url: str,
        docs_url: str,
        signup_ttl_hours: int = DEFAULT_SANDBOX_SIGNUP_TTL_HOURS,
        otp_generator: Callable[[], str] | None = None,
        otp_vault: OtpVault | None = None,
        bind_recipient_email: Callable[[str, str], None] | None = None,
        metrics: MetricsRegistry | None = None,
        ops_alert: Callable[[str, str], None] | None = None,
        producer: str = "gateway",
    ) -> None:
        """``onboarding`` is the REAL, unmodified spec/08
        :class:`~bdpay.kernel.onboarding.kyb.MerchantOnboardingService`
        wired against simulator-backed checks (typed structurally to keep the
        gateway free of a hard kernel import)."""
        if signup_ttl_hours < 1:
            raise ValueError("signup_ttl_hours must be >= 1")
        if not token_secret:
            raise ValueError("token_secret must be non-empty")
        if not sandbox_base_url or not docs_url:
            raise ValueError("sandbox_base_url and docs_url must be non-empty")
        self._store = store
        self._onboarding = onboarding
        self._key_minter = key_minter
        self._notifications = notifications
        self._audit = audit
        self._outbox = outbox
        self._token_secret = token_secret
        self._sandbox_base_url = sandbox_base_url
        self._docs_url = docs_url
        self._ttl = timedelta(hours=signup_ttl_hours)
        self._ttl_hours = signup_ttl_hours
        self._otp_generator = otp_generator or _default_otp_generator
        self._vault: OtpVault = otp_vault if otp_vault is not None else InMemoryOtpVault()
        self._bind_recipient_email = bind_recipient_email or (lambda _sbxs_id, _email: None)
        self._metrics = metrics
        self._ops_alert = ops_alert or (lambda _sbxs_id, _reason: None)
        self._producer = producer
        # Additive registration: the deployment may have seeded it already.
        try:
            template_registry.get(SANDBOX_OTP_TEMPLATE.template_id)
        except Exception:
            template_registry.register(SANDBOX_OTP_TEMPLATE)

    # -- helpers -----------------------------------------------------------

    def signup_token(self, sandbox_signup_id: str) -> str:
        """Content-addressed state-read token: id + per-deployment secret."""
        return sha256_hex(f"sbxs-token:{sandbox_signup_id}:{self._token_secret}")

    def _require(self, sandbox_signup_id: str) -> SandboxSignupRecord:
        record = self._store.get(sandbox_signup_id)
        if record is None:
            raise NotFoundError("sandbox signup not found", code="sandbox_signup_not_found")
        return record

    def _audit_event(
        self,
        event_type: str,
        record: SandboxSignupRecord,
        *,
        actor_id: str,
        from_state: str | None,
        to_state: str,
        payload: dict,
        clock: Clock,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type=event_type,
                actor_id=actor_id,
                subject_type=_SUBJECT_TYPE,
                subject_id=record.sandbox_signup_id,
                from_state=from_state,
                to_state=to_state,
                payload=payload,
            ),
            clock=clock,
        )

    # -- POST /v1/sandbox/signups -------------------------------------------

    def signup(self, email: str, display_name: str, clock: Clock) -> dict:
        """Create the signup and dispatch the OTP email.

        The raw email is hashed immediately; it leaves this method only
        through ``bind_recipient_email`` (the notification dispatch path).
        """
        if not isinstance(email, str) or not _EMAIL_RE.fullmatch(email.strip()):
            raise InvalidRequestError("email is not valid", code="invalid_email")
        if not isinstance(display_name, str) or not display_name.strip():
            raise InvalidRequestError("display_name is required", code="invalid_request")
        if len(display_name) > 120:
            raise InvalidRequestError("display_name is too long", code="invalid_request")
        email = email.strip().lower()
        email_hash = sha256_hex(email)
        now = clock.now()
        sandbox_signup_id = make_gateway_id(
            "sbxs", {"email_hash": email_hash, "created_at": _rfc3339_ms(now)}
        )
        existing = self._store.get(sandbox_signup_id)
        if existing is not None:
            return self._signup_view(existing)  # idempotent replay; no new OTP

        record = SandboxSignupRecord(
            sandbox_signup_id=sandbox_signup_id,
            email_hash=email_hash,
            display_name=display_name.strip(),
            created_at=now,
        )
        self._store.insert(record)
        otp = self._otp_generator()
        self._vault.put(sandbox_signup_id, sha256_hex(otp))
        self._audit_event(
            "SANDBOX_SIGNUP_CREATED",
            record,
            actor_id="public",
            from_state=None,
            to_state="CREATED",
            payload={"email_hash": email_hash},  # never the raw email, never the OTP
            clock=clock,
        )
        # Raw email exists ONLY on the dispatch path: the recipient directory
        # binding used by the (sandbox) email connector. The opaque
        # recipient_id handed to the queue is the signup id.
        self._bind_recipient_email(sandbox_signup_id, email)
        self._notifications.dispatch(
            recipient_type="merchant",
            recipient_id=sandbox_signup_id,
            channel="email",
            template_id=SANDBOX_OTP_TEMPLATE.template_id,
            template_vars={"otp_code": otp, "ttl_hours": str(self._ttl_hours)},
            idempotency_key=f"sbxs-otp:{sandbox_signup_id}",
            language="en",
        )
        return self._signup_view(record)

    def _signup_view(self, record: SandboxSignupRecord) -> dict:
        return {
            "sandbox_signup_id": record.sandbox_signup_id,
            "state": record.state,
            "signup_token": self.signup_token(record.sandbox_signup_id),
            "expires_at": _rfc3339_ms(record.created_at + self._ttl),
        }

    # -- POST /v1/sandbox/signups/{sbxs_id}/verify ----------------------------

    def verify(self, sandbox_signup_id: str, otp: str, clock: Clock) -> dict:
        """OTP check, then synchronous provisioning (spec/16 §API F).

        The FIFTH failed attempt revokes the signup (pinned semantics). On a
        match the row is persisted VERIFIED first, then provisioning runs; a
        provisioning failure leaves it VERIFIED for a clean retry.
        """
        record = self._require(sandbox_signup_id)
        if record.is_terminal:
            raise ConflictError(
                f"sandbox signup is {record.state}; terminal rows are immutable "
                "(refusal-first FSM)",
                code="sandbox_signup_terminal",
            )
        if record.state == "VERIFIED":
            raise ConflictError(
                "sandbox signup is already VERIFIED; provisioning can be retried",
                code="sandbox_signup_already_verified",
            )
        now = clock.now()
        if now >= record.created_at + self._ttl:
            self._expire(record, clock)
            raise ConflictError("sandbox signup has expired", code="sandbox_signup_expired")
        if not isinstance(otp, str) or not otp:
            raise InvalidRequestError("otp is required", code="invalid_request")
        stored = self._vault.get(sandbox_signup_id)
        if stored is None or not constant_time_equals(sha256_hex(otp), stored):
            record = replace(record, otp_attempts=record.otp_attempts + 1)
            if record.otp_attempts >= MAX_OTP_ATTEMPTS:
                self._store.save(record)
                self._revoke(record, reason="otp_attempts_exceeded", actor_id="public",
                             clock=clock)
                raise AuthenticationError(
                    "verification code attempts exhausted; signup revoked",
                    code="otp_attempts_exceeded",
                )
            self._store.save(record)
            raise AuthenticationError(
                "verification code mismatch", code="otp_invalid"
            )
        record = replace(record, state="VERIFIED")
        self._store.save(record)
        self._vault.drop(sandbox_signup_id)
        self._audit_event(
            "SANDBOX_SIGNUP_VERIFIED",
            record,
            actor_id="public",
            from_state="CREATED",
            to_state="VERIFIED",
            payload={"otp_attempts": record.otp_attempts},
            clock=clock,
        )
        return self.provision(sandbox_signup_id, clock)

    # -- provisioning (VERIFIED -> PROVISIONED) -------------------------------

    def provision(self, sandbox_signup_id: str, clock: Clock) -> dict:
        """Drive the unmodified spec/08 KYB FSM to ACTIVE and mint the key.

        Re-invokable from VERIFIED (the retry path after a provisioning
        failure; a fresh deterministic merchant identity is derived from the
        clock instant). Any failure leaves the signup VERIFIED with no
        merchant/key binding (no partial tenant) and fires the ops alert.
        """
        record = self._require(sandbox_signup_id)
        if record.state != "VERIFIED":
            raise ConflictError(
                "provisioning runs only from VERIFIED (refusal-first FSM)",
                code="sandbox_signup_not_verified",
            )
        try:
            merchant_id, created = self._drive_kyb(record, clock)
        except Exception as exc:
            if self._metrics is not None:
                self._metrics.increment("sandbox_provisioning_failures_total")
            self._ops_alert(sandbox_signup_id, f"{type(exc).__name__}: {exc}")
            raise InternalError(
                "sandbox provisioning failed; the signup remains VERIFIED and can "
                "be retried",
                code="sandbox_provisioning_failed",
            ) from exc
        now = clock.now()
        record = replace(
            record,
            state="PROVISIONED",
            merchant_id=merchant_id,
            api_key_id=created.record.key_id,
            provisioned_at=now,
        )
        self._store.save(record)
        self._audit_event(
            "SANDBOX_SIGNUP_PROVISIONED",
            record,
            actor_id=PROVISIONER_1,
            from_state="VERIFIED",
            to_state="PROVISIONED",
            payload={"merchant_id": merchant_id, "api_key_id": created.record.key_id},
            clock=clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="sandbox_signup.activated",
                subject_type=_SUBJECT_TYPE,
                subject_id=sandbox_signup_id,
                producer=self._producer,
                topic=_TOPIC,
                payload={"sandbox_signup_id": sandbox_signup_id, "merchant_id": merchant_id},
                occurred_at=now,
            )
        )
        return {
            "sandbox_signup_id": sandbox_signup_id,
            "state": "PROVISIONED",
            "merchant_id": merchant_id,
            "api_key_id": created.record.key_id,
            # the bdpk_test_ secret is shown exactly once, here:
            "api_key_secret": created.secret,
            "sandbox_base_url": self._sandbox_base_url,
            "docs_url": self._docs_url,
        }

    def _drive_kyb(self, record: SandboxSignupRecord, clock: Clock) -> tuple[str, Any]:
        """The exact spec/08 call sequence (FSM unmodified, simulator-backed).

        Identity fields are derived deterministically from the signup id +
        the provisioning instant via the canonical digest — content-addressed,
        no UUIDs, no wall-clock reads.
        """
        # Deferred import: the gateway package has no hard kernel dependency;
        # only the sandbox deployment wires both sides together.
        from bdpay.kernel.onboarding.kyb import MerchantApplication

        onboarding = self._onboarding
        digest = sha256_canonical(
            {
                "sandbox_signup_id": record.sandbox_signup_id,
                "provision_at": _rfc3339_ms(clock.now()),
            }
        )
        tin = f"{int(digest[:12], 16) % 10**12:012d}"
        nid = f"{int(digest[12:24], 16) % 10**10:010d}"
        application = MerchantApplication(
            legal_name=record.display_name,
            registration_type="SOLE_PROPRIETORSHIP",
            trade_license_number=f"TL-SBX-{digest[:8]}",
            tin_number=tin,
            primary_business_mcc="5411",
            contact_phone="01712345678",
            bank_routing_number="123456789",
        )
        merchant, kyb = onboarding.submit_application(application, actor_id=PROVISIONER_1)
        for index, document_type in enumerate(
            ("TRADE_LICENSE", "TIN_CERTIFICATE", "BANK_STATEMENT")
        ):
            onboarding.upload_document(
                kyb.kyb_record_id,
                document_type=document_type,
                content_hash=sha256_hex(f"{digest}:{index}"),
                uploaded_by=PROVISIONER_1,
            )
        onboarding.check_documents_complete(kyb.kyb_record_id, actor_id=PROVISIONER_1)
        node = onboarding.add_ubo_node(
            kyb.kyb_record_id,
            node_type="NATURAL_PERSON",
            full_name_en=record.display_name,
            created_by=PROVISIONER_1,
            nid_number=nid,
            dob="1990-01-01",
            is_ultimate_beneficial_owner=True,
            ownership_percentage_direct="100",
        )
        onboarding.record_porichoy_result(
            node.ubo_node_id,
            outcome="matched",
            actor_id=PROVISIONER_1,
            porichoy_ref=f"SBX-{digest[:8]}",
        )
        onboarding.submit_for_review(kyb.kyb_record_id, actor_id=PROVISIONER_1)
        onboarding.run_sanctions_review(kyb.kyb_record_id, actor_id=PROVISIONER_1)
        # Two-eyes gate satisfied by the two dedicated sandbox system
        # operators, recorded in ApprovalRequest rows exactly as in production.
        onboarding.request_activation(
            kyb.kyb_record_id,
            initiator_id=PROVISIONER_1,
            risk_tier="LOW",
            mdr_basis_points=0,
        )
        onboarding.decide_activation(
            kyb.kyb_record_id, approver_id=PROVISIONER_2, approve=True
        )
        onboarding.sign_agreement(
            kyb.kyb_record_id,
            signer_name=record.display_name,
            signer_nid_last4=nid[-4:],
            actor_id=PROVISIONER_1,
        )
        created = self._key_minter.mint(
            merchant_id=merchant.merchant_id,
            key_name="Sandbox default key",
            scopes=(
                "payment:write",
                "payment:read",
                "refund:write",
                "refund:read",
                "customer:write",
                "customer:read",
                "webhook:write",
                "webhook:read",
            ),
            env="test",
        )
        return merchant.merchant_id, created

    # -- revoke (CREATED/VERIFIED -> REVOKED) ---------------------------------

    def revoke(
        self, sandbox_signup_id: str, *, reason: str, actor_id: str, clock: Clock
    ) -> dict:
        """Abuse / operator revocation. Minted keys (if any) are revoked via
        the spec/01 ApiKey FSM (the spec transition row says so even though
        keys normally exist only at PROVISIONED — errata material)."""
        record = self._require(sandbox_signup_id)
        if record.state not in ("CREATED", "VERIFIED"):
            raise ConflictError(
                f"sandbox signup is {record.state}; revoke runs from CREATED/VERIFIED "
                "only (refusal-first FSM)",
                code="sandbox_signup_terminal",
            )
        if not reason:
            raise InvalidRequestError("revoke_reason is required", code="invalid_request")
        self._revoke(record, reason=reason, actor_id=actor_id, clock=clock)
        return {"sandbox_signup_id": sandbox_signup_id, "state": "REVOKED"}

    def _revoke(
        self, record: SandboxSignupRecord, *, reason: str, actor_id: str, clock: Clock
    ) -> None:
        from_state = record.state
        updated = replace(record, state="REVOKED", revoke_reason=reason)
        self._store.save(updated)
        self._vault.drop(record.sandbox_signup_id)
        if record.api_key_id is not None:
            self._key_minter.revoke(record.api_key_id)
        self._audit_event(
            "SANDBOX_SIGNUP_REVOKED",
            updated,
            actor_id=actor_id,
            from_state=from_state,
            to_state="REVOKED",
            payload={"reason": reason, "api_key_id": record.api_key_id},
            clock=clock,
        )

    # -- GET /v1/sandbox/signups/{sbxs_id} (state only, token-gated) ----------

    def get(self, sandbox_signup_id: str, signup_token: str) -> dict:
        """State-only read; a bad token is indistinguishable from a missing
        signup (anti-enumeration)."""
        if not isinstance(signup_token, str) or not constant_time_equals(
            self.signup_token(sandbox_signup_id), signup_token
        ):
            raise NotFoundError("sandbox signup not found", code="sandbox_signup_not_found")
        record = self._require(sandbox_signup_id)
        return {"sandbox_signup_id": record.sandbox_signup_id, "state": record.state}

    # -- TTL sweep (CREATED -> EXPIRED) ---------------------------------------

    def _expire(self, record: SandboxSignupRecord, clock: Clock) -> None:
        self._store.save(replace(record, state="EXPIRED"))
        self._vault.drop(record.sandbox_signup_id)
        self._audit_event(
            "SANDBOX_SIGNUP_EXPIRED",
            record,
            actor_id=self._producer,
            from_state="CREATED",
            to_state="EXPIRED",
            payload={},
            clock=clock,
        )

    def sweep_expiry(self, clock: Clock) -> list[str]:
        """Scheduler sweep: expire CREATED signups past the TTL."""
        expired: list[str] = []
        cutoff = clock.now() - self._ttl
        for record in self._store.list_created_before(cutoff):
            self._expire(record, clock)
            expired.append(record.sandbox_signup_id)
        return expired
