"""Real-component adapters for the DossierExport member ports (spec/16 LR-2).

:mod:`bdpay.compliance.dossier` defines the assembler against narrow port
Protocols so the FSM/ZIP mechanics are provable in isolation. This module
binds those ports to the REAL built components — no fakes:

- :class:`LedgerChainVerifyAdapter` — evidence class 2 over
  ``LedgerService.verify_chain(deep=True)`` (E5: deep mode is the gate mode).
- :class:`LedgerTrialBalanceAdapter` — evidence class 3 over
  ``LedgerService.trial_balance()`` + a deterministic snapshot hash.
- :class:`LedgerReplayReadAdapter` — the ledger read surface consumed by
  :class:`~bdpay.compliance.dossier.ChainReplayAdapter` (evidence class 4):
  chain rows for ``ENTRY_SEQUENCE``, AUDIT-chained ``audit_events`` rows for
  ``DISPUTE_TRAIL``.
- :class:`CertificationRunReadAdapter` — evidence class 1 over the spec/10
  ``certification_runs`` store + connector registry (the registry defines
  "every registered connector" for the CERT-01..10 coverage gate).
- :class:`FactCorrectionRegisterAdapter` — evidence class 7 over the LR-3
  ``fact_corrections`` store (``rows_as_of`` filters on ``created_at`` so the
  register snapshot is deterministic for a fixed ``fact_register_as_of``).
- :class:`AuditTrailCaseEvidenceAdapter` — evidence class 8: the spec/15 case
  evidence pointers are the AUDIT-domain rows for the named payment intent
  (payload hash + object pointer per row; PII never leaves the audit store).
- :class:`TrackedSponsorPackAdapter` — LR-1 members: the tracked
  ``docs/sponsor-bank/term-sheet-checklist.md`` source plus claims rows that
  resolve to in-ZIP artifacts (the assembler recomputes every hash).

All adapters are read-only over their sources; none takes a wall clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from bdpay.compliance.fact_corrections import FactCorrection, FactCorrectionStore
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "AuditTrailCaseEvidenceAdapter",
    "CertificationRunReadAdapter",
    "DEFAULT_SPONSOR_CLAIMS",
    "DEFAULT_TERM_SHEET_PATH",
    "FactCorrectionRegisterAdapter",
    "LedgerChainVerifyAdapter",
    "LedgerReplayReadAdapter",
    "LedgerTrialBalanceAdapter",
    "TrackedSponsorPackAdapter",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Tracked LR-1 source artifact (spec/16 §API A LR-1 specifics).
DEFAULT_TERM_SHEET_PATH = _REPO_ROOT / "docs" / "sponsor-bank" / "term-sheet-checklist.md"


def _parse_rfc3339(value: str, *, field: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidRequestError(
            f"{field} must be an RFC3339 timestamp", code="invalid_timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise InvalidRequestError(
            f"{field} must carry an explicit UTC offset", code="invalid_timestamp"
        )
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Ledger-backed adapters (evidence classes 2, 3, 4)
# ---------------------------------------------------------------------------


class LedgerChainVerifyAdapter:
    """ChainVerifyPort over the real ``LedgerService.verify_chain`` (deep)."""

    def __init__(self, ledger: object) -> None:
        self._ledger = ledger

    def verify(self, chain_domain: str, from_index: int, to_index: int) -> dict[str, object]:
        result = self._ledger.verify_chain(  # type: ignore[attr-defined]
            chain_domain=chain_domain,
            from_index=from_index,
            to_index=to_index,
            deep=True,
        )
        return {
            "status": result.status,
            "deep": True,
            "entries_checked": result.entries_checked,
            "checkpoints_verified": result.checkpoints_verified,
            "first_broken_index": result.first_broken_index,
            "detail": result.detail,
        }


class LedgerTrialBalanceAdapter:
    """TrialBalancePort over the real ``LedgerService.trial_balance()``.

    The snapshot hash is content-addressed over the totals plus the MONEY
    chain tip index: two snapshots hash equal iff the ledger state they
    attest is the same (E12 canonical bytes).
    """

    def __init__(self, ledger: object) -> None:
        self._ledger = ledger

    def snapshot(self) -> dict[str, object]:
        from bdpay.platform.canonical import sha256_canonical

        balance = self._ledger.trial_balance()  # type: ignore[attr-defined]
        tip = self._ledger.store.max_chain_index("MONEY")  # type: ignore[attr-defined]
        return {
            "total_debit_minor": balance.total_debit_minor,
            "total_credit_minor": balance.total_credit_minor,
            "balanced": balance.balanced,
            "balances_hash": sha256_canonical(
                {
                    "total_debit_minor": balance.total_debit_minor,
                    "total_credit_minor": balance.total_credit_minor,
                    "money_chain_tip": tip,
                }
            ),
        }


class LedgerReplayReadAdapter:
    """The :class:`~bdpay.compliance.dossier.LedgerReadPort` over the ledger.

    ``list_entries`` yields the chain rows themselves (hashes + indexes — the
    inspector-replayable spine, never raw payloads); ``dispute_trail`` yields
    the AUDIT-chained ``audit_events`` rows for the payment intent with
    payload hash + pointer (PII stays in the audit store).
    """

    def __init__(self, ledger: object) -> None:
        self._ledger = ledger

    def list_entries(
        self, chain_domain: str, from_index: int, to_index: int
    ) -> list[dict[str, object]]:
        rows = self._ledger.store.iter_chain(  # type: ignore[attr-defined]
            chain_domain, from_index, to_index
        )
        return [
            {
                "chain_domain": row.chain_domain,
                "chain_index": row.chain_index,
                "entry_type": row.entry_type,
                "payload_hash": row.payload_hash,
                "chain_hash": row.chain_hash,
            }
            for row in rows
        ]

    def verify(self, chain_domain: str, from_index: int, to_index: int) -> dict[str, object]:
        return LedgerChainVerifyAdapter(self._ledger).verify(chain_domain, from_index, to_index)

    def dispute_trail(self, payment_intent_id: str) -> list[dict[str, object]]:
        rows = self._ledger.store.list_audit_events(  # type: ignore[attr-defined]
            subject_id=payment_intent_id
        )
        return [
            {
                "event_id": row.event_id,
                "entry_index": row.entry_index,
                "event_type": row.event_type,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "from_state": row.from_state,
                "to_state": row.to_state,
                "payload_hash": row.payload_hash,
            }
            for row in rows
        ]


class AuditTrailCaseEvidenceAdapter:
    """CaseEvidencePort: spec/15 case-evidence pointers from the audit chain.

    Every audit row for the named payment intent becomes one evidence pointer
    ``{path, sha256, pointer}`` — the sha256 is the row's ``payload_hash``
    (recorded at write time over the REDACTED payload) and the pointer is the
    row's ``payload_pointer`` into the audit payload store.
    """

    def __init__(self, ledger: object) -> None:
        self._ledger = ledger

    def evidence_for(self, payment_intent_id: str) -> list[dict[str, object]]:
        rows = self._ledger.store.list_audit_events(  # type: ignore[attr-defined]
            subject_id=payment_intent_id
        )
        return [
            {
                "path": f"audit-events/{row.event_id}.json",
                "sha256": row.payload_hash,
                "pointer": row.payload_pointer,
                "event_type": row.event_type,
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Certification read adapter (evidence class 1)
# ---------------------------------------------------------------------------


@runtime_checkable
class _RegistryReads(Protocol):
    def list_all(self) -> Sequence[object]: ...


class CertificationRunReadAdapter:
    """CertificationReadPort over the spec/10 run store + connector registry."""

    def __init__(self, run_store: object, registry: _RegistryReads) -> None:
        self._runs = run_store
        self._registry = registry

    def get_run(self, run_id: str) -> dict[str, object] | None:
        record = self._runs.get(run_id)  # type: ignore[attr-defined]
        if record is None:
            return None
        return {
            "run_id": record.run_id,
            "connector_id": record.connector_id,
            "adapter_version": record.adapter_version,
            "mode_under_test": record.mode_under_test,
            "status": record.status,
            "checks": [dict(check) for check in record.checks],
            "report_pointer": record.report_pointer,
            "report_hash": record.report_hash,
        }

    def registered_connector_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(reg.connector_id for reg in self._registry.list_all())  # type: ignore[attr-defined]
        )


# ---------------------------------------------------------------------------
# Fact register adapter (evidence class 7)
# ---------------------------------------------------------------------------


class FactCorrectionRegisterAdapter:
    """FactRegisterPort over the LR-3 ``fact_corrections`` store."""

    def __init__(self, store: FactCorrectionStore) -> None:
        self._store = store

    @staticmethod
    def _row(correction: FactCorrection) -> dict[str, object]:
        return {
            "correction_id": correction.correction_id,
            "claim_source": correction.claim_source,
            "claim_text": correction.claim_text,
            "corrected_text": correction.corrected_text,
            "status": correction.status,
            "external_blocking": correction.external_blocking,
            "evidence_pointer": correction.evidence_pointer,
            "evidence_sha256": correction.evidence_sha256,
            "supersedes_correction_id": correction.supersedes_correction_id,
            "created_at": correction.created_at,
            "verified_at": correction.verified_at,
        }

    def rows_as_of(self, as_of: str) -> list[dict[str, object]]:
        cutoff = _parse_rfc3339(as_of, field="fact_register_as_of")
        rows = [
            self._row(correction)
            for correction in self._store.list_all()
            if correction.created_at <= cutoff
        ]
        rows.sort(key=lambda row: str(row["correction_id"]))
        return rows


# ---------------------------------------------------------------------------
# Sponsor-pack adapter (LR-1 members; workstream-1 tracked artifacts)
# ---------------------------------------------------------------------------

#: Default claims rows: every claim resolves to a member the BB-grade
#: assembler ALWAYS includes for SPONSOR_BANK_PACK, so the in-ZIP artifact
#: check holds by construction. ``artifact_sha256`` is left for the assembler
#: to compute (it refuses a mismatch); no row cites a FactCorrection, so the
#: default pack carries only claims the build itself proves.
DEFAULT_SPONSOR_CLAIMS: tuple[dict[str, object], ...] = (
    {
        "claim": "Double-entry ledger proves balanced at the snapshot instant "
        "(trial_balance_assertion output packaged verbatim)",
        "artifact_path_in_zip": "trial-balance/assertion.json",
    },
    {
        "claim": "External-fact register snapshot: every externally-cited claim "
        "tracked with verification state (spec/16 LR-3 gate)",
        "artifact_path_in_zip": "fact-register/snapshot.json",
    },
    {
        "claim": "Five term-sheet items enumerated for bank confirmation "
        "(TCSA, H2H modes, VA range, MFS refunds, NPSB sub-participation)",
        "artifact_path_in_zip": "term-sheet-checklist.md",
    },
)

_TERM_SHEET_REQUIRED_ITEMS = ("Item (a)", "Item (b)", "Item (c)", "Item (d)", "Item (e)")


class TrackedSponsorPackAdapter:
    """SponsorPackPort over the tracked LR-1 sources.

    The checklist is read from the tracked markdown source and validated for
    the five term-sheet items at read time (refusal-first: a truncated source
    fails the export rather than shipping an incomplete checklist).
    """

    def __init__(
        self,
        *,
        term_sheet_path: Path = DEFAULT_TERM_SHEET_PATH,
        claims: Sequence[dict[str, object]] = DEFAULT_SPONSOR_CLAIMS,
    ) -> None:
        self._term_sheet_path = term_sheet_path
        self._claims = tuple(dict(row) for row in claims)

    def term_sheet_checklist_md(self) -> str:
        if not self._term_sheet_path.is_file():
            raise InvalidRequestError(
                f"term-sheet source missing: {self._term_sheet_path.name}",
                code="term_sheet_source_missing",
            )
        text = self._term_sheet_path.read_text(encoding="utf-8")
        missing = [item for item in _TERM_SHEET_REQUIRED_ITEMS if item not in text]
        if missing:
            raise InvalidRequestError(
                f"term-sheet checklist is missing required items: {missing}",
                code="term_sheet_incomplete",
            )
        return text

    def claims_rows(self) -> list[dict[str, object]]:
        return [dict(row) for row in self._claims]
