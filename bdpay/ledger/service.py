"""LedgerService — the one and only writer of ledger records (spec/03).

Ported from bd-pay-fable-pilot/src/bdpay_pilot/ledger/service.py (verified
pilot; 93/93 green), with all ``bdpay_pilot.platform.*`` imports replaced by
the binding ``bdpay.platform.*`` implementations, and extended with the
audit-event write path (spec/03 AuditEvent design, PII-redaction-at-write —
descoped in the pilot) and ``emit_event`` (outbox row outside the
journal-entry bundle, for FSM side-effect events).

post_journal_entry appends ONE journal entry + N postings + ONE ledger_chain
row + ONE outbox row inside a single store transaction: the atomic bundle.
There is no partial-failure window — if any insert fails, everything rolls
back.

verify_chain walks the hash chain and FAILS CLOSED: any gap, hash mismatch,
checkpoint anomaly, untrusted signing key, payload mismatch against the
underlying journal rows (deep mode), count mismatch, or unexpected exception
yields a FAILED_* result and engages the write gate so no further money
moves until an operator clears it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from bdpay.ledger.account_ids import account_id as resolve_account_id
from bdpay.ledger.checkpoint_signer import CheckpointSigner, verify_checkpoint
from bdpay.ledger.errors import (
    LedgerAccountNotFoundError,
    LedgerAmountError,
    LedgerDuplicateError,
    LedgerDuplicateEventError,
    LedgerImbalanceError,
    LedgerValidationError,
    LedgerWriteGateEngagedError,
    TrialBalanceViolationError,
)
from bdpay.ledger.hash_chain import GENESIS_PREV_HASH, compute_entry_hash, to_canonical_ts
from bdpay.ledger.ids_ext import make_ledger_id
from bdpay.ledger.payloads import (
    audit_event_payload,
    audit_payload_pointer_for,
    entry_id_from_pointer,
    journal_entry_payload_hash,
    payload_pointer_for,
)
from bdpay.ledger.types import (
    ACCOUNT_SUBTYPES,
    ACCOUNT_TYPES,
    ACTOR_TYPES,
    AUDIT_EVENT_TYPES,
    CHECKPOINT_INTERVAL,
    ENTRY_TYPES,
    GENESIS_ENTRY_TYPE,
    OWNER_TYPES,
    PRODUCER,
    REFERENCE_TYPES,
    SIDES,
    VERIFY_COMPLETED,
    VERIFY_FAILED_BROKEN_CHAIN,
    VERIFY_FAILED_ERROR,
    VERIFY_FAILED_INVALID_CHECKPOINT_SIG,
    AccountHoldRow,
    AccountRow,
    AccountSpec,
    AuditEventRow,
    AuditEventSpec,
    ChainEntryRow,
    ChainVerificationResult,
    CheckpointRow,
    JournalEntryRow,
    JournalEntrySpec,
    OutboxRow,
    PostingRow,
    PostingSpec,
    TrialBalance,
)
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.ids import make_id
from bdpay.platform.outbox import TOPICS
from bdpay.platform.pii import is_opaque_identifier_key, redact

if TYPE_CHECKING:
    from datetime import datetime

    from bdpay.ledger.store import LedgerStore, LedgerTransaction

MONEY_DOMAIN = "MONEY"
AUDIT_DOMAIN = "AUDIT"
_GENESIS_PAYLOAD_HASH = hashlib.sha256(b"genesis").hexdigest()
_DEFAULT_HOLD_TTL = timedelta(days=7)


def _require_amount_minor(value: object) -> int:
    """Validate an integer-paisa amount; reject bool/float/str/non-positive."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerAmountError(
            f"amount_minor must be int paisa, got {type(value).__name__} (spec/00 §6)"
        )
    if value <= 0:
        raise LedgerAmountError(f"amount_minor must be > 0, got {value}")
    return value


def _redact_payload(value: Any, _key: str | None = None) -> Any:
    """Recursively PII-redact strings in a payload (redaction-at-write), EXCEPT
    structural identifier fields (``*_id`` / ``*_ref`` / ``idempotency_key``).

    Those carry opaque content-addressed ids/refs (spec/00 §4 keeps PII hashed in
    ``kyc_records``, never in identifiers); redacting them would corrupt audit /
    event linkage when an id contains a 10+ digit run matched as an NID/PAN.
    Free-text fields stay redacted."""
    if isinstance(value, str):
        if _key is not None and is_opaque_identifier_key(_key):
            return value
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_payload(item, key) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_payload(item) for item in value]
    return value


class LedgerService:
    """Sole writer of journal_entries / postings / ledger_chain / audit / outbox."""

    def __init__(
        self,
        store: LedgerStore,
        clock: Clock,
        signer: CheckpointSigner,
        *,
        producer: str = PRODUCER,
        checkpoint_interval: int = CHECKPOINT_INTERVAL,
        trusted_public_keys: frozenset[str] | None = None,
    ) -> None:
        if checkpoint_interval <= 0:
            raise LedgerValidationError("checkpoint_interval must be positive")
        self._store = store
        self._clock = clock
        self._signer = signer
        self._producer = producer
        self._interval = checkpoint_interval
        # Fail closed on checkpoint trust (E6): by default only the configured
        # signer's key is trusted.  A stored-in-row key alone proves nothing
        # (whoever re-signs with their own key also stores their own public
        # key); trust must come from outside the row.
        self._trusted_keys = (
            trusted_public_keys
            if trusted_public_keys is not None
            else frozenset({signer.public_key_b64()})
        )

    @property
    def store(self) -> LedgerStore:
        return self._store

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    def create_account(self, spec: AccountSpec, *, conn: Any | None = None) -> str:
        if spec.account_type not in ACCOUNT_TYPES:
            raise LedgerValidationError(f"unknown account_type {spec.account_type!r}")
        if spec.account_subtype not in ACCOUNT_SUBTYPES:
            raise LedgerValidationError(f"unknown account_subtype {spec.account_subtype!r}")
        if spec.owner_type is not None and spec.owner_type not in OWNER_TYPES:
            raise LedgerValidationError(f"unknown owner_type {spec.owner_type!r}")
        if spec.currency != "BDT":
            raise LedgerValidationError("currency must be 'BDT' in v1")
        # Single source of truth for the content-addressed account id (MONEY-01):
        # the kernel + offer engine derive byte-identical ids via this resolver.
        account_id = resolve_account_id(
            account_subtype=spec.account_subtype,
            purpose_tag=spec.purpose_tag,
            owner_id=spec.owner_id,
            currency=spec.currency,
        )
        created_at = self._now()
        with self._store.transaction(conn=conn) as tx:
            if tx.get_account(account_id) is not None:
                raise LedgerValidationError(f"account {account_id} already exists")
            if spec.parent_id is not None and tx.get_account(spec.parent_id) is None:
                raise LedgerAccountNotFoundError(f"parent account {spec.parent_id} not found")
            tx.insert_account(
                AccountRow(
                    account_id=account_id,
                    account_type=spec.account_type,
                    account_subtype=spec.account_subtype,
                    currency=spec.currency,
                    owner_id=spec.owner_id,
                    owner_type=spec.owner_type,
                    parent_id=spec.parent_id,
                    purpose_tag=spec.purpose_tag,
                    is_system=spec.is_system,
                    created_at=created_at,
                )
            )
        return account_id

    # ------------------------------------------------------------------
    # The atomic bundle
    # ------------------------------------------------------------------

    def post_journal_entry(self, spec: JournalEntrySpec, *, conn: Any | None = None) -> str:
        """Append journal entry + postings + chain row + outbox row atomically.

        Raises:
            LedgerWriteGateEngagedError  if the write gate is engaged (fail closed).
            LedgerImbalanceError         if SUM(debit) != SUM(credit).
            LedgerDuplicateError         if idempotency_key already exists.
            LedgerAccountNotFoundError   if any posting account is absent/closed.
            LedgerAmountError            if any posting amount is not a positive int.
            LedgerValidationError        for any other spec-shape violation.
        """
        self._validate_entry_spec(spec)
        produced_at = self._now()
        with self._store.transaction(conn=conn) as tx:
            entry_id = self._append_journal_entry(tx, spec, produced_at=produced_at)
        return entry_id

    def open_hold(
        self,
        account_id: str,
        amount_minor: int,
        hold_reason: str,
        reference_id: str,
        *,
        source_account_id: str | None = None,
        hold_reserve_account_id: str | None = None,
        hold_expires_at: datetime | None = None,
        conn: Any | None = None,
    ) -> str:
        amount = _require_amount_minor(amount_minor)
        if not account_id or not reference_id or not hold_reason:
            raise LedgerValidationError("account_id, reference_id and hold_reason are required")
        source_id = source_account_id or account_id
        reserve_id = hold_reserve_account_id or account_id
        opened_at = self._now()
        expires_at = hold_expires_at or (opened_at + _DEFAULT_HOLD_TTL)
        hold_id = make_ledger_id(
            "hold",
            {
                "account_id": account_id,
                "reference_id": reference_id,
                "amount_minor": amount,
                "hold_opened_at": to_canonical_ts(opened_at),
            },
        )
        with self._store.transaction(conn=conn) as tx:
            lock_reference = getattr(tx, "lock_account_hold_reference", None)
            if lock_reference is not None:
                lock_reference(reference_id)
            existing = tx.get_open_account_hold_by_reference(reference_id)
            if existing is not None:
                return existing.hold_id
            for label, candidate in (
                ("account", account_id),
                ("source account", source_id),
                ("hold reserve account", reserve_id),
            ):
                account = tx.get_account(candidate)
                if account is None or account.closed_at is not None:
                    raise LedgerAccountNotFoundError(f"{label} {candidate} is absent or closed")
            open_je_id = self._append_journal_entry(
                tx,
                JournalEntrySpec(
                    reference_id=reference_id,
                    reference_type="HOLD",
                    entry_type="hold_opened",
                    description=f"hold opened: {hold_reason}",
                    produced_by=self._producer,
                    idempotency_key=make_id(
                        "je",
                        {
                            "hold_id": hold_id,
                            "reference_id": reference_id,
                            "entry_type": "hold_opened",
                        },
                    ),
                    postings=(
                        PostingSpec(source_id, "DEBIT", amount),
                        PostingSpec(reserve_id, "CREDIT", amount),
                    ),
                ),
                produced_at=opened_at,
            )
            tx.insert_account_hold(
                AccountHoldRow(
                    hold_id=hold_id,
                    account_id=account_id,
                    hold_reserve_account_id=reserve_id,
                    source_account_id=source_id,
                    reference_id=reference_id,
                    amount_minor=amount,
                    hold_reason=hold_reason,
                    status="OPEN",
                    hold_opened_at=opened_at,
                    hold_expires_at=expires_at,
                    hold_closed_at=None,
                    open_je_id=open_je_id,
                    close_je_id=None,
                )
            )
        return hold_id

    def release_hold(
        self,
        hold_id: str,
        outcome: str,
        *,
        amount_minor: int | None = None,
        conn: Any | None = None,
    ) -> None:
        if outcome not in ("CAPTURED", "VOIDED", "EXPIRED"):
            raise LedgerValidationError("hold outcome must be one of CAPTURED, VOIDED or EXPIRED")
        closed_at = self._now()
        with self._store.transaction(conn=conn) as tx:
            hold = tx.get_account_hold(hold_id, lock=True)
            if hold is None:
                raise LedgerValidationError(f"unknown hold {hold_id}")
            if hold.status != "OPEN":
                if hold.status == outcome:
                    return
                raise LedgerValidationError(
                    f"hold {hold_id} is {hold.status}, cannot release as {outcome}"
                )
            capture_amount = (
                hold.amount_minor if amount_minor is None else _require_amount_minor(amount_minor)
            )
            if capture_amount > hold.amount_minor:
                raise LedgerAmountError("hold release amount must be <= the open hold amount")
            entry_type = {
                "CAPTURED": "hold_captured",
                "VOIDED": "hold_voided",
                "EXPIRED": "hold_expired",
            }[outcome]
            postings: list[PostingSpec]
            if outcome == "CAPTURED":
                unused_minor = hold.amount_minor - capture_amount
                postings = [
                    PostingSpec(
                        hold.hold_reserve_account_id,
                        "DEBIT",
                        hold.amount_minor,
                    ),
                    PostingSpec(hold.account_id, "CREDIT", capture_amount),
                ]
                if unused_minor > 0:
                    postings.append(PostingSpec(hold.source_account_id, "CREDIT", unused_minor))
            else:
                if amount_minor is not None and capture_amount != hold.amount_minor:
                    raise LedgerAmountError("void/expiry must release the full hold amount")
                postings = [
                    PostingSpec(
                        hold.hold_reserve_account_id,
                        "DEBIT",
                        hold.amount_minor,
                    ),
                    PostingSpec(hold.source_account_id, "CREDIT", hold.amount_minor),
                ]
            close_je_id = self._append_journal_entry(
                tx,
                JournalEntrySpec(
                    reference_id=hold.reference_id,
                    reference_type="HOLD",
                    entry_type=entry_type,
                    description=f"hold {outcome.lower()}",
                    produced_by=self._producer,
                    idempotency_key=make_id(
                        "je",
                        {
                            "hold_id": hold.hold_id,
                            "entry_type": entry_type,
                            "amount_minor": capture_amount,
                        },
                    ),
                    postings=tuple(postings),
                ),
                produced_at=closed_at,
            )
            import dataclasses

            tx.update_account_hold(
                dataclasses.replace(
                    hold,
                    status=outcome,
                    hold_closed_at=closed_at,
                    close_je_id=close_je_id,
                )
            )

    def get_open_hold_id(self, reference_id: str, *, conn: Any | None = None) -> str | None:
        if conn is not None:
            with self._store.transaction(conn=conn) as tx:
                hold = tx.get_open_account_hold_by_reference(reference_id)
        else:
            hold = self._store.get_open_account_hold_by_reference(reference_id)
        return None if hold is None else hold.hold_id

    def _append_journal_entry(
        self,
        tx: LedgerTransaction,
        spec: JournalEntrySpec,
        *,
        produced_at: datetime,
    ) -> str:
        self._validate_entry_spec(spec)
        entry_id = make_id(
            "je",
            {
                "reference_id": spec.reference_id,
                "entry_type": spec.entry_type,
                "idempotency_key": spec.idempotency_key,
            },
        )

        gate = tx.write_gate_state()
        if gate.engaged:
            raise LedgerWriteGateEngagedError(
                f"write gate engaged ({gate.reason}); money movement is halted"
            )

        existing = tx.find_entry_id_by_idempotency_key(spec.idempotency_key)
        if existing is not None:
            raise LedgerDuplicateError(spec.idempotency_key, existing)

        for account_id in {p.account_id for p in spec.postings}:
            account = tx.get_account(account_id)
            if account is None or account.closed_at is not None:
                raise LedgerAccountNotFoundError(
                    f"posting account {account_id} is absent or closed"
                )

        entry_index = tx.insert_journal_entry(
            JournalEntryRow(
                entry_id=entry_id,
                entry_index=0,
                reference_id=spec.reference_id,
                reference_type=spec.reference_type,
                entry_type=spec.entry_type,
                description=spec.description,
                produced_by=spec.produced_by,
                produced_at=produced_at,
                idempotency_key=spec.idempotency_key,
            )
        )
        entry_row = JournalEntryRow(
            entry_id=entry_id,
            entry_index=entry_index,
            reference_id=spec.reference_id,
            reference_type=spec.reference_type,
            entry_type=spec.entry_type,
            description=spec.description,
            produced_by=spec.produced_by,
            produced_at=produced_at,
            idempotency_key=spec.idempotency_key,
        )

        posting_rows: list[PostingRow] = []
        for ordinal, p in enumerate(spec.postings):
            posting_id = make_id(
                "post",
                {
                    "entry_id": entry_id,
                    "account_id": p.account_id,
                    "side": p.side,
                    "amount_minor": p.amount_minor,
                    "ordinal": ordinal,
                },
            )
            row = PostingRow(
                posting_id=posting_id,
                entry_id=entry_id,
                account_id=p.account_id,
                side=p.side,
                amount_minor=p.amount_minor,
                currency=p.currency,
                produced_at=produced_at,
            )
            tx.insert_posting(row)
            posting_rows.append(row)

        payload_hash = journal_entry_payload_hash(entry_row, posting_rows)
        payload_pointer = payload_pointer_for(entry_id, produced_at)
        amount_minor_sum = sum(p.amount_minor for p in posting_rows)
        self._append_chain_entry(
            tx,
            chain_domain=MONEY_DOMAIN,
            entry_type=spec.entry_type,
            payload_hash=payload_hash,
            payload_pointer=payload_pointer,
            amount_minor_sum=amount_minor_sum,
            produced_at=produced_at,
        )

        event_id = make_id("obx", {"type": "journal_entry.posted", "subject_id": entry_id})
        envelope = {
            "event_id": event_id,
            "type": "journal_entry.posted",
            "occurred_at": to_canonical_ts(produced_at),
            "subject_type": "JournalEntry",
            "subject_id": entry_id,
            "schema_version": 1,
            "producer": self._producer,
            "payload": {
                "entry_id": entry_id,
                "entry_type": spec.entry_type,
                "reference_id": spec.reference_id,
                "reference_type": spec.reference_type,
                "amount_minor_sum": amount_minor_sum,
            },
        }
        tx.insert_outbox(
            OutboxRow(
                event_id=event_id,
                topic="payment.events",
                event_type="journal_entry.posted",
                subject_type="JournalEntry",
                subject_id=entry_id,
                payload_json=canonical_json(envelope).decode("utf-8"),
                producer=self._producer,
                occurred_at=produced_at,
            )
        )
        return entry_id

    # ------------------------------------------------------------------
    # Audit events — PII-redaction-at-write, AUDIT-domain chained (spec/03)
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_audit_event_identity(
        existing: AuditEventRow,
        spec: AuditEventSpec,
        actor_id: str,
        occurred_at: datetime,
    ) -> None:
        """Ensure an existing audit row represents the same event we want to write.

        The deterministic event id covers most identity fields, but fields such
        as ``actor_type`` are not part of the id.  A mismatch means the caller
        is asking for a different audit event that happens to collide; fail
        closed rather than silently returning the stored row.
        """
        if existing.event_type != spec.event_type:
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different event_type"
            )
        if existing.actor_id != actor_id:
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different actor_id"
            )
        if existing.actor_type != spec.actor_type:
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different actor_type"
            )
        if existing.subject_type != spec.subject_type:
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different subject_type"
            )
        if existing.subject_id != spec.subject_id:
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different subject_id"
            )
        if (existing.from_state or "") != (spec.from_state or ""):
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different from_state"
            )
        if (existing.to_state or "") != (spec.to_state or ""):
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different to_state"
            )
        if to_canonical_ts(existing.occurred_at) != to_canonical_ts(occurred_at):
            raise LedgerValidationError(
                f"audit event {existing.event_id!r} already exists with a different occurred_at"
            )

    def write_audit_event(
        self,
        spec: AuditEventSpec,
        *,
        conn: Any | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        """Append one PII-redacted audit_events row + one AUDIT chain row.

        ``write_audit_event`` is the SOLE entry point to audit_events (single
        write path); there is no code path that bypasses PII redaction.
        Returns the created ``aud_<...>`` event id.

        ``occurred_at`` may be supplied to replay an event at its original
        timestamp (e.g. crash-window reconciliation); when omitted the current
        clock time is used.
        """
        if spec.event_type not in AUDIT_EVENT_TYPES:
            raise LedgerValidationError(f"unknown audit event_type {spec.event_type!r}")
        if spec.actor_type not in ACTOR_TYPES:
            raise LedgerValidationError(f"unknown actor_type {spec.actor_type!r}")
        for name in ("actor_id", "subject_type", "subject_id"):
            if not getattr(spec, name):
                raise LedgerValidationError(f"{name} is required")
        if not isinstance(spec.payload, dict):
            raise LedgerValidationError("audit payload must be a dict")

        occurred_at = occurred_at or self._now()
        actor_id = redact(spec.actor_id)
        redacted_payload = _redact_payload(spec.payload)
        payload_hash = sha256_canonical(redacted_payload)
        event_id = make_id(
            "aud",
            {
                "actor_id": actor_id,
                "subject_type": spec.subject_type,
                "subject_id": spec.subject_id,
                "event_type": spec.event_type,
                "from_state": spec.from_state or "",
                "to_state": spec.to_state or "",
                "occurred_at": to_canonical_ts(occurred_at),
            },
        )
        existing_audit = self._store.get_audit_event(event_id)
        if existing_audit is not None:
            self._assert_audit_event_identity(existing_audit, spec, actor_id, occurred_at)
            if existing_audit.payload_hash != payload_hash:
                raise LedgerValidationError(
                    f"audit event {event_id!r} already exists with a different payload"
                )
            return event_id
        payload_pointer = audit_payload_pointer_for(event_id, occurred_at)

        try:
            with self._store.transaction(conn=conn) as tx:
                row = AuditEventRow(
                    event_id=event_id,
                    entry_index=0,  # store-assigned; the input value is ignored on insert
                    event_type=spec.event_type,
                    actor_id=actor_id,
                    actor_type=spec.actor_type,
                    subject_type=spec.subject_type,
                    subject_id=spec.subject_id,
                    from_state=spec.from_state,
                    to_state=spec.to_state,
                    payload_hash=payload_hash,
                    payload_pointer=payload_pointer,
                    payload_json=canonical_json(redacted_payload).decode("utf-8"),
                    prev_chain_hash="",  # sealed below once the chain tip is locked
                    chain_hash="",
                    pii_redacted=True,
                    occurred_at=occurred_at,
                )
                with tx.savepoint():
                    envelope_hash = sha256_canonical(audit_event_payload(row))
                    _index, chain_hash, prev_hash = self._append_chain_entry(
                        tx,
                        chain_domain=AUDIT_DOMAIN,
                        entry_type=spec.event_type,
                        payload_hash=envelope_hash,
                        payload_pointer=payload_pointer,
                        amount_minor_sum=0,
                        produced_at=occurred_at,
                    )
                    import dataclasses

                    tx.insert_audit_event(
                        dataclasses.replace(
                            row, prev_chain_hash=prev_hash, chain_hash=chain_hash
                        )
                    )
        except LedgerDuplicateEventError as _exc:
            existing_audit = self._store.get_audit_event(event_id)
            if existing_audit is None:
                raise LedgerValidationError(
                    f"audit event {event_id!r} already exists with a different payload"
                ) from _exc
            self._assert_audit_event_identity(existing_audit, spec, actor_id, occurred_at)
            if existing_audit.payload_hash != payload_hash:
                raise LedgerValidationError(
                    f"audit event {event_id!r} already exists with a different payload"
                ) from _exc
            return event_id
        return event_id

    def audit_event_exists(
        self, spec: AuditEventSpec, *, occurred_at: datetime | None = None
    ) -> bool:
        """Return True if an identical audit event (same id + payload hash) exists."""
        occurred_at = occurred_at or self._now()
        actor_id = redact(spec.actor_id)
        redacted_payload = _redact_payload(spec.payload)
        payload_hash = sha256_canonical(redacted_payload)
        event_id = make_id(
            "aud",
            {
                "actor_id": actor_id,
                "subject_type": spec.subject_type,
                "subject_id": spec.subject_id,
                "event_type": spec.event_type,
                "from_state": spec.from_state or "",
                "to_state": spec.to_state or "",
                "occurred_at": to_canonical_ts(occurred_at),
            },
        )
        existing = self._store.get_audit_event(event_id)
        return existing is not None and existing.payload_hash == payload_hash

    # ------------------------------------------------------------------
    # Outbox events outside the journal bundle (FSM side-effect events)
    # ------------------------------------------------------------------

    def emit_event(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict,
        topic: str,
        producer: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        """Insert one spec/00 §5 outbox row (idempotent on the derived id).

        ``occurred_at`` may be supplied to replay an event at its original
        timestamp (e.g. crash-window reconciliation); when omitted the current
        clock time is used.
        """
        if topic not in TOPICS:
            raise LedgerValidationError(
                f"unknown topic {topic!r}; topics are a closed registry (spec 00 §5)"
            )
        if not event_type or "." not in event_type:
            raise LedgerValidationError(
                f"event_type must be '<entity>.<past_tense_verb>', got {event_type!r}"
            )
        occurred_at = occurred_at or self._now()
        producer = producer or self._producer
        redacted_payload = _redact_payload(payload)
        event_id = make_id(
            "obx",
            {
                "type": event_type,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "occurred_at": to_canonical_ts(occurred_at),
                "payload_hash": sha256_canonical(redacted_payload),
            },
        )
        envelope = {
            "event_id": event_id,
            "type": event_type,
            "occurred_at": to_canonical_ts(occurred_at),
            "subject_type": subject_type,
            "subject_id": subject_id,
            "schema_version": 1,
            "producer": producer,
            "payload": redacted_payload,
        }
        envelope_json = canonical_json(envelope).decode("utf-8")
        existing_outbox = self._store.get_outbox_event(event_id)
        if existing_outbox is not None:
            if existing_outbox.payload_json != envelope_json:
                raise LedgerValidationError(
                    f"outbox event {event_id!r} already exists with a different payload"
                )
            if existing_outbox.topic != topic:
                raise LedgerValidationError(
                    f"outbox event {event_id!r} already exists with a different topic"
                )
            return event_id
        try:
            with self._store.transaction() as tx:
                tx.insert_outbox(
                    OutboxRow(
                        event_id=event_id,
                        topic=topic,
                        event_type=event_type,
                        subject_type=subject_type,
                        subject_id=subject_id,
                        payload_json=envelope_json,
                        producer=producer,
                        occurred_at=occurred_at,
                    )
                )
        except LedgerDuplicateEventError as _exc:
            existing_outbox = self._store.get_outbox_event(event_id)
            if existing_outbox is None or existing_outbox.payload_json != envelope_json:
                raise LedgerValidationError(
                    f"outbox event {event_id!r} already exists with a different payload"
                ) from _exc
            if existing_outbox.topic != topic:
                raise LedgerValidationError(
                    f"outbox event {event_id!r} already exists with a different topic"
                ) from _exc
            return event_id
        return event_id

    def outbox_event_exists(
        self,
        *,
        event_type: str,
        subject_type: str,
        subject_id: str,
        payload: dict,
        occurred_at: datetime,
    ) -> bool:
        """Return True if the deterministic outbox event already exists.

        The derived event id includes the payload hash, so existence implies
        the payload is identical.
        """
        redacted_payload = _redact_payload(payload)
        event_id = make_id(
            "obx",
            {
                "type": event_type,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "occurred_at": to_canonical_ts(occurred_at),
                "payload_hash": sha256_canonical(redacted_payload),
            },
        )
        return self._store.get_outbox_event(event_id) is not None

    # ------------------------------------------------------------------
    # Chain mechanics
    # ------------------------------------------------------------------

    def initialize_chain(self, chain_domain: str = MONEY_DOMAIN) -> None:
        """Idempotently create the genesis row for a chain domain."""
        produced_at = self._now()
        with self._store.transaction() as tx:
            if tx.lock_chain_tip(chain_domain) is None:
                self._insert_genesis(tx, chain_domain, produced_at)

    def _insert_genesis(
        self, tx: LedgerTransaction, chain_domain: str, produced_at: datetime
    ) -> ChainEntryRow:
        payload_pointer = f"genesis://bdpay/{chain_domain}"
        chain_hash = compute_entry_hash(
            prev_hash=GENESIS_PREV_HASH,
            chain_index=0,
            chain_domain=chain_domain,
            entry_type=GENESIS_ENTRY_TYPE,
            payload_hash=_GENESIS_PAYLOAD_HASH,
            payload_pointer=payload_pointer,
            amount_minor_sum=0,
            producer=self._producer,
            produced_at=to_canonical_ts(produced_at),
        )
        row = ChainEntryRow(
            chain_domain=chain_domain,
            chain_index=0,
            entry_type=GENESIS_ENTRY_TYPE,
            payload_hash=_GENESIS_PAYLOAD_HASH,
            payload_pointer=payload_pointer,
            prev_chain_hash=GENESIS_PREV_HASH,
            amount_minor_sum=0,
            chain_hash=chain_hash,
            is_checkpoint=False,
            checkpoint_sequence_in_domain=None,
            checkpoint_sig=None,
            checkpoint_public_key_b64=None,
            producer=self._producer,
            produced_at=produced_at,
        )
        tx.insert_chain_entry(row)
        return row

    def _append_chain_entry(
        self,
        tx: LedgerTransaction,
        *,
        chain_domain: str,
        entry_type: str,
        payload_hash: str,
        payload_pointer: str,
        amount_minor_sum: int,
        produced_at: datetime,
    ) -> tuple[int, str, str]:
        tip = tx.lock_chain_tip(chain_domain)
        if tip is None:
            genesis = self._insert_genesis(tx, chain_domain, produced_at)
            prev_index, prev_hash = genesis.chain_index, genesis.chain_hash
        else:
            prev_index, prev_hash = tip.chain_index, tip.chain_hash

        next_index = prev_index + 1
        chain_hash = compute_entry_hash(
            prev_hash=prev_hash,
            chain_index=next_index,
            chain_domain=chain_domain,
            entry_type=entry_type,
            payload_hash=payload_hash,
            payload_pointer=payload_pointer,
            amount_minor_sum=amount_minor_sum,
            producer=self._producer,
            produced_at=to_canonical_ts(produced_at),
        )

        is_checkpoint = next_index % self._interval == 0
        checkpoint_seq: int | None = None
        sig: str | None = None
        pub: str | None = None
        if is_checkpoint:
            checkpoint_seq = next_index // self._interval
            sig, pub = self._signer.sign(chain_hash)

        tx.insert_chain_entry(
            ChainEntryRow(
                chain_domain=chain_domain,
                chain_index=next_index,
                entry_type=entry_type,
                payload_hash=payload_hash,
                payload_pointer=payload_pointer,
                prev_chain_hash=prev_hash,
                amount_minor_sum=amount_minor_sum,
                chain_hash=chain_hash,
                is_checkpoint=is_checkpoint,
                checkpoint_sequence_in_domain=checkpoint_seq,
                checkpoint_sig=sig,
                checkpoint_public_key_b64=pub,
                producer=self._producer,
                produced_at=produced_at,
            )
        )
        if is_checkpoint:
            assert checkpoint_seq is not None and sig is not None and pub is not None
            tx.insert_checkpoint(
                CheckpointRow(
                    checkpoint_id=make_ledger_id(
                        "lckp",
                        {
                            "chain_domain": chain_domain,
                            "checkpoint_sequence_in_domain": checkpoint_seq,
                            "chain_hash": chain_hash,
                        },
                    ),
                    chain_domain=chain_domain,
                    checkpoint_sequence_in_domain=checkpoint_seq,
                    chain_index_from=next_index - self._interval + 1,
                    chain_index_to=next_index,
                    chain_hash_at_checkpoint=chain_hash,
                    checkpoint_sig=sig,
                    checkpoint_public_key_b64=pub,
                    entries_in_segment=self._interval,
                    signed_at=produced_at,
                )
            )
        return next_index, chain_hash, prev_hash

    # ------------------------------------------------------------------
    # Balances / trial balance
    # ------------------------------------------------------------------

    def get_balance(
        self,
        account_id: str,
        *,
        as_of: datetime | None = None,
        conn: Any | None = None,
        lock: bool = False,
    ) -> int:
        if conn is not None and as_of is None:
            with self._store.transaction(conn) as tx:
                if tx.get_account(account_id) is None:
                    raise LedgerAccountNotFoundError(f"account {account_id} not found")
                return tx.get_balance(account_id, lock=lock)
        if self._store.get_account(account_id) is None:
            raise LedgerAccountNotFoundError(f"account {account_id} not found")
        if as_of is None:
            return self._store.get_balance(account_id)
        return self._store.get_balance_as_of(account_id, as_of)

    def trial_balance(self) -> TrialBalance:
        return self._store.trial_balance()

    def list_journal_entries_by_reference_and_type(
        self, reference_id: str, entry_type: str
    ) -> list[JournalEntryRow]:
        """Return all JEs for reference_id with the given entry_type, ordered by entry_index."""
        return self._store.list_journal_entries_by_reference_and_type(reference_id, entry_type)

    def list_journal_entries(self, *, limit: int = 20, offset: int = 0) -> list[JournalEntryRow]:
        """Return committed journal entries for read-only operator surfaces."""
        return self._store.list_journal_entries(limit=limit, offset=offset)

    def get_postings_for_entry(self, entry_id: str) -> list[PostingRow]:
        """Return all postings for a given journal entry_id."""
        return self._store.get_postings_for_entry(entry_id)

    def trial_balance_assertion(self) -> None:
        """Assert SUM(DEBIT) == SUM(CREDIT) over ALL postings; fail closed."""
        tb = self._store.trial_balance()
        if not tb.balanced:
            self._store.engage_write_gate(
                reason=(
                    f"trial balance violated: debit={tb.total_debit_minor} "
                    f"credit={tb.total_credit_minor}"
                ),
                engaged_at=self._now(),
            )
            raise TrialBalanceViolationError(tb.total_debit_minor, tb.total_credit_minor)

    # ------------------------------------------------------------------
    # verify_chain — fail closed on every tamper surface
    # ------------------------------------------------------------------

    def verify_chain(
        self,
        *,
        chain_domain: str = MONEY_DOMAIN,
        from_index: int = 0,
        to_index: int | None = None,
        deep: bool = True,
    ) -> ChainVerificationResult:
        try:
            result = self._verify_chain_inner(
                chain_domain=chain_domain, from_index=from_index, to_index=to_index, deep=deep
            )
        except Exception as exc:  # fail closed: unexpected error is NOT a pass
            result = ChainVerificationResult(
                status=VERIFY_FAILED_ERROR, detail=f"{type(exc).__name__}: {exc}"
            )
        if result.status in (
            VERIFY_FAILED_BROKEN_CHAIN,
            VERIFY_FAILED_INVALID_CHECKPOINT_SIG,
        ):
            self._store.engage_write_gate(
                reason=(
                    f"CHAIN_TAMPER_DETECTED domain={chain_domain} "
                    f"first_broken_index={result.first_broken_index} detail={result.detail}"
                ),
                engaged_at=self._now(),
            )
        return result

    def verify_chain_status(
        self,
        *,
        chain_domain: str = MONEY_DOMAIN,
        from_index: int = 0,
        to_index: int | None = None,
        deep: bool = True,
    ) -> ChainVerificationResult:
        """Verify chain state without engaging the write gate on failure.

        ``verify_chain`` is the operational fail-closed path and mutates the
        write gate when tamper is detected. Read-only console endpoints need the
        same integrity calculation without side effects.
        """
        try:
            return self._verify_chain_inner(
                chain_domain=chain_domain,
                from_index=from_index,
                to_index=to_index,
                deep=deep,
            )
        except Exception as exc:  # fail closed in the status without mutating state
            return ChainVerificationResult(
                status=VERIFY_FAILED_ERROR, detail=f"{type(exc).__name__}: {exc}"
            )

    def _verify_chain_inner(
        self, *, chain_domain: str, from_index: int, to_index: int | None, deep: bool
    ) -> ChainVerificationResult:
        store = self._store
        rows = store.iter_chain(chain_domain, from_index, to_index)

        def broken(
            index: int | None, detail: str, *, checked: int = 0, cps: int = 0
        ) -> ChainVerificationResult:
            return ChainVerificationResult(
                status=VERIFY_FAILED_BROKEN_CHAIN,
                entries_checked=checked,
                first_broken_index=index,
                checkpoints_verified=cps,
                detail=detail,
            )

        def bad_sig(index: int, detail: str, *, checked: int, cps: int) -> ChainVerificationResult:
            return ChainVerificationResult(
                status=VERIFY_FAILED_INVALID_CHECKPOINT_SIG,
                entries_checked=checked,
                first_broken_index=index,
                checkpoints_verified=cps,
                detail=detail,
            )

        # Anchor the walk.
        if from_index == 0:
            prev_hash = GENESIS_PREV_HASH
        else:
            anchor = store.get_chain_entry(chain_domain, from_index - 1)
            if anchor is None:
                return broken(from_index - 1, "anchor row before from_index is missing")
            prev_hash = anchor.chain_hash

        if not rows:
            checkpoints = store.list_checkpoints(chain_domain)
            if checkpoints and from_index == 0:
                return broken(
                    checkpoints[0].chain_index_to,
                    "signed checkpoints exist but the chain is empty (chain truncated)",
                )
            if to_index is not None:
                return broken(from_index, f"requested range [{from_index},{to_index}] is empty")
            if deep and chain_domain == MONEY_DOMAIN and from_index == 0:
                journal_count = store.count_journal_entries()
                if journal_count > 0:
                    return broken(
                        0, f"{journal_count} journal entries exist but the chain is empty"
                    )
            return ChainVerificationResult(status=VERIFY_COMPLETED, entries_checked=0)

        checked = 0
        cps_verified = 0
        expected_index = from_index
        for row in rows:
            if row.chain_index != expected_index:
                return broken(
                    row.chain_index,
                    f"chain gap: expected index {expected_index}, found {row.chain_index} "
                    "(row deleted or reordered)",
                    checked=checked,
                    cps=cps_verified,
                )
            if row.prev_chain_hash != prev_hash:
                return broken(
                    row.chain_index,
                    "prev_chain_hash does not match the preceding row's chain_hash",
                    checked=checked,
                    cps=cps_verified,
                )
            expected_hash = compute_entry_hash(
                prev_hash=prev_hash,
                chain_index=row.chain_index,
                chain_domain=row.chain_domain,
                entry_type=row.entry_type,
                payload_hash=row.payload_hash,
                payload_pointer=row.payload_pointer,
                amount_minor_sum=row.amount_minor_sum,
                producer=row.producer,
                produced_at=to_canonical_ts(row.produced_at),
            )
            if expected_hash != row.chain_hash:
                return broken(
                    row.chain_index,
                    "recomputed chain_hash does not match the stored chain_hash",
                    checked=checked,
                    cps=cps_verified,
                )

            should_be_checkpoint = row.chain_index > 0 and row.chain_index % self._interval == 0
            if should_be_checkpoint != row.is_checkpoint:
                return broken(
                    row.chain_index,
                    "checkpoint placement violated (is_checkpoint flag tampered)",
                    checked=checked,
                    cps=cps_verified,
                )
            if row.is_checkpoint:
                if row.checkpoint_sig is None or row.checkpoint_public_key_b64 is None:
                    return bad_sig(
                        row.chain_index,
                        "checkpoint row is missing signature or public key",
                        checked=checked,
                        cps=cps_verified,
                    )
                if row.checkpoint_public_key_b64 not in self._trusted_keys:
                    return bad_sig(
                        row.chain_index,
                        "checkpoint signed with an UNTRUSTED public key (forged checkpoint)",
                        checked=checked,
                        cps=cps_verified,
                    )
                if not verify_checkpoint(
                    row.chain_hash, row.checkpoint_sig, row.checkpoint_public_key_b64
                ):
                    return bad_sig(
                        row.chain_index,
                        "Ed25519 checkpoint signature verification failed",
                        checked=checked,
                        cps=cps_verified,
                    )
                if row.checkpoint_sequence_in_domain != row.chain_index // self._interval:
                    return broken(
                        row.chain_index,
                        "checkpoint_sequence_in_domain inconsistent with chain_index",
                        checked=checked,
                        cps=cps_verified,
                    )
                cps_verified += 1

            if deep and chain_domain == MONEY_DOMAIN and row.chain_index > 0:
                detail = self._deep_verify_money_row(row)
                if detail is not None:
                    return broken(row.chain_index, detail, checked=checked, cps=cps_verified)

            prev_hash = row.chain_hash
            expected_index += 1
            checked += 1

        # Cross-check the signed-checkpoint summary table: detects tail
        # truncation at or below the last signed checkpoint and forged
        # checkpoint summary rows.
        max_index = store.max_chain_index(chain_domain)
        for cp in store.list_checkpoints(chain_domain):
            if max_index is None or cp.chain_index_to > max_index:
                return broken(
                    cp.chain_index_to,
                    "ledger_checkpoints references a chain index beyond the tip "
                    "(chain truncated below a signed checkpoint)",
                    checked=checked,
                    cps=cps_verified,
                )
            chain_row = store.get_chain_entry(chain_domain, cp.chain_index_to)
            if chain_row is None or not chain_row.is_checkpoint:
                return broken(
                    cp.chain_index_to,
                    "checkpoint summary row has no matching checkpoint chain row",
                    checked=checked,
                    cps=cps_verified,
                )
            if (
                chain_row.chain_hash != cp.chain_hash_at_checkpoint
                or chain_row.checkpoint_sig != cp.checkpoint_sig
            ):
                return broken(
                    cp.chain_index_to,
                    "checkpoint summary row disagrees with the chain row (forged checkpoint)",
                    checked=checked,
                    cps=cps_verified,
                )
            if cp.checkpoint_public_key_b64 not in self._trusted_keys:
                return bad_sig(
                    cp.chain_index_to,
                    "checkpoint summary signed with an UNTRUSTED public key",
                    checked=checked,
                    cps=cps_verified,
                )
            if not verify_checkpoint(
                cp.chain_hash_at_checkpoint, cp.checkpoint_sig, cp.checkpoint_public_key_b64
            ):
                return bad_sig(
                    cp.chain_index_to,
                    "checkpoint summary Ed25519 signature verification failed",
                    checked=checked,
                    cps=cps_verified,
                )

        # Full-domain deep pass: every journal entry must have exactly one
        # chain row (detects orphan journal inserts that bypass the chain).
        if deep and chain_domain == MONEY_DOMAIN and from_index == 0 and to_index is None:
            expected_entries = checked - 1  # minus genesis
            journal_count = store.count_journal_entries()
            if journal_count != expected_entries:
                return broken(
                    None,
                    f"journal/chain count mismatch: {journal_count} journal entries vs "
                    f"{expected_entries} chain entries",
                    checked=checked,
                    cps=cps_verified,
                )

        return ChainVerificationResult(
            status=VERIFY_COMPLETED,
            entries_checked=checked,
            checkpoints_verified=cps_verified,
        )

    def _deep_verify_money_row(self, row: ChainEntryRow) -> str | None:
        """Recompute payload_hash from journal_entries + postings content (E5)."""
        entry_id = entry_id_from_pointer(row.payload_pointer)
        if entry_id is None:
            return "payload_pointer is not a parseable ledger pointer"
        entry = self._store.get_journal_entry(entry_id)
        if entry is None:
            return f"journal entry {entry_id} referenced by the chain is missing (row deleted)"
        postings = self._store.get_postings_for_entry(entry_id)
        if len(postings) < 2:
            return f"journal entry {entry_id} has {len(postings)} postings (rows deleted)"
        debit = sum(p.amount_minor for p in postings if p.side == "DEBIT")
        credit = sum(p.amount_minor for p in postings if p.side == "CREDIT")
        if debit != credit:
            return f"journal entry {entry_id} is imbalanced: debit={debit} credit={credit}"
        if debit + credit != row.amount_minor_sum:
            return (
                f"amount_minor_sum mismatch for {entry_id}: chain says {row.amount_minor_sum}, "
                f"postings sum to {debit + credit} (amounts mutated)"
            )
        if entry.entry_type != row.entry_type:
            return f"entry_type mismatch for {entry_id} (journal row mutated)"
        if to_canonical_ts(entry.produced_at) != to_canonical_ts(row.produced_at):
            return f"produced_at mismatch for {entry_id} (journal row mutated)"
        recomputed = journal_entry_payload_hash(entry, postings)
        if recomputed != row.payload_hash:
            return (
                f"payload_hash mismatch for {entry_id}: journal/posting content does not "
                "match the hash sealed into the chain"
            )
        return None

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_entry_spec(self, spec: JournalEntrySpec) -> None:
        if not spec.reference_id:
            raise LedgerValidationError("reference_id is required")
        if spec.reference_type not in REFERENCE_TYPES:
            raise LedgerValidationError(f"unknown reference_type {spec.reference_type!r}")
        if spec.entry_type not in ENTRY_TYPES:
            raise LedgerValidationError(f"unknown entry_type {spec.entry_type!r}")
        if not spec.description:
            raise LedgerValidationError("description is required (PII-free)")
        if not spec.produced_by:
            raise LedgerValidationError("produced_by is required")
        if not spec.idempotency_key:
            raise LedgerValidationError("idempotency_key is required")
        if len(spec.postings) < 2:
            raise LedgerValidationError("a journal entry requires at least 2 postings")

        debit = credit = 0
        for p in spec.postings:
            if p.side not in SIDES:
                raise LedgerValidationError(f"posting side must be DEBIT|CREDIT, got {p.side!r}")
            if not p.account_id:
                raise LedgerValidationError("posting account_id is required")
            if p.currency != "BDT":
                raise LedgerValidationError(f"posting currency must be 'BDT', got {p.currency!r}")
            amount = _require_amount_minor(p.amount_minor)
            if p.side == "DEBIT":
                debit += amount
            else:
                credit += amount
        if debit != credit:
            raise LedgerImbalanceError(
                f"postings do not sum to zero: debit={debit} credit={credit}"
            )

    def _now(self) -> datetime:
        dt = self._clock.now()
        if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
            raise LedgerValidationError("clock returned a naive datetime (spec/00 §7)")
        return dt
