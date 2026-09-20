"""Payment links — entity, FSM, stores, service, consumer (spec/16 LR-4).

States: ``ACTIVE``, ``PAID``/``EXPIRED``/``CANCELLED`` (terminal, immutable;
refusal-first — invalid transitions raise, never coerce):

- ``ACTIVE -> PAID``     — consumed matching ``payment_intent.succeeded``
  (idempotent on ``event_id``; ``single_use`` links only); records
  ``payment_intent_id``; aud ``PAYMENT_LINK_PAID`` + event ``payment_link.paid``.
- ``ACTIVE -> EXPIRED``  — ``clock.now() >= expires_at`` AND no open claim.
  Expiry with an open claim is DEFERRED (a payer mid-checkout is never cut
  off by link TTL); the link transitions on the intent's own terminal event.
- ``ACTIVE -> CANCELLED`` — authorized actor; refused ``409
  link_payment_in_flight`` while an open claim exists.

Single-use concurrency (spec/16 §Failure modes "link race"): the checkout
path inserts a CLAIM row first; ``uidx_plink_one_open_claim`` (partial unique
``WHERE released = FALSE``) makes the second concurrent claim fail and surface
``409 link_not_payable``. The claim is released on the intent's terminal
failure event, making the link payable again. The claim is inserted before
the kernel mints the intent id, so it initially carries the deterministic
server idempotency key as a provisional ``payment_intent_id`` and is bound to
the real intent id immediately after ``PaymentIntentPort.create_intent``
returns.

``public_code`` preimage (pinned by a golden test): the UTF-8 concatenation
``link_id + merchant_id + created_at_rfc3339`` where ``created_at_rfc3339``
is the E12 canonical timestamp form (UTC, millisecond precision, ``Z``
suffix, e.g. ``2026-06-12T10:00:00.000Z``); the code is
``base64.b32encode(sha256(preimage).digest())`` lowercased, unpadded,
truncated to 16 characters.

Events ride topic ``payment.events`` via the injected OutboxPort, producer
``gateway``; every transition writes exactly one audit event (spec/00 §8).
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.gateway.ids_ext import make_gateway_id
from bdpay.gateway.ports import PaymentIntentPort
from bdpay.platform.bangla import AmountParseError, normalize_bengali_digits, parse_bdt_amount
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, InvalidRequestError, NotFoundError
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.money import MoneyError
from bdpay.platform.outbox import build_event
from bdpay.platform.pii import redact

__all__ = [
    "DEFAULT_PAYMENT_LINK_TTL_HOURS",
    "InMemoryPaymentLinkStore",
    "LinkClaimConflict",
    "PaymentLinkClaim",
    "PaymentLinkEventConsumer",
    "PaymentLinkRecord",
    "PaymentLinkService",
    "PaymentLinkStore",
    "PostgresPaymentLinkStore",
    "make_public_code",
]

#: spec/16 §Config ``PAYMENT_LINK_DEFAULT_TTL_HOURS`` default.
DEFAULT_PAYMENT_LINK_TTL_HOURS = 72

_STATES = ("ACTIVE", "PAID", "EXPIRED", "CANCELLED")
_TERMINAL_STATES = frozenset({"PAID", "EXPIRED", "CANCELLED"})

_TOPIC = "payment.events"
_SUBJECT_TYPE = "payment_link"

#: Intent terminal-failure events that release the link's open claim.
_RELEASE_EVENT_TYPES = frozenset(
    {"payment_intent.failed", "payment_intent.cancelled", "payment_intent.expired"}
)


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


def make_public_code(link_id: str, merchant_id: str, created_at: datetime) -> str:
    """Content-addressed public code (spec/16 §API C; not guessable-sequential
    and never a random UUID).

    Preimage = UTF-8 bytes of ``link_id + merchant_id + created_at_rfc3339``
    (E12 millisecond ``Z`` form). Code = base32 of the SHA-256 digest,
    lowercase, padding stripped, first 16 characters.
    """
    preimage = f"{link_id}{merchant_id}{_rfc3339_ms(_require_aware(created_at, 'created_at'))}"
    digest = hashlib.sha256(preimage.encode("utf-8")).digest()
    return base64.b32encode(digest).decode("ascii").rstrip("=").lower()[:16]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PaymentLinkRecord:
    """One ``payment_links`` row (migration 0094)."""

    link_id: str
    merchant_id: str
    public_code: str
    amount_minor: int | None
    amount_min_minor: int | None
    amount_max_minor: int | None
    description: str
    expires_at: datetime
    created_at: datetime
    currency: str = "BDT"
    single_use: bool = True
    state: str = "ACTIVE"
    payment_intent_id: str | None = None
    terminal_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"state must be one of {_STATES}, got {self.state!r}")
        fixed = self.amount_minor is not None
        bounded = self.amount_min_minor is not None and self.amount_max_minor is not None
        # mirror plink_amount_shape + plink_amount_positive
        if fixed:
            if self.amount_min_minor is not None or self.amount_max_minor is not None:
                raise ValueError("amount_minor excludes bounds (plink_amount_shape)")
            if self.amount_minor is not None and self.amount_minor <= 0:
                raise ValueError("amount_minor must be > 0 (plink_amount_positive)")
        elif not bounded:
            raise ValueError("either amount_minor or both bounds required (plink_amount_shape)")
        elif not (
            0 < self.amount_min_minor  # type: ignore[operator]
            and self.amount_min_minor <= self.amount_max_minor  # type: ignore[operator]
        ):
            raise ValueError("bounds require 0 < min <= max (plink_amount_shape)")

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


@dataclass(frozen=True)
class PaymentLinkClaim:
    """One ``payment_link_claims`` row (migration 0094)."""

    link_id: str
    payment_intent_id: str
    claimed_at: datetime
    released: bool = False


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


class LinkClaimConflict(Exception):
    """A different open claim already holds this link (partial unique guard)."""


@runtime_checkable
class PaymentLinkStore(Protocol):
    """Storage contract for payment links + checkout claims."""

    def insert(self, record: PaymentLinkRecord) -> None: ...

    def save(self, record: PaymentLinkRecord) -> None: ...

    def get(self, link_id: str) -> PaymentLinkRecord | None: ...

    def get_by_public_code(self, public_code: str) -> PaymentLinkRecord | None: ...

    def list_for_merchant(
        self, merchant_id: str, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[PaymentLinkRecord], str | None]: ...

    def list_due_active(self, *, now: datetime) -> Sequence[PaymentLinkRecord]: ...

    def claim(self, link_id: str, payment_intent_id: str, *, now: datetime) -> None: ...

    def bind_claim_intent(
        self, link_id: str, provisional_id: str, payment_intent_id: str
    ) -> None: ...

    def open_claim(self, link_id: str) -> PaymentLinkClaim | None: ...

    def find_open_claim_by_intent(self, payment_intent_id: str) -> PaymentLinkClaim | None: ...

    def release_claim(self, link_id: str, payment_intent_id: str) -> None: ...


class InMemoryPaymentLinkStore:
    """Deterministic in-memory store; enforces the one-open-claim invariant
    exactly like ``uidx_plink_one_open_claim`` does at the DB level."""

    def __init__(self) -> None:
        self._rows: dict[str, PaymentLinkRecord] = {}
        self._order: list[str] = []
        self._by_code: dict[str, str] = {}
        self._claims: dict[tuple[str, str], PaymentLinkClaim] = {}

    def insert(self, record: PaymentLinkRecord) -> None:
        if record.link_id in self._rows:
            raise ValueError(f"payment link {record.link_id!r} already exists")
        if record.public_code in self._by_code:
            raise ValueError(f"public_code {record.public_code!r} already exists")
        self._rows[record.link_id] = record
        self._order.append(record.link_id)
        self._by_code[record.public_code] = record.link_id

    def save(self, record: PaymentLinkRecord) -> None:
        if record.link_id not in self._rows:
            raise ValueError(f"unknown payment link {record.link_id!r}")
        self._rows[record.link_id] = record

    def get(self, link_id: str) -> PaymentLinkRecord | None:
        return self._rows.get(link_id)

    def get_by_public_code(self, public_code: str) -> PaymentLinkRecord | None:
        link_id = self._by_code.get(public_code)
        return None if link_id is None else self._rows.get(link_id)

    def list_for_merchant(
        self, merchant_id: str, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[PaymentLinkRecord], str | None]:
        rows = [
            self._rows[link_id]
            for link_id in self._order
            if self._rows[link_id].merchant_id == merchant_id
        ]
        rows.sort(key=lambda r: (r.created_at, r.link_id), reverse=True)
        offset = _parse_offset_cursor(cursor)
        page = rows[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(rows) else None
        return page, next_cursor

    def list_due_active(self, *, now: datetime) -> Sequence[PaymentLinkRecord]:
        now = _require_aware(now, "now")
        return [
            self._rows[link_id]
            for link_id in self._order
            if self._rows[link_id].state == "ACTIVE" and self._rows[link_id].expires_at <= now
        ]

    # -- claims (one-open-claim invariant mirrors uidx_plink_one_open_claim) --

    def claim(self, link_id: str, payment_intent_id: str, *, now: datetime) -> None:
        if link_id not in self._rows:
            raise ValueError(f"unknown payment link {link_id!r}")
        existing = self.open_claim(link_id)
        if existing is not None:
            if existing.payment_intent_id == payment_intent_id:
                return  # idempotent retry of the same claim
            raise LinkClaimConflict(link_id)
        self._claims[(link_id, payment_intent_id)] = PaymentLinkClaim(
            link_id=link_id,
            payment_intent_id=payment_intent_id,
            claimed_at=_require_aware(now, "now"),
        )

    def bind_claim_intent(
        self, link_id: str, provisional_id: str, payment_intent_id: str
    ) -> None:
        claim = self._claims.pop((link_id, provisional_id), None)
        if claim is None:
            return
        self._claims[(link_id, payment_intent_id)] = replace(
            claim, payment_intent_id=payment_intent_id
        )

    def open_claim(self, link_id: str) -> PaymentLinkClaim | None:
        for claim in self._claims.values():
            if claim.link_id == link_id and not claim.released:
                return claim
        return None

    def find_open_claim_by_intent(self, payment_intent_id: str) -> PaymentLinkClaim | None:
        for claim in self._claims.values():
            if claim.payment_intent_id == payment_intent_id and not claim.released:
                return claim
        return None

    def release_claim(self, link_id: str, payment_intent_id: str) -> None:
        key = (link_id, payment_intent_id)
        claim = self._claims.get(key)
        if claim is not None and not claim.released:
            self._claims[key] = replace(claim, released=True)


def _parse_offset_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        offset = int(cursor, 10)
    except ValueError as exc:
        raise InvalidRequestError("cursor is not valid", code="invalid_cursor") from exc
    if offset < 0:
        raise InvalidRequestError("cursor is not valid", code="invalid_cursor")
    return offset


_PLINK_COLUMNS = """
    link_id, merchant_id, public_code, amount_minor, amount_min_minor,
    amount_max_minor, currency, description, single_use, state,
    payment_intent_id, expires_at, created_at, terminal_at, schema_version
"""


def _link_from_row(row: Sequence[Any]) -> PaymentLinkRecord:
    return PaymentLinkRecord(
        link_id=row[0],
        merchant_id=row[1],
        public_code=row[2],
        amount_minor=row[3],
        amount_min_minor=row[4],
        amount_max_minor=row[5],
        currency=str(row[6]).strip(),
        description=row[7],
        single_use=row[8],
        state=row[9],
        payment_intent_id=row[10],
        expires_at=row[11],
        created_at=row[12],
        terminal_at=row[13],
        schema_version=row[14],
    )


class PostgresPaymentLinkStore:
    """psycopg3 store over migration 0094 (gateway pg style: small txns)."""

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

    def insert(self, record: PaymentLinkRecord) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                INSERT INTO payment_links (
                    link_id, merchant_id, public_code, amount_minor,
                    amount_min_minor, amount_max_minor, currency, description,
                    single_use, state, payment_intent_id, expires_at,
                    created_at, terminal_at, schema_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.link_id,
                    record.merchant_id,
                    record.public_code,
                    record.amount_minor,
                    record.amount_min_minor,
                    record.amount_max_minor,
                    record.currency,
                    record.description,
                    record.single_use,
                    record.state,
                    record.payment_intent_id,
                    record.expires_at,
                    record.created_at,
                    record.terminal_at,
                    record.schema_version,
                ),
            )

        self._run(op)

    def save(self, record: PaymentLinkRecord) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE payment_links SET
                    state = %s, payment_intent_id = %s, terminal_at = %s
                WHERE link_id = %s
                """,
                (record.state, record.payment_intent_id, record.terminal_at, record.link_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"unknown payment link {record.link_id!r}")

        self._run(op)

    def get(self, link_id: str) -> PaymentLinkRecord | None:
        def op(cur: Any) -> PaymentLinkRecord | None:
            cur.execute(
                f"SELECT {_PLINK_COLUMNS} FROM payment_links WHERE link_id = %s", (link_id,)
            )
            row = cur.fetchone()
            return None if row is None else _link_from_row(row)

        return self._run(op)

    def get_by_public_code(self, public_code: str) -> PaymentLinkRecord | None:
        def op(cur: Any) -> PaymentLinkRecord | None:
            cur.execute(
                f"SELECT {_PLINK_COLUMNS} FROM payment_links WHERE public_code = %s",
                (public_code,),
            )
            row = cur.fetchone()
            return None if row is None else _link_from_row(row)

        return self._run(op)

    def list_for_merchant(
        self, merchant_id: str, *, limit: int, cursor: str | None
    ) -> tuple[Sequence[PaymentLinkRecord], str | None]:
        offset = _parse_offset_cursor(cursor)

        def op(cur: Any) -> tuple[Sequence[PaymentLinkRecord], str | None]:
            cur.execute(
                f"""
                SELECT {_PLINK_COLUMNS} FROM payment_links
                WHERE merchant_id = %s
                ORDER BY created_at DESC, link_id
                LIMIT %s OFFSET %s
                """,
                (merchant_id, limit + 1, offset),
            )
            rows = cur.fetchall()
            page = [_link_from_row(row) for row in rows[:limit]]
            next_cursor = str(offset + limit) if len(rows) > limit else None
            return page, next_cursor

        return self._run(op)

    def list_due_active(self, *, now: datetime) -> Sequence[PaymentLinkRecord]:
        now = _require_aware(now, "now")

        def op(cur: Any) -> Sequence[PaymentLinkRecord]:
            cur.execute(
                f"""
                SELECT {_PLINK_COLUMNS} FROM payment_links
                WHERE state = 'ACTIVE' AND expires_at <= %s
                ORDER BY expires_at, link_id
                """,
                (now,),
            )
            return [_link_from_row(row) for row in cur.fetchall()]

        return self._run(op)

    def claim(self, link_id: str, payment_intent_id: str, *, now: datetime) -> None:
        from psycopg import errors as pg_errors

        now = _require_aware(now, "now")

        def op(cur: Any) -> None:
            cur.execute(
                """
                INSERT INTO payment_link_claims (link_id, payment_intent_id, claimed_at)
                VALUES (%s, %s, %s)
                """,
                (link_id, payment_intent_id, now),
            )

        try:
            self._run(op)
        except pg_errors.UniqueViolation as exc:
            existing = self.open_claim(link_id)
            if existing is not None and existing.payment_intent_id == payment_intent_id:
                return  # idempotent retry of the same claim
            raise LinkClaimConflict(link_id) from exc

    def bind_claim_intent(
        self, link_id: str, provisional_id: str, payment_intent_id: str
    ) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE payment_link_claims SET payment_intent_id = %s
                WHERE link_id = %s AND payment_intent_id = %s
                """,
                (payment_intent_id, link_id, provisional_id),
            )

        self._run(op)

    def open_claim(self, link_id: str) -> PaymentLinkClaim | None:
        def op(cur: Any) -> PaymentLinkClaim | None:
            cur.execute(
                """
                SELECT link_id, payment_intent_id, claimed_at, released
                FROM payment_link_claims
                WHERE link_id = %s AND released = FALSE
                """,
                (link_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return PaymentLinkClaim(
                link_id=row[0], payment_intent_id=row[1], claimed_at=row[2], released=row[3]
            )

        return self._run(op)

    def find_open_claim_by_intent(self, payment_intent_id: str) -> PaymentLinkClaim | None:
        def op(cur: Any) -> PaymentLinkClaim | None:
            cur.execute(
                """
                SELECT link_id, payment_intent_id, claimed_at, released
                FROM payment_link_claims
                WHERE payment_intent_id = %s AND released = FALSE
                """,
                (payment_intent_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return PaymentLinkClaim(
                link_id=row[0], payment_intent_id=row[1], claimed_at=row[2], released=row[3]
            )

        return self._run(op)

    def release_claim(self, link_id: str, payment_intent_id: str) -> None:
        def op(cur: Any) -> None:
            cur.execute(
                """
                UPDATE payment_link_claims SET released = TRUE
                WHERE link_id = %s AND payment_intent_id = %s AND released = FALSE
                """,
                (link_id, payment_intent_id),
            )

        self._run(op)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_amount_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(
            f"{field} must be an integer amount in paisa (no floats, spec 00 §6)",
            code="invalid_amount",
        )
    return value


def _description_has_pii(description: str) -> bool:
    """True when the spec/00 PII screen would alter the description."""
    return redact(description) != normalize_bengali_digits(description)


def _parse_expires_at(value: object) -> datetime:
    if isinstance(value, datetime):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidRequestError(
                "expires_at must be an RFC3339 timestamp", code="invalid_expires_at"
            ) from exc
    else:
        raise InvalidRequestError(
            "expires_at must be an RFC3339 timestamp", code="invalid_expires_at"
        )
    if candidate.tzinfo is None or candidate.tzinfo.utcoffset(candidate) is None:
        raise InvalidRequestError(
            "expires_at must be timezone-aware (spec 00 §7)", code="invalid_expires_at"
        )
    return candidate.astimezone(UTC)


def _parse_payer_amount_minor(payer_inputs: Mapping[str, object]) -> int:
    """Exact payer-entered amount: integer paisa, or a decimal BDT string
    (Bengali numerals normalized; never floats)."""
    if "amount_minor" in payer_inputs:
        return _require_amount_int(payer_inputs["amount_minor"], "amount_minor")
    raw = payer_inputs.get("amount")
    if isinstance(raw, float):
        raise InvalidRequestError(
            "amount must be a decimal string or integer paisa (floats rejected, spec 00 §6)",
            code="invalid_amount",
        )
    if isinstance(raw, str):
        try:
            return parse_bdt_amount(raw).amount_minor
        except (AmountParseError, MoneyError) as exc:
            raise InvalidRequestError(
                "amount is not a valid BDT amount", code="invalid_amount"
            ) from exc
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw * 100  # an integer "amount" is whole BDT; exact paisa conversion
    raise InvalidRequestError(
        "amount is required for payer-entered links", code="amount_required"
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PaymentLinkService:
    """Create / fetch / cancel payment links and run the public checkout."""

    def __init__(
        self,
        store: PaymentLinkStore,
        *,
        audit: AuditPort,
        outbox: OutboxPort,
        intents: PaymentIntentPort,
        public_host: str,
        default_ttl_hours: int = DEFAULT_PAYMENT_LINK_TTL_HOURS,
        producer: str = "gateway",
        merchant_display_name: Callable[[str], str | None] | None = None,
    ) -> None:
        if default_ttl_hours < 1:
            raise ValueError("default_ttl_hours must be >= 1")
        if not public_host:
            raise ValueError("public_host must be non-empty")
        self._store = store
        self._audit = audit
        self._outbox = outbox
        self._intents = intents
        self._public_host = public_host
        self._default_ttl = timedelta(hours=default_ttl_hours)
        self._producer = producer
        self._merchant_display_name = merchant_display_name or (lambda _merchant_id: None)

    # -- views ---------------------------------------------------------------

    def _url(self, public_code: str) -> str:
        return f"https://pay.{self._public_host}/l/{public_code}"

    def _view(self, record: PaymentLinkRecord) -> dict:
        return {
            "payment_link_id": record.link_id,
            "merchant_id": record.merchant_id,
            "public_code": record.public_code,
            "url": self._url(record.public_code),
            "amount_minor": record.amount_minor,
            "amount_min_minor": record.amount_min_minor,
            "amount_max_minor": record.amount_max_minor,
            "currency": record.currency,
            "description": record.description,
            "single_use": record.single_use,
            "state": record.state,
            "payment_intent_id": record.payment_intent_id,
            "expires_at": _rfc3339_ms(record.expires_at),
            "created_at": _rfc3339_ms(record.created_at),
            "terminal_at": None if record.terminal_at is None else _rfc3339_ms(record.terminal_at),
        }

    # -- merchant surface ------------------------------------------------------

    def create(
        self, merchant_id: str, payload: Mapping[str, object], idempotency_key: str, clock: Clock
    ) -> dict:
        """``POST /v1/payment-links`` — idempotent on (merchant, idempotency_key)."""
        if not merchant_id:
            raise InvalidRequestError("merchant_id is required", code="invalid_request")
        if not idempotency_key:
            raise InvalidRequestError("Idempotency-Key is required", code="invalid_request")
        link_id = make_gateway_id(
            "plink", {"merchant_id": merchant_id, "idempotency_key": idempotency_key}
        )
        existing = self._store.get(link_id)
        if existing is not None:
            return self._view(existing)  # idempotent replay; no new side effects

        currency = payload.get("currency", "BDT")
        if currency != "BDT":
            raise InvalidRequestError(
                "currency must be BDT (spec 00 §6)", code="unsupported_currency"
            )
        description = payload.get("description")
        if not isinstance(description, str) or not description.strip():
            raise InvalidRequestError("description is required", code="invalid_request")
        if _description_has_pii(description):
            raise InvalidRequestError(
                "description contains PII; reject-not-redact (spec/16 §API C)",
                code="description_contains_pii",
            )

        amount_minor = payload.get("amount_minor")
        amount_min = payload.get("amount_min_minor")
        amount_max = payload.get("amount_max_minor")
        if amount_minor is not None:
            amount_minor = _require_amount_int(amount_minor, "amount_minor")
            if amount_minor <= 0:
                raise InvalidRequestError(
                    "amount_minor must be > 0 (plink_amount_positive)", code="invalid_amount"
                )
            if amount_min is not None or amount_max is not None:
                raise InvalidRequestError(
                    "amount_minor excludes amount_min_minor/amount_max_minor "
                    "(plink_amount_shape)",
                    code="invalid_amount",
                )
        else:
            if amount_min is None or amount_max is None:
                raise InvalidRequestError(
                    "payer-entered links require both amount_min_minor and amount_max_minor "
                    "(plink_amount_shape)",
                    code="invalid_amount",
                )
            amount_min = _require_amount_int(amount_min, "amount_min_minor")
            amount_max = _require_amount_int(amount_max, "amount_max_minor")
            if not 0 < amount_min <= amount_max:
                raise InvalidRequestError(
                    "bounds require 0 < amount_min_minor <= amount_max_minor "
                    "(plink_amount_shape)",
                    code="invalid_amount",
                )

        now = clock.now()
        raw_expires = payload.get("expires_at")
        expires_at = self._default_ttl + now if raw_expires is None else _parse_expires_at(
            raw_expires
        )
        if expires_at <= now:
            raise InvalidRequestError(
                "expires_at must be in the future", code="invalid_expires_at"
            )
        single_use = payload.get("single_use", True)
        if not isinstance(single_use, bool):
            raise InvalidRequestError("single_use must be a boolean", code="invalid_request")

        record = PaymentLinkRecord(
            link_id=link_id,
            merchant_id=merchant_id,
            public_code=make_public_code(link_id, merchant_id, now),
            amount_minor=amount_minor,
            amount_min_minor=None if amount_minor is not None else amount_min,
            amount_max_minor=None if amount_minor is not None else amount_max,
            description=description,
            single_use=single_use,
            expires_at=expires_at,
            created_at=now,
        )
        self._store.insert(record)
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_LINK_CREATED",
                actor_id=merchant_id,
                subject_type=_SUBJECT_TYPE,
                subject_id=link_id,
                from_state=None,
                to_state="ACTIVE",
                payload={"merchant_id": merchant_id, "public_code": record.public_code},
            ),
            clock=clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="payment_link.created",
                subject_type=_SUBJECT_TYPE,
                subject_id=link_id,
                producer=self._producer,
                topic=_TOPIC,
                payload={
                    "link_id": link_id,
                    "merchant_id": merchant_id,
                    "amount_minor": record.amount_minor,
                    "expires_at": _rfc3339_ms(expires_at),
                },
                occurred_at=now,
            )
        )
        return self._view(record)

    def get(self, plink_id: str, *, merchant_id: str) -> dict:
        """``GET /v1/payment-links/{plink_id}`` — merchant-scoped fetch."""
        record = self._store.get(plink_id)
        if record is None or record.merchant_id != merchant_id:
            raise NotFoundError("payment link not found", code="payment_link_not_found")
        return self._view(record)

    def list(self, *, merchant_id: str, limit: int, cursor: str | None) -> dict:
        """``GET /v1/payment-links`` — ``{data, next_cursor}`` page envelope."""
        records, next_cursor = self._store.list_for_merchant(
            merchant_id, limit=limit, cursor=cursor
        )
        return {"data": [self._view(r) for r in records], "next_cursor": next_cursor}

    def cancel(
        self, plink_id: str, actor: str, clock: Clock, *, merchant_id: str | None = None
    ) -> dict:
        """``ACTIVE -> CANCELLED``; refusal-first, 409 while a claim is open."""
        record = self._store.get(plink_id)
        if record is None or (merchant_id is not None and record.merchant_id != merchant_id):
            raise NotFoundError("payment link not found", code="payment_link_not_found")
        if record.state != "ACTIVE":
            raise ConflictError(
                f"payment link is {record.state}; terminal rows are immutable "
                "(refusal-first FSM)",
                code="link_not_active",
            )
        if self._store.open_claim(plink_id) is not None:
            raise ConflictError(
                "a checkout intent is in flight for this link", code="link_payment_in_flight"
            )
        now = clock.now()
        updated = replace(record, state="CANCELLED", terminal_at=now)
        self._store.save(updated)
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_LINK_CANCELLED",
                actor_id=actor,
                subject_type=_SUBJECT_TYPE,
                subject_id=plink_id,
                from_state="ACTIVE",
                to_state="CANCELLED",
                payload={"merchant_id": record.merchant_id},
            ),
            clock=clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="payment_link.cancelled",
                subject_type=_SUBJECT_TYPE,
                subject_id=plink_id,
                producer=self._producer,
                topic=_TOPIC,
                payload={"link_id": plink_id, "merchant_id": record.merchant_id},
                occurred_at=now,
            )
        )
        return self._view(updated)

    # -- public surface --------------------------------------------------------

    def get_by_public_code(self, public_code: str) -> dict:
        """PII-free public view for the hosted checkout page (spec/16 §API C).

        Non-ACTIVE links still return their state so the page can render the
        terminal paid/expired/cancelled view — never an error envelope.
        """
        record = self._store.get_by_public_code(public_code)
        if record is None:
            raise NotFoundError("payment link not found", code="payment_link_not_found")
        return {
            "public_code": record.public_code,
            "state": record.state,
            "merchant_display_name": self._merchant_display_name(record.merchant_id),
            "amount_minor": record.amount_minor,
            "amount_min_minor": record.amount_min_minor,
            "amount_max_minor": record.amount_max_minor,
            "currency": record.currency,
            "description": record.description,
        }

    def create_checkout_intent(
        self, public_code: str, payer_inputs: Mapping[str, object], clock: Clock
    ) -> dict:
        """``POST /v1/public/payment-links/{public_code}/intents``.

        Claims the link first (single_use): a second concurrent attempt hits
        the one-open-claim guard and surfaces ``409 link_not_payable``. The
        intent is created THROUGH the kernel ``PaymentIntentPort`` — links
        never grow a payment lifecycle of their own.
        """
        record = self._store.get_by_public_code(public_code)
        if record is None:
            raise NotFoundError("payment link not found", code="payment_link_not_found")
        now = clock.now()
        if record.state != "ACTIVE" or now >= record.expires_at:
            raise ConflictError("payment link is not payable", code="link_not_payable")

        if record.amount_minor is not None:
            amount_minor = record.amount_minor
        else:
            amount_minor = _parse_payer_amount_minor(payer_inputs)
            if not (
                record.amount_min_minor <= amount_minor <= record.amount_max_minor  # type: ignore[operator]
            ):
                raise InvalidRequestError(
                    "amount is outside the link's allowed bounds", code="amount_out_of_bounds"
                )

        # Server-generated idempotency key: content-addressed over the public
        # code + canonical payer inputs — identical inputs replay, not duplicate.
        server_idem_key = "plinkck_" + sha256_canonical(
            {"public_code": public_code, "payer_inputs": dict(payer_inputs)}
        )[:24]

        if record.single_use:
            try:
                # Claim FIRST under the provisional id (= the deterministic
                # server idempotency key); bound to the real intent id below.
                self._store.claim(record.link_id, server_idem_key, now=now)
            except LinkClaimConflict as exc:
                raise ConflictError(
                    "another checkout already holds this link", code="link_not_payable"
                ) from exc

        payer_metadata = payer_inputs.get("metadata")
        metadata = dict(payer_metadata) if isinstance(payer_metadata, Mapping) else {}
        metadata["payment_link_id"] = record.link_id
        intent_payload = {
            "amount_minor": amount_minor,
            "currency": record.currency,
            "description": record.description,
            "metadata": metadata,
        }
        try:
            intent = self._intents.create_intent(
                intent_payload,
                merchant_id=record.merchant_id,
                idempotency_key=server_idem_key,
                clock=clock,
            )
        except Exception:
            if record.single_use:  # compensate: never strand a claim on failure
                self._store.release_claim(record.link_id, server_idem_key)
            raise
        if record.single_use:
            self._store.bind_claim_intent(
                record.link_id, server_idem_key, str(intent["payment_intent_id"])
            )
        return dict(intent)

    # -- FSM transitions driven by consumed events / the sweep ------------------

    def _expire(self, record: PaymentLinkRecord, clock: Clock) -> None:
        """ACTIVE -> EXPIRED; caller has verified the no-open-claim guard."""
        now = clock.now()
        self._store.save(replace(record, state="EXPIRED", terminal_at=now))
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_LINK_EXPIRED",
                actor_id=self._producer,
                subject_type=_SUBJECT_TYPE,
                subject_id=record.link_id,
                from_state="ACTIVE",
                to_state="EXPIRED",
                payload={"merchant_id": record.merchant_id},
            ),
            clock=clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="payment_link.expired",
                subject_type=_SUBJECT_TYPE,
                subject_id=record.link_id,
                producer=self._producer,
                topic=_TOPIC,
                payload={"link_id": record.link_id, "merchant_id": record.merchant_id},
                occurred_at=now,
            )
        )

    def mark_paid(self, link_id: str, payment_intent_id: str, clock: Clock) -> None:
        """ACTIVE -> PAID on a consumed matching ``payment_intent.succeeded``.

        Guard (spec/16 FSM): ``single_use = TRUE`` only — non-single-use links
        never take the paid transition. Terminal states refuse silently here
        because the consumer is at-least-once (a duplicate-delivery no-op, not
        an API-surface refusal).
        """
        record = self._store.get(link_id)
        if record is None or record.state != "ACTIVE" or not record.single_use:
            return
        now = clock.now()
        self._store.save(
            replace(record, state="PAID", payment_intent_id=payment_intent_id, terminal_at=now)
        )
        self._audit.append(
            AuditEventSpec(
                event_type="PAYMENT_LINK_PAID",
                actor_id=self._producer,
                subject_type=_SUBJECT_TYPE,
                subject_id=link_id,
                from_state="ACTIVE",
                to_state="PAID",
                payload={
                    "merchant_id": record.merchant_id,
                    "payment_intent_id": payment_intent_id,
                },
            ),
            clock=clock,
        )
        self._outbox.enqueue(
            build_event(
                event_type="payment_link.paid",
                subject_type=_SUBJECT_TYPE,
                subject_id=link_id,
                producer=self._producer,
                topic=_TOPIC,
                payload={
                    "link_id": link_id,
                    "merchant_id": record.merchant_id,
                    "payment_intent_id": payment_intent_id,
                    "amount_minor": record.amount_minor,
                },
                occurred_at=now,
            )
        )

    def release_claim_for_intent(self, payment_intent_id: str, clock: Clock) -> None:
        """Terminal intent failure: release the open claim; expire if now due."""
        claim = self._store.find_open_claim_by_intent(payment_intent_id)
        if claim is None:
            return
        self._store.release_claim(claim.link_id, payment_intent_id)
        record = self._store.get(claim.link_id)
        if (
            record is not None
            and record.state == "ACTIVE"
            and clock.now() >= record.expires_at
            and self._store.open_claim(record.link_id) is None
        ):
            self._expire(record, clock)

    def sweep_expiry(self, clock: Clock) -> list[str]:
        """Scheduler sweep: expire due ACTIVE links with no open claim.

        Expiry with an open claim is deferred (spec/16 §Failure modes —
        "link expiry while payer mid-checkout"). Returns expired link ids.
        """
        expired: list[str] = []
        for record in self._store.list_due_active(now=clock.now()):
            if self._store.open_claim(record.link_id) is not None:
                continue  # deferred to the intent's own terminal event
            self._expire(record, clock)
            expired.append(record.link_id)
        return expired


# ---------------------------------------------------------------------------
# Event consumer
# ---------------------------------------------------------------------------


class PaymentLinkEventConsumer:
    """Consumes ``payment_intent.*`` envelopes (spec/16 §Events consumed).

    Idempotent on ``event_id`` (spec/00 §5 consumer rule): a redelivery is a
    recorded no-op. Unknown event types are ignored.
    """

    def __init__(self, service: PaymentLinkService, *, clock: Clock) -> None:
        self._service = service
        self._clock = clock
        self._seen: set[str] = set()
        self.duplicate_count = 0

    def consume(self, event: Mapping[str, object]) -> None:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise InvalidRequestError(
                "envelope is missing a string event_id", code="invalid_event"
            )
        if event_id in self._seen:
            self.duplicate_count += 1
            return
        self._seen.add(event_id)

        event_type = event.get("type")
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        payment_intent_id = payload.get("payment_intent_id")
        if not isinstance(payment_intent_id, str):
            return  # not an intent event we can act on

        if event_type == "payment_intent.succeeded":
            metadata = payload.get("metadata")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            link_id = metadata.get("payment_link_id")
            if isinstance(link_id, str) and link_id:
                self._service.mark_paid(link_id, payment_intent_id, self._clock)
        elif event_type in _RELEASE_EVENT_TYPES:
            self._service.release_claim_for_intent(payment_intent_id, self._clock)
        # anything else: ignored by design
