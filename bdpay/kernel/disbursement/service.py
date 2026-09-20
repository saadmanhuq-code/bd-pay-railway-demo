"""Bulk disbursement service for spec/17 VX4."""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

from bdpay.connectors.sdk import Money, PaymentInstruction
from bdpay.kernel.disbursement.ids_ext import make_disbursement_id
from bdpay.kernel.disbursement.models import (
    DisbursementBatch,
    DisbursementBatchResult,
    DisbursementItem,
    DisbursementItemInput,
    ValidationRow,
)
from bdpay.kernel.disbursement.states import BATCH_TABLE, ITEM_TABLE
from bdpay.kernel.disbursement.store import DisbursementStore
from bdpay.kernel.fsm import TransitionDeniedError
from bdpay.ledger.settlement.beftn import SettlementFileConnector, build_beftn_entries
from bdpay.ledger.settlement.types import SettlementInstructionRow
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    ConnectorError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
    SanctionsBlockError,
)
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import (
    ApprovalPort,
    AuditEventSpec,
    AuditPort,
    ConnectorRunnerPort,
    JournalEntrySpec,
    LedgerPort,
    OutboxPort,
    PostingSpec,
    SanctionsFreshnessPort,
    SanctionsPort,
    SanctionsScreenResult,
)
from bdpay.platform.outbox import build_event

if TYPE_CHECKING:
    from bdpay.connectors.sdk import ConnectorResult

__all__ = [
    "DISBURSEMENT_PRODUCER",
    "HIGH_VALUE_COSIGN_THRESHOLD_MINOR",
    "DisbursementService",
    "disbursement_accounts",
]

DISBURSEMENT_PRODUCER = "settlement-engine@1"
HIGH_VALUE_COSIGN_THRESHOLD_MINOR = 100_000_000
SUPPORTED_RAILS = frozenset({"BEFTN_CREDIT", "BKASH", "NAGAD"})
MFS_RAIL_TO_CONNECTOR = {
    "BKASH": "bkash_pgw_v2",
    "NAGAD": "nagad_pgw_v33",
}
# Recipient screening is a platform obligation, not a rule-pack rule; the
# synthetic pack id keeps aml_alerts rows attributable (same convention as
# rule_monitor_evaluation_failure alerts in bdpay/compliance/monitor.py).
RECIPIENT_SCREENING_RULE_PACK_ID = "disbursement-recipient-screening@1"
_SCREENING_ALERT_MAX_LISTED_ITEMS = 20
# spec/07 screening contexts: the funding-approval screen is a PAYMENT-path
# screen; the pre-rail dispatch screen is a RESCREEN against the then-active
# list. Distinct contexts also keep the screener's per-screen evidence records
# distinct when approve and dispatch run within one clock instant.
_SCREENING_CONTEXT_BY_STAGE = {"approve": "PAYMENT", "dispatch": "RESCREEN"}
# Fail-closed reason code for a recipient whose screening-relevant identity
# (name or beneficiary ref) is empty/whitespace-only: the screener is never
# consulted with nothing to check, so this is blocked on the same item-scoped
# path as a genuine sanctions hit, never allowed through as "clean".
_BLANK_IDENTITY_REASON = "screening_identity_missing"


def disbursement_accounts(merchant_id: str) -> dict[str, str]:
    """Deterministic ledger account ids touched by spec/17 disbursement entries."""
    return {
        "merchant_settlement": make_id(
            "acct",
            {
                "owner_id": merchant_id,
                "account_subtype": "MERCHANT_SETTLEMENT",
                "currency": "BDT",
                "purpose_tag": "settlement_main",
            },
        ),
        "merchant_hold_reserve": make_id(
            "acct",
            {
                "owner_id": merchant_id,
                "account_subtype": "MERCHANT_HOLD_RESERVE",
                "currency": "BDT",
                "purpose_tag": "hold_reserve",
            },
        ),
        "sponsor_bank_tcsa": make_id(
            "acct",
            {
                "owner_id": None,
                "account_subtype": "SPONSOR_BANK_TCSA",
                "currency": "BDT",
                "purpose_tag": "tcsa_main",
            },
        ),
        "mdr_income": make_id(
            "acct",
            {
                "owner_id": None,
                "account_subtype": "MDR_INCOME",
                "currency": "BDT",
                "purpose_tag": "mdr_earned",
            },
        ),
    }


class DisbursementService:
    """Merchant-balance-funded bulk payout batches with maker-checker release."""

    def __init__(
        self,
        *,
        store: DisbursementStore,
        ledger: LedgerPort,
        audit: AuditPort,
        outbox: OutboxPort,
        approvals: ApprovalPort,
        clock: Clock,
        connector_runner: ConnectorRunnerPort | None = None,
        beftn_connector: SettlementFileConnector | None = None,
        sanctions: SanctionsPort | None = None,
        sanctions_freshness: SanctionsFreshnessPort | None = None,
        alerts: Any | None = None,  # AmlAlertService-shaped (raise_alert)
        producer: str = DISBURSEMENT_PRODUCER,
        transaction_factory: Callable[[], Any] | None = None,
        stale_sanctions_fail_closed: bool = False,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._audit = audit
        self._outbox = outbox
        self._approvals = approvals
        self._clock = clock
        self._connector_runner = connector_runner
        self._beftn_connector = beftn_connector
        self._sanctions = sanctions
        self._sanctions_freshness = sanctions_freshness
        self._alerts = alerts
        self._producer = producer
        self._transaction_factory = transaction_factory
        self._stale_sanctions_fail_closed = bool(stale_sanctions_fail_closed)

    @contextlib.contextmanager
    def _operation_transaction(self, conn: Any | None) -> Iterator[Any | None]:
        if conn is not None or self._transaction_factory is None:
            yield conn
            return
        with self._transaction_factory() as owned:
            with owned.transaction():
                yield owned

    # ------------------------------------------------------------------
    # API-facing facade
    # ------------------------------------------------------------------

    def create_disbursement_batch(
        self,
        payload: Mapping[str, object],
        *,
        merchant_id: str,
        maker_user_id: str,
        idempotency_key: str,
        clock: Clock,
    ) -> Mapping[str, object]:
        rows = payload.get("items")
        if not isinstance(rows, list):
            raise InvalidRequestError("items must be a list", code="validation_failed")
        parsed = tuple(
            DisbursementItemInput(
                beneficiary_ref=str(row.get("beneficiary_ref", ""))
                if isinstance(row, Mapping)
                else "",
                beneficiary_name=str(row.get("beneficiary_name", ""))
                if isinstance(row, Mapping)
                else "",
                rail=str(row.get("rail", "")) if isinstance(row, Mapping) else "",
                amount_minor=row.get("amount_minor", 0) if isinstance(row, Mapping) else 0,
                purpose_code=str(row.get("purpose_code", ""))
                if isinstance(row, Mapping)
                else "",
                item_ref=str(row.get("item_ref", "")) if isinstance(row, Mapping) else "",
            )
            for row in rows
        )
        result = self.create_batch(
            merchant_id=merchant_id,
            maker_user_id=maker_user_id,
            client_batch_ref=str(payload.get("client_batch_ref", "")),
            rows=parsed,
            conn=None,
        )
        return result.to_dict()

    def get_disbursement_batch(
        self, batch_id: str, *, merchant_id: str | None = None
    ) -> Mapping[str, object] | None:
        batch = self._store.get_batch(batch_id)
        if batch is not None and merchant_id is not None:
            self._assert_merchant(batch, merchant_id)
        return None if batch is None else batch.to_dict()

    def list_disbursement_items(
        self,
        batch_id: str,
        *,
        status: str | None,
        limit: int,
        cursor: str | None,
        merchant_id: str | None = None,
    ) -> tuple[list[Mapping[str, object]], str | None]:
        offset = int(cursor) if cursor else 0
        batch = self._require_batch(batch_id)
        if merchant_id is not None:
            self._assert_merchant(batch, merchant_id)
        items = self.list_items(batch_id, status=status)
        page = items[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(items) else None
        return [item.to_dict() for item in page], next_cursor

    def submit_disbursement_batch(
        self,
        batch_id: str,
        *,
        merchant_id: str,
        maker_user_id: str,
        totp_verified: bool,
        clock: Clock,
    ) -> Mapping[str, object]:
        batch = self._require_batch(batch_id)
        self._assert_merchant(batch, merchant_id)
        return self.submit_batch(
            batch_id, maker_user_id=maker_user_id, totp_verified=totp_verified
        ).to_dict()

    def cancel_disbursement_batch(
        self, batch_id: str, *, merchant_id: str, actor_id: str, clock: Clock
    ) -> Mapping[str, object]:
        batch = self._require_batch(batch_id)
        self._assert_merchant(batch, merchant_id)
        return self.cancel_batch(batch_id, actor_id=actor_id).to_dict()

    async def dispatch_disbursement_batch(
        self,
        batch_id: str,
        *,
        operator_id: str,
        session: str,
        effective_date: date,
        clock: Clock,
    ) -> Mapping[str, object]:
        del clock
        return (
            await self.dispatch_batch(
                batch_id,
                beftn_connector=self._beftn_connector,
                session=session,
                effective_date=effective_date,
                actor_id=operator_id,
            )
        ).to_dict()

    # ------------------------------------------------------------------
    # Intake and validation
    # ------------------------------------------------------------------

    def create_batch(
        self,
        *,
        merchant_id: str,
        maker_user_id: str,
        client_batch_ref: str,
        rows: Iterable[DisbursementItemInput],
        conn: Any | None = None,
    ) -> DisbursementBatchResult:
        self._require_nonempty("merchant_id", merchant_id)
        self._require_nonempty("maker_user_id", maker_user_id)
        self._require_nonempty("client_batch_ref", client_batch_ref)
        row_tuple = tuple(rows)
        if not row_tuple:
            raise InvalidRequestError(
                "at least one disbursement item is required", code="items_required"
            )
        now = self._now()
        batch_id = make_disbursement_id(
            "dbat",
            {"merchant_id": merchant_id, "client_batch_ref": client_batch_ref},
        )
        accepted: list[DisbursementItem] = []
        report: list[ValidationRow] = []
        for ordinal, row in enumerate(row_tuple, start=1):
            clean, codes = self._validate_item(row)
            item_id: str | None = None
            if clean is not None:
                item_id = make_disbursement_id(
                    "ditm",
                    {
                        "batch_id": batch_id,
                        "ordinal": ordinal,
                        "beneficiary_ref": clean["beneficiary_ref"],
                        "amount_minor": clean["amount_minor"],
                    },
                )
                accepted.append(
                    DisbursementItem(
                        item_id=item_id,
                        batch_id=batch_id,
                        ordinal=ordinal,
                        beneficiary_ref=clean["beneficiary_ref"],
                        beneficiary_name_normalized=clean["beneficiary_name"],
                        rail=clean["rail"],
                        amount_minor=clean["amount_minor"],
                        fee_minor=0,
                        purpose_code=clean["purpose_code"],
                        item_ref=clean["item_ref"],
                        status="QUEUED",
                    )
                )
            report.append(
                ValidationRow(
                    ordinal=ordinal,
                    item_ref=row.item_ref or None,
                    accepted=clean is not None,
                    codes=tuple(codes),
                    item_id=item_id,
                )
            )
        draft = DisbursementBatch(
            batch_id=batch_id,
            merchant_id=merchant_id,
            client_batch_ref=client_batch_ref,
            item_count=len(accepted),
            total_amount_minor=sum(item.amount_minor for item in accepted),
            fees_total_minor=sum(item.fee_minor for item in accepted),
            status="DRAFT",
            maker_user_id=maker_user_id,
            approval_request_id=None,
            reconciliation_tag=make_disbursement_id(
                "dbat",
                {
                    "merchant_id": merchant_id,
                    "client_batch_ref": client_batch_ref,
                    "reconciliation": "tag",
                },
            ),
            created_at=now,
        )
        with self._operation_transaction(conn) as tx_conn:
            if (
                self._store.find_batch_by_client_ref(
                    merchant_id, client_batch_ref, conn=tx_conn
                )
                is not None
            ):
                raise ConflictError(
                    "client_batch_ref has already been used for this merchant",
                    code="client_batch_ref_exists",
                )
            trigger = "validated" if accepted else "validation_failed"
            rule = BATCH_TABLE.resolve("DRAFT", trigger)
            batch = replace(draft, status=rule.to_state)
            self._store.insert_batch(batch, conn=tx_conn)
            self._store.insert_items(accepted, conn=tx_conn)
            self._audit_batch(
                batch,
                "DRAFT",
                batch.status,
                "create",
                actor_id=maker_user_id,
                conn=tx_conn,
            )
            event_type = (
                "disbursement_batch.validated"
                if batch.status == "VALIDATED"
                else "disbursement_batch.validation_failed"
            )
            self._emit_batch(event_type, batch, conn=tx_conn)
        return DisbursementBatchResult(batch=batch, validation_report=tuple(report))

    @staticmethod
    def _validate_item(row: DisbursementItemInput) -> tuple[dict[str, Any] | None, list[str]]:
        codes: list[str] = []
        beneficiary_ref = row.beneficiary_ref.strip()
        beneficiary_name = normalize_bengali_digits(row.beneficiary_name).strip()
        rail = row.rail.strip().upper()
        purpose_code = row.purpose_code.strip().upper()
        item_ref = row.item_ref.strip()
        if not beneficiary_ref:
            codes.append("beneficiary_ref_required")
        elif any(ch.isspace() for ch in beneficiary_ref):
            codes.append("beneficiary_ref_invalid")
        if not beneficiary_name:
            codes.append("beneficiary_name_required")
        if rail not in SUPPORTED_RAILS:
            codes.append("rail_not_supported")
        amount = _parse_amount(row.amount_minor, codes)
        if not purpose_code:
            codes.append("purpose_code_required")
        if not item_ref:
            codes.append("item_ref_required")
        if codes:
            return None, codes
        return {
            "beneficiary_ref": beneficiary_ref,
            "beneficiary_name": beneficiary_name,
            "rail": rail,
            "amount_minor": amount,
            "purpose_code": purpose_code,
            "item_ref": item_ref,
        }, []

    # ------------------------------------------------------------------
    # Batch FSM
    # ------------------------------------------------------------------

    def submit_batch(
        self,
        batch_id: str,
        *,
        maker_user_id: str,
        totp_verified: bool,
        conn: Any | None = None,
    ) -> DisbursementBatch:
        if not totp_verified:
            raise AuthenticationError(
                "maker TOTP verification is required", code="totp_required"
            )
        batch = self._require_batch(batch_id)
        if maker_user_id != batch.maker_user_id:
            raise AuthorizationError(
                "only the batch maker may submit this batch", code="maker_mismatch"
            )
        # BDPAY-08: opt-in fail-closed sanctions-list freshness gate. Runs
        # before the FSM transition and the ApprovalRequest creation, so a
        # refused submission leaves the batch in its current state with no
        # approval request opened. Default OFF = no-op (alarm-only posture).
        self._enforce_sanctions_freshness(batch, conn=conn)
        rule = BATCH_TABLE.resolve(batch.status, "submit")
        approval = self._approvals.request(
            action_type="bulk_disbursement_release",
            subject_type="DisbursementBatch",
            subject_id=batch.batch_id,
            payload={
                "batch_id": batch.batch_id,
                "merchant_id": batch.merchant_id,
                "total_amount_minor": batch.total_amount_minor,
                "fees_total_minor": batch.fees_total_minor,
                "reconciliation_tag": batch.reconciliation_tag,
            },
            reason="bulk disbursement release",
            initiator_id=maker_user_id,
            amount_minor=batch.total_amount_minor + batch.fees_total_minor,
        )
        now = self._now()
        with self._operation_transaction(conn) as tx_conn:
            updated = self._store.update_batch(
                batch.batch_id,
                status=rule.to_state,
                approval_request_id=approval.approval_request_id,
                submitted_at=now,
                conn=tx_conn,
            )
            self._audit_batch(
                batch,
                batch.status,
                updated.status,
                "submit",
                actor_id=maker_user_id,
                conn=tx_conn,
            )
            self._emit_batch("disbursement_batch.submitted", updated, conn=tx_conn)
        return updated

    # ------------------------------------------------------------------
    # Recipient sanctions screening (audit P0 cab6a60 — fail-closed pre-rail)
    # ------------------------------------------------------------------

    def _screen_recipients(
        self,
        batch: DisbursementBatch,
        items: Iterable[DisbursementItem],
        *,
        stage: str,
        conn: Any | None = None,
    ) -> tuple[
        list[DisbursementItem],
        dict[str, SanctionsScreenResult],
        list[tuple[DisbursementItem, SanctionsScreenResult]],
    ]:
        """Screen every recipient synchronously; unavailable is a refusal.

        Returns ``(clean_items, evidence_by_item_id, hits)``. Raises
        :class:`SanctionsBlockError` when the screener is unwired or any
        screen attempt errors — mirroring the SanctionsPort contract that an
        unavailable screener is a decline, never a pass (ATA 2009 freeze
        obligation). An infrastructure failure carries no per-recipient
        information, so it aborts the whole operation without mutating item
        state; only a genuine HIT is item-scoped (handled by the caller).

        Two more fail-closed checks happen per item, both audit P0 cab6a60
        gaps: (1) a blank/whitespace-only ``beneficiary_name_normalized`` or
        ``beneficiary_ref`` is never forwarded to the screener — the DB schema
        only enforces NOT NULL, not non-blank, so this is item-scoped BLOCKED
        exactly like a hit, never silently "clean"; (2) a screener result that
        claims ``hit=False`` but carries no ``list_version_id`` is contract-pinned
        as unavailable (see ``_SanctionsScreenerAdapter`` in ``bdpay/app.py`` —
        the production screener can never emit CLEAR without at least one
        active list version, so a versionless clean result is either a broken
        adapter or a test double standing in for one) and aborts the whole
        operation the same way a screener exception does.

        RED-ON-REVERT: removing the calls to this method lets unscreened
        BEFTN/bKash/Nagad recipients reach the rails;
        tests/kernel/disbursement/test_disbursement_recipient_screening.py
        asserts approve/dispatch cannot complete without one screen per item.
        """
        item_tuple = tuple(items)
        if self._sanctions is None:
            self._raise_screening_alert(
                batch,
                stage=stage,
                blocked=[(item, "sanctions_screener_unwired") for item in item_tuple],
                conn=conn,
            )
            raise SanctionsBlockError(
                "disbursement recipient screening is not wired; refusing to "
                "move funds without a sanctions screen",
                code="disbursement_screening_unwired",
            )
        clean: list[DisbursementItem] = []
        hits: list[tuple[DisbursementItem, SanctionsScreenResult]] = []
        evidence: dict[str, SanctionsScreenResult] = {}
        for item in item_tuple:
            if _is_blank_identity(item):
                result = SanctionsScreenResult(
                    hit=True, hit_id=None, detail=_BLANK_IDENTITY_REASON
                )
                evidence[item.item_id] = result
                hits.append((item, result))
                continue
            try:
                result = self._sanctions.screen_entity(
                    entity_name=item.beneficiary_name_normalized,
                    entity_type="INDIVIDUAL",
                    identifiers={"beneficiary_ref": item.beneficiary_ref},
                    context=_SCREENING_CONTEXT_BY_STAGE.get(stage, "PAYMENT"),
                )
            except Exception as exc:  # noqa: BLE001 - fail-closed: any screener failure is a refusal
                self._raise_screening_alert(
                    batch,
                    stage=stage,
                    blocked=[
                        (item, f"sanctions_screening_unavailable:{type(exc).__name__}")
                    ],
                    conn=conn,
                )
                raise SanctionsBlockError(
                    "sanctions screening is unavailable for a disbursement "
                    "recipient; fail-closed refusal",
                    code="disbursement_screening_unavailable",
                ) from exc
            if not result.hit and result.list_version_id is None:
                self._raise_screening_alert(
                    batch,
                    stage=stage,
                    blocked=[(item, "sanctions_screening_no_evidence")],
                    conn=conn,
                )
                raise SanctionsBlockError(
                    "sanctions screening returned a clean result with no list "
                    "version evidence for a disbursement recipient; fail-closed "
                    "refusal",
                    code="disbursement_screening_unavailable",
                )
            evidence[item.item_id] = result
            if result.hit:
                hits.append((item, result))
            else:
                clean.append(item)
        if hits:
            self._raise_screening_alert(
                batch,
                stage=stage,
                blocked=[(item, _hit_reason_code(result)) for item, result in hits],
                conn=conn,
            )
        return clean, evidence, hits

    def _raise_screening_alert(
        self,
        batch: DisbursementBatch,
        *,
        stage: str,
        blocked: list[tuple[DisbursementItem, str]],
        conn: Any | None = None,
    ) -> None:
        """One AmlAlert per batch per screening gate (never per item, so a
        screener outage on a 10k-item batch cannot flood the CAMLCO queue).
        Missing alert wiring never bypasses the block itself."""
        if self._alerts is None:
            return
        listed = ", ".join(
            f"{item.item_id}={reason}"
            for item, reason in blocked[:_SCREENING_ALERT_MAX_LISTED_ITEMS]
        )
        overflow = len(blocked) - _SCREENING_ALERT_MAX_LISTED_ITEMS
        if overflow > 0:
            listed += f", +{overflow} more"
        self._alerts.raise_alert(
            subject_type="Merchant",
            subject_id=batch.merchant_id,
            rule_id=f"rule_disbursement_recipient_screening_{stage}",
            rule_pack_id=RECIPIENT_SCREENING_RULE_PACK_ID,
            risk_tier="HIGH",
            window_start=self._now(),
            triggered_amount_minor=sum(item.amount_minor for item, _ in blocked),
            notes=(
                f"disbursement {batch.batch_id} {stage} blocked pre-rail "
                f"({len(blocked)} item(s)): {listed}"
            ),
            conn=conn,
        )

    def _record_screening_evidence(
        self,
        evidence: Mapping[str, SanctionsScreenResult],
        *,
        conn: Any | None = None,
    ) -> None:
        now = self._now()
        for item_id, result in evidence.items():
            self._store.update_item(
                item_id,
                screened_at=now,
                screening_list_version=result.list_version_id,
                conn=conn,
            )

    # ------------------------------------------------------------------
    # Sanctions-list freshness gate (BDPAY-08 — opt-in fail-closed)
    # ------------------------------------------------------------------

    def _enforce_sanctions_freshness(
        self,
        batch: DisbursementBatch,
        *,
        stage: str = "submit",
        conn: Any | None = None,
    ) -> None:
        """Refuse submission/dispatch when the active sanctions list is stale.

        Opt-in via ``stale_sanctions_fail_closed`` (default OFF → no-op, the
        legacy alarm-only posture where a stale list raises the spec/12 §G
        side-channel STALE alarm but screening continues). When ON, a stale
        watchlist — one whose newest ACTIVE version is older than the
        freshness threshold (24h, the same line the §G alarm draws) — refuses
        disbursement submission: money cannot move against an obsolete list,
        and a CLEAR screen against a stale book is not honest clearance.

        Fail-closed on missing proof (TAXONOMY: money/compliance paths fail
        closed when proof is missing): flag ON with no ``SanctionsFreshnessPort``
        wired is a refusal, never a pass — the gate cannot assert freshness it
        cannot measure.

        F01/F03: the gate also runs at DISPATCH — the final external-effect
        boundary. Submission-time freshness says nothing about the list the
        money actually moves against: a verdict can go stale between submit,
        checker approval and the rail handoff (review F03 / evidence E06, E18,
        where a 30-hour-stale verdict after approval still reached the BEFTN
        connector). The dispatch check runs BEFORE the batch CAS transition and
        before any provider call — BEFTN file submit, BEFTN status query, MFS
        runner — so a stale list produces ZERO provider calls and leaves the
        batch in its current, retryable state.

        RED-ON-REVERT: deleting the call in ``submit_batch`` or in
        ``dispatch_batch``/``_recover_dispatching_batch`` (or flipping the
        default to True) lets a stale list clear disbursement; the BDPAY-08
        test module asserts flag-on+stale refuses while flag-off stays alarm-only.
        """
        if not self._stale_sanctions_fail_closed:
            return  # default posture: alarm-only, screening proceeds
        now = self._now()
        if self._sanctions_freshness is None:
            self._raise_stale_list_alert(batch, verdict=None, stage=stage, conn=conn)
            raise SanctionsBlockError(
                "stale-sanctions fail-closed is enabled but no sanctions freshness "
                f"port is wired; refusing {stage} without a freshness proof",
                code="disbursement_sanctions_list_stale",
            )
        verdict = self._sanctions_freshness.is_stale(now=now)
        if verdict.is_stale:
            self._raise_stale_list_alert(batch, verdict=verdict, stage=stage, conn=conn)
            raise SanctionsBlockError(
                "sanctions watchlist is stale (last ingest older than the freshness "
                f"threshold); stale-sanctions fail-closed refuses disbursement {stage}",
                code="disbursement_sanctions_list_stale",
            )

    def _raise_stale_list_alert(
        self,
        batch: DisbursementBatch,
        *,
        verdict: Any,  # SanctionsFreshnessVerdict | None
        stage: str = "submit",
        conn: Any | None = None,
    ) -> None:
        """One HIGH alert when the freshness gate refuses a submission/dispatch.

        ``verdict is None`` means the gate fired fail-closed with no freshness
        port wired (missing proof). Missing alert wiring never bypasses the
        block itself — the SanctionsBlockError is raised regardless.
        """
        if self._alerts is None:
            return
        if verdict is None:
            notes = (
                f"disbursement {batch.batch_id} {stage} blocked: sanctions freshness "
                "proof missing (no freshness port wired)"
            )
        else:
            notes = (
                f"disbursement {batch.batch_id} {stage} blocked: sanctions list stale "
                f"(age_seconds={verdict.age_seconds}, "
                f"threshold_seconds={verdict.threshold_seconds})"
            )
        self._alerts.raise_alert(
            subject_type="Merchant",
            subject_id=batch.merchant_id,
            rule_id="rule_sanctions_list_stale",
            rule_pack_id=RECIPIENT_SCREENING_RULE_PACK_ID,
            risk_tier="HIGH",
            window_start=self._now(),
            notes=notes,
            conn=conn,
        )

    def approve_batch(
        self,
        batch_id: str,
        *,
        checker_user_id: str,
        platform_finance_cosign: bool = False,
        conn: Any | None = None,
    ) -> DisbursementBatch:
        batch = self._require_batch(batch_id, conn=conn)
        if batch.approval_request_id is None:
            raise ConflictError(
                "batch has no approval request", code="approval_request_missing"
            )
        total = batch.total_amount_minor + batch.fees_total_minor
        if total > HIGH_VALUE_COSIGN_THRESHOLD_MINOR and not platform_finance_cosign:
            raise AuthorizationError(
                "platform finance co-sign is required for this batch",
                code="platform_finance_cosign_required",
            )
        # Fail-closed recipient screen BEFORE the approval decision and the
        # funding journal entry: any hit or screener failure leaves the batch
        # in PENDING_APPROVAL with the approval request undecided.
        _, screen_evidence, screen_hits = self._screen_recipients(
            batch, self.list_items(batch_id, conn=conn), stage="approve", conn=conn
        )
        if screen_hits:
            raise SanctionsBlockError(
                f"{len(screen_hits)} disbursement recipient(s) matched an active "
                "sanctions list or failed identity screening; approval refused",
                code="disbursement_recipient_sanctions_hit",
            )
        self._approvals.decide(
            batch.approval_request_id,
            approver_id=checker_user_id,
            approve=True,
            decision_reason="bulk disbursement release approved",
        )
        with self._operation_transaction(conn) as tx_conn:
            batch = self._require_batch(batch_id, conn=tx_conn, lock=True)
            rule = BATCH_TABLE.resolve(batch.status, "approved")
            accounts = disbursement_accounts(batch.merchant_id)
            available = self._ledger.get_balance(
                accounts["merchant_settlement"], as_of=None, conn=tx_conn, lock=True
            )
            if available < total:
                raise LimitExceededError(
                    "merchant settlement balance is insufficient for this batch",
                    code="insufficient_merchant_settlement_balance",
                )
            self._ledger.post_journal_entry(
                JournalEntrySpec(
                    reference_id=batch.batch_id,
                    reference_type="SETTLEMENT",
                    entry_type="disbursement_funded",
                    description="bulk disbursement funded into merchant hold reserve",
                    produced_by=self._producer,
                    idempotency_key=make_id(
                        "je",
                        {"batch_id": batch.batch_id, "entry_type": "disbursement_funded"},
                    ),
                    postings=(
                        PostingSpec(accounts["merchant_settlement"], "DEBIT", total),
                        PostingSpec(accounts["merchant_hold_reserve"], "CREDIT", total),
                    ),
                ),
                clock=self._clock,
                conn=tx_conn,
            )
            now = self._now()
            updated = self._store.update_batch(
                batch.batch_id,
                status=rule.to_state,
                approved_at=now,
                conn=tx_conn,
                expected_status=batch.status,
            )
            self._record_screening_evidence(screen_evidence, conn=tx_conn)
            self._audit_batch(
                batch,
                batch.status,
                updated.status,
                "approved",
                actor_id=checker_user_id,
                conn=tx_conn,
            )
            self._emit_batch("disbursement_batch.approved", updated, conn=tx_conn)
        mark_executed = getattr(self._approvals, "mark_executed", None)
        if mark_executed is not None:
            mark_executed(batch.approval_request_id)
        return updated

    def cancel_batch(
        self, batch_id: str, *, actor_id: str, conn: Any | None = None
    ) -> DisbursementBatch:
        batch = self._require_batch(batch_id, conn=conn)
        now = self._now()
        with self._operation_transaction(conn) as tx_conn:
            batch = self._require_batch(batch_id, conn=tx_conn, lock=True)
            rule = BATCH_TABLE.resolve(batch.status, "cancel")
            updated = self._store.update_batch(
                batch_id,
                status=rule.to_state,
                cancelled_at=now,
                conn=tx_conn,
                expected_status=batch.status,
            )
            self._audit_batch(
                batch,
                batch.status,
                updated.status,
                "cancel",
                actor_id=actor_id,
                conn=tx_conn,
            )
            self._emit_batch("disbursement_batch.cancelled", updated, conn=tx_conn)
        withdraw = getattr(self._approvals, "withdraw", None)
        if withdraw is not None and batch.approval_request_id is not None:
            try:
                withdraw(batch.approval_request_id, actor_id=actor_id)
            except Exception:
                pass
        return updated

    def apply_approval_terminal(
        self, batch_id: str, *, actor_id: str = "system:approval"
    ) -> DisbursementBatch:
        now = self._now()
        with self._operation_transaction(None) as tx_conn:
            batch = self._require_batch(batch_id, conn=tx_conn, lock=True)
            rule = BATCH_TABLE.resolve(batch.status, "rejected_or_expired")
            updated = self._store.update_batch(
                batch_id,
                status=rule.to_state,
                cancelled_at=now,
                conn=tx_conn,
                expected_status=batch.status,
            )
            self._audit_batch(
                batch,
                batch.status,
                updated.status,
                "rejected_or_expired",
                actor_id=actor_id,
                conn=tx_conn,
            )
            self._emit_batch("disbursement_batch.cancelled", updated, conn=tx_conn)
        return updated

    # ------------------------------------------------------------------
    # Rail dispatch and item outcomes
    # ------------------------------------------------------------------

    async def dispatch_batch(
        self,
        batch_id: str,
        *,
        beftn_connector: SettlementFileConnector | None,
        session: str,
        effective_date: date,
        actor_id: str | None = None,
        conn: Any | None = None,
    ) -> DisbursementBatch:
        batch = self._require_batch(batch_id, conn=conn)
        # F03: freshness is re-consulted at the final external-effect boundary,
        # BEFORE any provider call or state transition. A verdict that was
        # fresh at submit can be stale by the time the checker approves and the
        # rails are handed the money.
        self._enforce_sanctions_freshness(batch, stage="dispatch", conn=conn)
        if batch.status == "DISPATCHING":
            return await self._recover_dispatching_batch(
                batch,
                beftn_connector=beftn_connector,
                session=session,
                effective_date=effective_date,
                actor_id=actor_id,
                conn=conn,
            )
        rule = BATCH_TABLE.resolve(batch.status, "dispatch")
        actor = actor_id or self._producer
        items = self.list_items(batch_id, conn=conn)
        if not items:
            raise ConflictError("batch has no accepted items", code="batch_empty")
        # Pre-rail RE-screen (audit P0 cab6a60): a hit added to the watchlist
        # after approval fails that item terminally before any rail handoff
        # (funded hold-reserve share released via disbursement_failed_release);
        # a screener failure aborts dispatch with the batch still APPROVED.
        clean_items, screen_evidence, screen_hits = self._screen_recipients(
            batch, items, stage="dispatch", conn=conn
        )
        now = self._now()
        try:
            with self._operation_transaction(conn) as tx_conn:
                # expected_status CAS: two dispatchers racing on the same
                # APPROVED batch must not both hand the batch to the rails.
                updated = self._store.update_batch(
                    batch.batch_id,
                    status=rule.to_state,
                    dispatched_at=now,
                    conn=tx_conn,
                    expected_status=batch.status,
                )
                self._record_screening_evidence(screen_evidence, conn=tx_conn)
                self._audit_batch(
                    batch,
                    batch.status,
                    updated.status,
                    "dispatch",
                    actor_id=actor,
                    conn=tx_conn,
                )
                self._emit_batch("disbursement_batch.dispatched", updated, conn=tx_conn)
        except ConflictError as exc:
            # F04: LOSER SEMANTICS. The winner of the CAS already moved the
            # batch out of APPROVED; the loser must not surface
            # ConflictError('stale batch transition') to its caller (review F04
            # / evidence E15). It retries against a FRESH snapshot:
            #
            #   * DISPATCHING — the winner is mid-dispatch. The recovery path,
            #     whose per-item ``claim_item_for_dispatch`` keeps the external
            #     transfer exactly-once, completes any item the winner has not
            #     claimed. NOTE: for a BEFTN batch this path may re-query and
            #     re-submit the settlement file while the winner is still
            #     building its own. Duplicate submission is held off by the
            #     connector, which persists the batch record before uploading
            #     and refuses a duplicate content hash
            #     (``bdpay/connectors/rails/beftn.py``); a per-batch dispatch
            #     claim would close the window in the service itself and is the
            #     right follow-up.
            #   * COMPLETED / PARTIALLY_RETURNED — the winner already finished:
            #     every item reached a terminal state, so there is nothing left
            #     to hand to a rail. Deterministic NO-OP returning the settled
            #     batch, rather than a raw conflict that depends purely on how
            #     fast the winner ran.
            #
            # Any other transition (a cancellation, or a state this dispatch
            # could never have produced) is a genuine conflict and propagates.
            if getattr(exc, "code", None) != "stale_state_transition":
                raise
            current = self._require_batch(batch_id, conn=conn)
            if current.status in ("COMPLETED", "PARTIALLY_RETURNED"):
                return current
            if current.status != "DISPATCHING":
                raise
            return await self._recover_dispatching_batch(
                current,
                beftn_connector=beftn_connector,
                session=session,
                effective_date=effective_date,
                actor_id=actor_id,
                conn=conn,
            )
        batch = updated
        # Durably fail every screening hit BEFORE any connector wiring check
        # or rail submission work touches the remaining clean items (audit P0
        # cab6a60 dispatch-ordering fix): a connector-unavailable error on a
        # different, clean item must never leave a detected hit un-persisted
        # for a later retry to resurrect if the watchlist changes back in the
        # meantime. The batch is already DISPATCHING by this point, so a
        # connector error raised below leaves it in the same recoverable
        # state the crash-recovery path (_recover_dispatching_batch) already
        # handles — it re-checks wiring and re-screens on the next attempt.
        for item, result in screen_hits:
            self.mark_item_failed(
                item.item_id,
                rail_transaction_id=None,
                reason_code=_hit_reason_code(result),
            )
        beftn_items = [item for item in clean_items if item.rail == "BEFTN_CREDIT"]
        if beftn_items:
            if beftn_connector is None:
                raise ConnectorError(
                    "BEFTN settlement connector is not wired",
                    code="beftn_connector_unavailable",
                )
        if any(item.rail in MFS_RAIL_TO_CONNECTOR for item in clean_items):
            if self._connector_runner is None:
                raise ConnectorError(
                    "MFS disbursement connector runner is not wired",
                    code="mfs_runner_unavailable",
                )
        # Re-read item state AFTER failing the hits: the BEFTN file and the
        # per-item dispatch loop must only ever see store-current clean items,
        # never the pre-screening snapshot.
        clean_ids = {item.item_id for item in clean_items}
        dispatchable = [
            item
            for item in self.list_items(batch.batch_id, conn=conn)
            if item.item_id in clean_ids and item.status == "QUEUED"
        ]
        beftn_items = [item for item in dispatchable if item.rail == "BEFTN_CREDIT"]
        if beftn_items:
            entries = self._beftn_entries(batch, beftn_items)
            outcome = await beftn_connector.submit_batch(
                batch.batch_id, session, entries, effective_date.isoformat()
            )
            self._require_batch_submit_success(outcome)
        await self._dispatch_queued_items(batch, dispatchable, actor_id=actor)
        return updated

    def _beftn_entries(
        self, batch: DisbursementBatch, beftn_items: Iterable[DisbursementItem]
    ) -> list[dict]:
        return build_beftn_entries(
            [_settlement_instruction_from_item(batch, item) for item in beftn_items],
            {batch.merchant_id: batch.merchant_id},
        )

    def _screen_for_dispatch(
        self,
        batch: DisbursementBatch,
        candidates: Iterable[DisbursementItem],
        *,
        conn: Any | None = None,
    ) -> set[str]:
        """Recovery-path pre-rail screen: fail hits terminally, return their ids.

        Raises :class:`SanctionsBlockError` (via ``_screen_recipients``) when
        the screener is unwired/unavailable, leaving the batch DISPATCHING for
        a later retry — nothing reaches a rail unscreened.
        """
        candidate_tuple = tuple(candidates)
        if not candidate_tuple:
            return set()
        _, evidence, hits = self._screen_recipients(
            batch, candidate_tuple, stage="dispatch", conn=conn
        )
        self._record_screening_evidence(evidence, conn=conn)
        for item, result in hits:
            self.mark_item_failed(
                item.item_id,
                rail_transaction_id=None,
                reason_code=_hit_reason_code(result),
            )
        return {item.item_id for item, _ in hits}

    async def _recover_dispatching_batch(
        self,
        batch: DisbursementBatch,
        *,
        beftn_connector: SettlementFileConnector | None,
        session: str,
        effective_date: date,
        actor_id: str | None,
        conn: Any | None,
    ) -> DisbursementBatch:
        actor = actor_id or self._producer
        # F03: the retry/recovery path hands money to the rails too — and its
        # first act would otherwise be a provider status query. Re-consult
        # freshness before any of that.
        self._enforce_sanctions_freshness(batch, stage="dispatch", conn=conn)
        items = self.list_items(batch.batch_id, conn=conn)
        beftn_items = [item for item in items if item.rail == "BEFTN_CREDIT"]
        if beftn_items:
            if beftn_connector is None:
                raise ConnectorError(
                    "BEFTN settlement connector is not wired",
                    code="beftn_connector_unavailable",
                )
            # Review finding beftn-dispatch-race-double-uploads: the CAS
            # winner's OWN dispatch call may still be uploading this batch's
            # settlement file when this loser reaches recovery — a query here
            # would legitimately see NOT_RECEIVED (the winner has not
            # finished the transport put yet) and resubmit while the winner
            # is mid-upload. ``is_upload_in_flight`` is the connector's own
            # single-flight claim (bdpay/connectors/rails/beftn.py) read back,
            # not a wall-clock guess: it is only True while a real upload
            # attempt is genuinely running (an expired lease or a finished
            # upload both read False), so this never affects a sequential
            # retry after the winner's OWN attempt already returned or raised
            # (the claim it held is released either way, before it returns).
            # An older fake connector without this capability (existing unit
            # tests) is treated as never in flight — unchanged behaviour.
            is_in_flight = getattr(beftn_connector, "is_upload_in_flight", None)
            if callable(is_in_flight) and is_in_flight(batch.batch_id):
                return batch
            outcome = await beftn_connector.query_batch_status(batch.batch_id)
            status = str(outcome.get("status", "")).upper()
            if status in {"SUBMITTED", "ACCEPTED"}:
                # The BEFTN file already reached the rail; only still-QUEUED
                # MFS items face a rail handoff here, so only they re-screen.
                blocked = self._screen_for_dispatch(
                    batch,
                    (
                        item
                        for item in items
                        if item.status == "QUEUED"
                        and item.rail in MFS_RAIL_TO_CONNECTOR
                    ),
                    conn=conn,
                )
                await self._dispatch_queued_items(
                    batch,
                    self._current_unblocked_items(batch.batch_id, blocked, conn=conn),
                    actor_id=actor,
                )
                return batch
            if status == "NOT_RECEIVED":
                # The file never reached the rail: every non-terminal item is
                # about to face a (re)handoff, so all of them re-screen and
                # the rebuilt file carries only screened-clean BEFTN items —
                # re-read from the store AFTER the screen so a concurrent
                # failure between screen and rebuild never rides the file out.
                pending = [
                    item
                    for item in items
                    if item.status not in ITEM_TABLE.terminal_states
                ]
                blocked = self._screen_for_dispatch(batch, pending, conn=conn)
                screened_ids = {
                    item.item_id for item in pending if item.item_id not in blocked
                }
                rebuild = [
                    item
                    for item in self.list_items(batch.batch_id, conn=conn)
                    if item.item_id in screened_ids
                    and item.status not in ITEM_TABLE.terminal_states
                ]
                clean_beftn = [
                    item for item in rebuild if item.rail == "BEFTN_CREDIT"
                ]
                if clean_beftn:
                    entries = self._beftn_entries(batch, clean_beftn)
                    submit = await beftn_connector.submit_batch(
                        batch.batch_id, session, entries, effective_date.isoformat()
                    )
                    self._require_batch_submit_success(submit)
                await self._dispatch_queued_items(batch, rebuild, actor_id=actor)
                return batch
            raise ConflictError(
                "BEFTN batch dispatch acknowledgement is still pending",
                code="batch_dispatch_ack_pending",
            )
        blocked = self._screen_for_dispatch(
            batch,
            (item for item in items if item.status == "QUEUED"),
            conn=conn,
        )
        await self._dispatch_queued_items(
            batch,
            self._current_unblocked_items(batch.batch_id, blocked, conn=conn),
            actor_id=actor,
        )
        return batch

    def _current_unblocked_items(
        self, batch_id: str, blocked: set[str], *, conn: Any | None = None
    ) -> list[DisbursementItem]:
        """Store-current items minus screening-blocked ids (never a stale
        pre-screen snapshot — the rail must only ever see current state)."""
        return [
            item
            for item in self.list_items(batch_id, conn=conn)
            if item.item_id not in blocked
        ]

    async def _dispatch_queued_items(
        self,
        batch: DisbursementBatch,
        items: Iterable[DisbursementItem],
        *,
        actor_id: str,
    ) -> None:
        for item in items:
            if item.status != "QUEUED":
                continue
            if item.rail in MFS_RAIL_TO_CONNECTOR:
                claimed = self._store.claim_item_for_dispatch(item.item_id)
                if claimed is None:
                    continue
                await self._dispatch_mfs_item(batch, claimed)
                current = self._store.get_item(item.item_id)
                if current is not None and current.status == "DISPATCHING":
                    self._advance_item(current, "dispatched", actor_id=actor_id)
            else:
                try:
                    self._advance_item(item, "dispatched", actor_id=actor_id)
                except ConflictError:
                    # The item changed under this snapshot (e.g. terminally
                    # FAILED by the sanctions screen in a concurrent dispatch);
                    # never clobber that state back to DISPATCHED.
                    continue

    @staticmethod
    def _require_batch_submit_success(outcome: Mapping[str, object]) -> None:
        status = str(outcome.get("status", "")).upper()
        if status not in {"SUBMITTED", "ACCEPTED"}:
            raise ConnectorError(
                "BEFTN batch submission was not accepted", code="beftn_batch_refused"
            )

    async def _dispatch_mfs_item(
        self, batch: DisbursementBatch, item: DisbursementItem
    ) -> None:
        if self._connector_runner is None:
            raise ConnectorError(
                "MFS disbursement connector runner is not wired",
                code="mfs_runner_unavailable",
            )
        connector_id = MFS_RAIL_TO_CONNECTOR[item.rail]
        instruction = PaymentInstruction(
            instruction_id=make_id(
                "ins",
                {
                    "item_id": item.item_id,
                    "rail": item.rail,
                    "amount_minor": item.amount_minor,
                },
            ),
            connector_ref=item.item_id,
            amount=Money(item.amount_minor),
            method=item.rail,
            sender_ref=batch.merchant_id,
            beneficiary_ref=item.beneficiary_ref,
            rail=connector_id,
            instruction_at=_rfc3339(self._now()),
            metadata={
                "batch_id": batch.batch_id,
                "item_id": item.item_id,
                "purpose_code": item.purpose_code,
                "reconciliation_tag": batch.reconciliation_tag,
            },
        )
        result = await self._connector_runner.submit(instruction)
        self._apply_connector_result(item, result)

    def _apply_connector_result(
        self, item: DisbursementItem, result: ConnectorResult
    ) -> None:
        status = result.status.value if hasattr(result.status, "value") else str(result.status)
        if status == "success":
            self.mark_item_paid(
                item.item_id,
                rail_transaction_id=result.rail_transaction_id,
            )
            return
        if status == "pending":
            self._store.update_item(
                item.item_id, rail_transaction_id=result.rail_transaction_id
            )
            return
        if status == "rejected":
            self.mark_item_returned(
                item.item_id,
                rail_transaction_id=result.rail_transaction_id,
                reason_code=result.error_code or "connector_rejected",
            )
            return
        self.mark_item_failed(
            item.item_id,
            rail_transaction_id=result.rail_transaction_id,
            reason_code=result.error_code or "connector_failed",
        )

    def mark_item_paid(
        self,
        item_id: str,
        *,
        rail_transaction_id: str,
        conn: Any | None = None,
    ) -> DisbursementItem:
        with self._operation_transaction(conn) as tx_conn:
            item = self._require_item(item_id, conn=tx_conn)
            batch = self._require_batch(item.batch_id, conn=tx_conn)
            updated = self._advance_item(
                item,
                "settled",
                actor_id=self._producer,
                rail_transaction_id=rail_transaction_id,
                settled_at=self._now(),
                conn=tx_conn,
            )
            self._post_item_paid(batch, updated, conn=tx_conn)
            self._emit_item("disbursement_item.paid", updated, batch, conn=tx_conn)
            self._complete_batch_if_ready(batch.batch_id, conn=tx_conn)
        return updated

    def mark_item_returned(
        self,
        item_id: str,
        *,
        rail_transaction_id: str | None,
        reason_code: str,
        conn: Any | None = None,
    ) -> DisbursementItem:
        with self._operation_transaction(conn) as tx_conn:
            item = self._require_item(item_id, conn=tx_conn)
            batch = self._require_batch(item.batch_id, conn=tx_conn)
            from_state = item.status
            updated = self._store.claim_item_returned(item_id, conn=tx_conn)
            if updated is None:
                # Another concurrent caller won the race; report the current
                # state so the consumer can map terminal states to
                # already_processed exactly as it maps a TransitionDeniedError.
                current = self._require_item(item_id, conn=tx_conn)
                raise TransitionDeniedError(
                    f"DisbursementItem: transition denied from {current.status!r} on "
                    "'returned': item is not in a returnable state",
                    machine="DisbursementItem",
                    from_state=current.status,
                    trigger="returned",
                )
            if rail_transaction_id is not None:
                updated = self._store.update_item(
                    item_id,
                    rail_transaction_id=rail_transaction_id,
                    settled_at=self._now(),
                    conn=tx_conn,
                )
            self._audit_item(
                item,
                from_state,
                updated.status,
                "returned",
                actor_id=self._producer,
                conn=tx_conn,
                extra={"reason_code": reason_code},
            )
            self._post_item_returned(batch, updated, reason_code=reason_code, conn=tx_conn)
            self._emit_item(
                "disbursement_item.returned",
                updated,
                batch,
                conn=tx_conn,
                extra={"reason_code": reason_code},
            )
            self._complete_batch_if_ready(batch.batch_id, conn=tx_conn)
        return updated

    def mark_item_failed(
        self,
        item_id: str,
        *,
        rail_transaction_id: str | None,
        reason_code: str,
    ) -> DisbursementItem:
        with self._operation_transaction(None) as tx_conn:
            item = self._require_item(item_id, conn=tx_conn)
            batch = self._require_batch(item.batch_id, conn=tx_conn)
            rule = ITEM_TABLE.resolve(item.status, "failed")
            updated = self._advance_item(
                item,
                "failed",
                actor_id=self._producer,
                audit_extra={"reason_code": reason_code},
                rail_transaction_id=rail_transaction_id,
                settled_at=self._now(),
                conn=tx_conn,
            )
            if "je:disbursement_failed_release" in rule.side_effects:
                self._post_item_failed(batch, updated, conn=tx_conn)
            self._emit_item(
                "disbursement_item.failed",
                updated,
                batch,
                conn=tx_conn,
                extra={"reason_code": reason_code},
            )
            self._complete_batch_if_ready(batch.batch_id, conn=tx_conn)
        return updated

    def _complete_batch_if_ready(self, batch_id: str, *, conn: Any | None = None) -> None:
        batch = self._require_batch(batch_id, conn=conn)
        items = self.list_items(batch_id, conn=conn)
        if not items or any(item.status not in ITEM_TABLE.terminal_states for item in items):
            return
        guard = (
            "all_paid"
            if all(item.status == "PAID" for item in items)
            else "any_returned_or_failed"
        )
        rule = BATCH_TABLE.resolve(batch.status, "all_items_terminal", guard=guard)
        now = self._now()
        updated = self._store.update_batch(
            batch_id, status=rule.to_state, completed_at=now, conn=conn
        )
        self._audit_batch(
            batch,
            batch.status,
            updated.status,
            "all_items_terminal",
            actor_id=self._producer,
            conn=conn,
        )
        self._emit_batch("disbursement_batch.completed", updated, conn=conn)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_batch(self, batch_id: str, *, conn: Any | None = None) -> DisbursementBatch:
        return self._require_batch(batch_id, conn=conn)

    def list_items(
        self, batch_id: str, *, status: str | None = None, conn: Any | None = None
    ) -> tuple[DisbursementItem, ...]:
        return self._store.list_items(batch_id, status=status, conn=conn)

    # ------------------------------------------------------------------
    # Ledger and event helpers
    # ------------------------------------------------------------------

    def _post_item_paid(
        self, batch: DisbursementBatch, item: DisbursementItem, *, conn: Any | None
    ) -> None:
        accounts = disbursement_accounts(batch.merchant_id)
        debit = item.amount_minor + item.fee_minor
        credits = [
            PostingSpec(accounts["sponsor_bank_tcsa"], "CREDIT", item.amount_minor)
        ]
        if item.fee_minor > 0:
            credits.append(PostingSpec(accounts["mdr_income"], "CREDIT", item.fee_minor))
        self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=item.item_id,
                reference_type="SETTLEMENT",
                entry_type="disbursement_item_paid",
                description="disbursement item paid",
                produced_by=self._producer,
                idempotency_key=make_id(
                    "je",
                    {"item_id": item.item_id, "entry_type": "disbursement_item_paid"},
                ),
                postings=(
                    PostingSpec(accounts["merchant_hold_reserve"], "DEBIT", debit),
                    *credits,
                ),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _post_item_failed(
        self,
        batch: DisbursementBatch,
        item: DisbursementItem,
        *,
        conn: Any | None,
    ) -> None:
        """Release the hold_reserve share for a FAILED item.

        Mirrors _post_item_returned exactly: DEBIT merchant_hold_reserve /
        CREDIT merchant_settlement for amount_minor + fee_minor so the funded
        reserve zeroes out regardless of whether the terminal state is PAID,
        RETURNED, or FAILED.

        RED-ON-REVERT: removing this call leaves merchant_hold_reserve
        permanently credited after a FAILED transition; the integration test
        test_failed_item_releases_hold_reserve_pg will catch this because
        get_balance(merchant_hold_reserve) will remain at the item amount
        instead of 0.
        """
        accounts = disbursement_accounts(batch.merchant_id)
        amount = item.amount_minor + item.fee_minor
        self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=item.item_id,
                reference_type="SETTLEMENT",
                entry_type="disbursement_failed_release",
                description="disbursement item failed; hold reserve released",
                produced_by=self._producer,
                idempotency_key=make_id(
                    "je",
                    {
                        "item_id": item.item_id,
                        "entry_type": "disbursement_failed_release",
                    },
                ),
                postings=(
                    PostingSpec(accounts["merchant_hold_reserve"], "DEBIT", amount),
                    PostingSpec(accounts["merchant_settlement"], "CREDIT", amount),
                ),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _post_item_returned(
        self,
        batch: DisbursementBatch,
        item: DisbursementItem,
        *,
        reason_code: str,
        conn: Any | None,
    ) -> None:
        accounts = disbursement_accounts(batch.merchant_id)
        amount = item.amount_minor + item.fee_minor
        self._ledger.post_journal_entry(
            JournalEntrySpec(
                reference_id=item.item_id,
                reference_type="REVERSAL",
                entry_type="disbursement_item_returned",
                description="disbursement item returned; hold released",
                produced_by=self._producer,
                idempotency_key=make_id(
                    "je",
                    {
                        "item_id": item.item_id,
                        "entry_type": "disbursement_item_returned",
                        "reason_code": reason_code,
                    },
                ),
                postings=(
                    PostingSpec(accounts["merchant_hold_reserve"], "DEBIT", amount),
                    PostingSpec(accounts["merchant_settlement"], "CREDIT", amount),
                ),
            ),
            clock=self._clock,
            conn=conn,
        )

    def _advance_item(
        self,
        item: DisbursementItem,
        trigger: str,
        *,
        actor_id: str,
        conn: Any | None = None,
        audit_extra: Mapping[str, object] | None = None,
        **changes: object,
    ) -> DisbursementItem:
        rule = ITEM_TABLE.resolve(item.status, trigger)
        # CAS on the from_state: a stale item snapshot (e.g. failed by the
        # pre-rail sanctions screen or a concurrent dispatcher after this
        # object was read) must never be overwritten by a later transition.
        updated = self._store.update_item(
            item.item_id,
            status=rule.to_state,
            conn=conn,
            expected_status=item.status,
            **changes,
        )
        self._audit_item(
            item,
            item.status,
            updated.status,
            trigger,
            actor_id=actor_id,
            conn=conn,
            extra=audit_extra,
        )
        return updated

    def _audit_batch(
        self,
        batch: DisbursementBatch,
        from_state: str | None,
        to_state: str,
        trigger: str,
        *,
        actor_id: str,
        conn: Any | None = None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="DISBURSEMENT_BATCH_STATE_TRANSITION",
                actor_id=actor_id,
                subject_type="DisbursementBatch",
                subject_id=batch.batch_id,
                from_state=from_state,
                to_state=to_state,
                payload={
                    "trigger": trigger,
                    "merchant_id": batch.merchant_id,
                    "client_batch_ref": batch.client_batch_ref,
                    "reconciliation_tag": batch.reconciliation_tag,
                },
            ),
            clock=self._clock,
            conn=conn,
        )

    def _audit_item(
        self,
        item: DisbursementItem,
        from_state: str,
        to_state: str,
        trigger: str,
        *,
        actor_id: str,
        conn: Any | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="DISBURSEMENT_ITEM_STATE_TRANSITION",
                actor_id=actor_id,
                subject_type="DisbursementItem",
                subject_id=item.item_id,
                from_state=from_state,
                to_state=to_state,
                payload={
                    "trigger": trigger,
                    "batch_id": item.batch_id,
                    "amount_minor": item.amount_minor,
                    "item_ref": item.item_ref,
                    **(dict(extra) if extra else {}),
                },
            ),
            clock=self._clock,
            conn=conn,
        )

    def _emit_batch(
        self, event_type: str, batch: DisbursementBatch, *, conn: Any | None = None
    ) -> None:
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="DisbursementBatch",
                subject_id=batch.batch_id,
                producer=self._producer,
                topic="settlement.events",
                occurred_at=self._now(),
                payload={
                    "batch_id": batch.batch_id,
                    "merchant_id": batch.merchant_id,
                    "item_count": batch.item_count,
                    "total_amount_minor": batch.total_amount_minor,
                    "fees_total_minor": batch.fees_total_minor,
                    "status": batch.status,
                    "reconciliation_tag": batch.reconciliation_tag,
                },
            ),
            conn=conn,
        )

    def _emit_item(
        self,
        event_type: str,
        item: DisbursementItem,
        batch: DisbursementBatch,
        *,
        conn: Any | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="DisbursementItem",
                subject_id=item.item_id,
                producer=self._producer,
                topic="settlement.events",
                occurred_at=self._now(),
                payload={
                    "item_id": item.item_id,
                    "batch_id": item.batch_id,
                    "merchant_id": batch.merchant_id,
                    "item_ref": item.item_ref,
                    "amount_minor": item.amount_minor,
                    "rail_transaction_id": item.rail_transaction_id,
                    "reconciliation_tag": batch.reconciliation_tag,
                    **(dict(extra) if extra else {}),
                },
            ),
            conn=conn,
        )

    # ------------------------------------------------------------------
    # Guards
    # ------------------------------------------------------------------

    @staticmethod
    def _require_nonempty(name: str, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequestError(f"{name} is required", code="validation_failed")

    def _require_batch(
        self, batch_id: str, *, conn: Any | None = None, lock: bool = False
    ) -> DisbursementBatch:
        batch = self._store.get_batch(batch_id, conn=conn, lock=lock)
        if batch is None:
            raise NotFoundError("disbursement batch not found", code="batch_not_found")
        return batch

    def _require_item(self, item_id: str, *, conn: Any | None = None) -> DisbursementItem:
        item = self._store.get_item(item_id, conn=conn)
        if item is None:
            raise NotFoundError("disbursement item not found", code="item_not_found")
        return item

    @staticmethod
    def _assert_merchant(batch: DisbursementBatch, merchant_id: str) -> None:
        if batch.merchant_id != merchant_id:
            raise AuthorizationError(
                "batch belongs to a different merchant", code="merchant_id_mismatch"
            )

    def _now(self) -> datetime:
        now = self._clock.now()
        if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
            raise InvalidRequestError("clock returned a naive datetime", code="invalid_clock")
        return now.astimezone(UTC)


def _is_blank_identity(item: DisbursementItem) -> bool:
    """True when a screening-relevant recipient identity field is empty or
    whitespace-only. Both fields are forwarded to the screener as the name
    and the exact-match identifier, so either being blank means nothing
    honest was actually screened."""
    return (
        not item.beneficiary_name_normalized.strip()
        or not item.beneficiary_ref.strip()
    )


def _hit_reason_code(result: SanctionsScreenResult) -> str:
    """Alert/audit reason for a blocked item: a genuine hit carries a
    ``hit_id``; a blank-identity block does not."""
    return f"sanctions_hit:{result.hit_id}" if result.hit_id else _BLANK_IDENTITY_REASON


def _parse_amount(value: int | str, codes: list[str]) -> int:
    if isinstance(value, bool):
        codes.append("amount_minor_invalid")
        return 0
    if isinstance(value, int):
        amount = value
    elif isinstance(value, str):
        normalized = normalize_bengali_digits(value).strip()
        if not normalized.isdigit():
            codes.append("amount_minor_invalid")
            return 0
        amount = int(normalized, 10)
    else:
        codes.append("amount_minor_invalid")
        return 0
    if amount <= 0:
        codes.append("amount_minor_must_be_positive")
    return amount


def _settlement_instruction_from_item(
    batch: DisbursementBatch, item: DisbursementItem
) -> SettlementInstructionRow:
    now = batch.created_at
    return SettlementInstructionRow(
        instruction_id=item.item_id,
        batch_id=batch.batch_id,
        payment_intent_id=item.item_ref,
        merchant_id=batch.merchant_id,
        direction="CREDIT",
        amount_minor=item.amount_minor,
        fee_minor=item.fee_minor,
        net_payout_minor=item.amount_minor,
        fee_rule_id=None,
        currency="BDT",
        rail="beftn_batch_v2",
        beneficiary_account_ref=item.beneficiary_ref,
        mcc_category="OTHER",
        high_value=False,
        earliest_release_at=now,
        latest_release_at=now,
        delivery_hold_cleared=True,
        aml_hold=False,
        status="QUEUED",
        rail_transaction_id=None,
        return_reason_code=None,
        connector_ref=item.item_id,
        created_at=now,
        submitted_at=None,
        settled_at=None,
        returned_at=None,
        failed_at=None,
        produced_by=DISBURSEMENT_PRODUCER,
    )


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
