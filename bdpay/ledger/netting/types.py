"""Netting entities, FSM transition table, and pure computation helpers.

Binding sources: spec/19 §Entities (NettingRun ``nrun``, NettingObligation
``nobl``), §State machines FSM 3 (NettingRun), §Data model 0102 DDL, and
§"The netting algorithm" steps 3–4 — the obligation fold and the canonical
result document are PURE functions of the ordered position list so the
replay service (PSO-3) re-runs them byte-identically.

Determinism contract (D-19-7, pinned by tests): the hashed result document
contains LEDGER FACTS ONLY — no clock reads, no randomness; ``computed_at``
lives in row columns, never inside the hashed bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from bdpay.ledger.netting.errors import NettingError
from bdpay.ledger.netting.ids_ext import make_netting_id
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.ids import make_id

__all__ = [
    "NETTING_RESULT_SUITE",
    "NETTING_RUN_STATES",
    "NETTING_RUN_TERMINAL_STATES",
    "NETTING_RUN_TRANSITIONS",
    "POSITION_FACT_ENTRY_TYPES",
    "PSO_INSTRUCTION_KIND",
    "PSO_INSTRUCTION_TRANSITIONS",
    "NettingObligationRow",
    "NettingRunRow",
    "ParticipantPosition",
    "PsoNettingInstructionRow",
    "build_result_document",
    "compute_obligations",
    "instruction_connector_ref",
    "netting_inputs_hash",
    "netting_run_id_for",
    "participant_position_account_id",
    "result_pointer_for",
]

PRODUCER = "settlement-engine@1"

#: The canonical result-document suite tag (spec/19 algorithm step 4).
NETTING_RESULT_SUITE = "netting-v1"

#: D-19-6 attribution — journal entry types that are POSITION FACTS for the
#: per-window derivation. ``settlement_batch_close`` is the netting OUTPUT
#: (written at seal time) and is therefore excluded from the derivation input
#: (errata N19-1); ``payment_reversed`` rows are the D-19-2 unwind offsets
#: and ARE inputs to the re-net.
POSITION_FACT_ENTRY_TYPES: frozenset[str] = frozenset({"position_updated", "payment_reversed"})

PSO_INSTRUCTION_KIND = "PSO_NETTING"

# --- FSM 3: NettingRun (spec/19) ---------------------------------------------

NETTING_RUN_STATES: frozenset[str] = frozenset(
    {"COMPUTING", "PASSED", "FAILED_INTEGRITY", "SUPERSEDED"}
)
NETTING_RUN_TERMINAL_STATES: frozenset[str] = frozenset(
    {"PASSED", "FAILED_INTEGRITY", "SUPERSEDED"}
)
# PASSED is terminal for normal flow; the ONE listed exception is the D-19-2
# unwind row PASSED --[superseded_by_unwind]--> SUPERSEDED.

#: (from_state, trigger) -> tuple of permitted to-states. Refusal-first: any
#: pair not in this table is DENIED.
NETTING_RUN_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("COMPUTING", "result_sealed"): ("PASSED",),
    ("COMPUTING", "integrity_failure"): ("FAILED_INTEGRITY",),
    ("PASSED", "superseded_by_unwind"): ("SUPERSEDED",),
}

#: PSO netting legs follow the spec/04 FSM 2 instruction lifecycle unchanged
#: (D-19-4: "the instruction lifecycle is rail-facing"), plus the spec/19
#: unwind row: "its non-terminal instructions CANCELLED" — which needs a
#: SUBMITTED -> CANCELLED edge spec/04 does not carry (spec/04's only
#: cancellation is from QUEUED). Additive to THIS table only; the spec/04
#: table is untouched. Errata N19-6 records the addition.
PSO_INSTRUCTION_TRANSITIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("QUEUED", "batch_dispatched"): ("SUBMITTED",),
    ("QUEUED", "batch_cancelled"): ("CANCELLED",),
    ("SUBMITTED", "rail_confirmed"): ("SETTLED",),
    ("SUBMITTED", "rail_returned"): ("RETURNED",),
    ("SUBMITTED", "rail_hard_rejected"): ("FAILED",),
    ("SUBMITTED", "timeout_on_batch_fail"): ("FAILED",),
    ("SUBMITTED", "batch_cancelled"): ("CANCELLED",),  # spec/19 unwind row
}

PSO_INSTRUCTION_TERMINAL_STATES: frozenset[str] = frozenset(
    {"SETTLED", "RETURNED", "CANCELLED", "FAILED"}
)


# --- rows ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParticipantPosition:
    """One participant's net position in a window (signed integer paisa)."""

    participant_id: str
    net_position_minor: int


@dataclass(frozen=True, slots=True)
class NettingRunRow:
    """``netting_runs`` row (spec/19 0102 DDL; append-only results)."""

    netting_run_id: str
    settlement_window_id: str
    status: str
    participant_count: int
    inputs_hash: str
    result_hash: str | None
    result_pointer: str | None
    total_gross_minor: int | None
    total_net_pay_minor: int | None
    excluded_participant_id: str | None
    supersedes_netting_run_id: str | None
    failure_detail: dict | None
    computed_at: datetime | None  # row column; NEVER inside the hashed result (D-19-7)
    created_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class NettingObligationRow:
    """``netting_obligations`` row — one participant's net obligation."""

    obligation_id: str
    netting_run_id: str
    participant_id: str
    net_position_minor: int  # signed: negative = net debtor
    direction: str  # PAY | RECEIVE | FLAT
    obligation_minor: int  # abs(net_position_minor); >= 0
    settlement_instruction_id: str | None  # NULL for FLAT
    schema_version: int = 1

    def __post_init__(self) -> None:
        ok = (
            (self.direction == "PAY" and self.net_position_minor < 0
             and self.obligation_minor == -self.net_position_minor)
            or (self.direction == "RECEIVE" and self.net_position_minor > 0
                and self.obligation_minor == self.net_position_minor)
            or (self.direction == "FLAT" and self.net_position_minor == 0
                and self.obligation_minor == 0)
        )
        if not ok:
            raise NettingError(
                f"nobl_direction_sign violated for {self.participant_id}: "
                f"direction={self.direction} net={self.net_position_minor} "
                f"obligation={self.obligation_minor}"
            )


@dataclass(frozen=True, slots=True)
class PsoNettingInstructionRow:
    """A ``settlement_instructions`` row in the D-19-4 ``PSO_NETTING`` shape.

    ``payment_intent_id``/``merchant_id`` are NULL by construction;
    ``fee_minor=0``, ``net_payout_minor=amount_minor``, ``mcc_category='OTHER'``,
    and both release instants equal the window settlement time. The spec/04
    FSM 2 lifecycle applies unchanged (rail-facing).
    """

    instruction_id: str  # sinst_<...>
    netting_run_id: str
    participant_id: str
    direction: str  # DEBIT (net debtor pays) | CREDIT (net creditor receives)
    amount_minor: int
    currency: str
    rail: str
    counterparty_account_token: str  # tokenized participant settlement account
    high_value: bool
    earliest_release_at: datetime
    latest_release_at: datetime
    status: str
    connector_ref: str
    rail_transaction_id: str | None
    created_at: datetime
    submitted_at: datetime | None
    settled_at: datetime | None
    failed_at: datetime | None
    cancelled_at: datetime | None
    produced_by: str = PRODUCER
    instruction_kind: str = PSO_INSTRUCTION_KIND
    fee_minor: int = 0
    mcc_category: str = "OTHER"
    schema_version: int = 1

    @property
    def net_payout_minor(self) -> int:
        """D-19-4 shape: net equals gross (fee_minor is structurally zero)."""
        return self.amount_minor


# --- pure functions (spec/19 algorithm steps 3–4) ------------------------------


def compute_obligations(
    positions: list[ParticipantPosition],
) -> list[tuple[str, str, int]]:
    """Step 3 — the obligation fold, a pure function of the ORDERED positions.

    Returns ``[(participant_id, direction, obligation_minor), ...]`` in
    participant_id ASC order. Integer paisa arithmetic only; the zero-sum
    invariant is asserted by the caller (step 2), not silently corrected here.
    """
    out: list[tuple[str, str, int]] = []
    for pos in sorted(positions, key=lambda p: p.participant_id):
        if isinstance(pos.net_position_minor, bool) or not isinstance(
            pos.net_position_minor, int
        ):
            raise NettingError(
                f"net_position_minor must be int paisa for {pos.participant_id}"
            )
        net = pos.net_position_minor
        if net < 0:
            out.append((pos.participant_id, "PAY", -net))
        elif net > 0:
            out.append((pos.participant_id, "RECEIVE", net))
        else:
            out.append((pos.participant_id, "FLAT", 0))
    return out


def netting_inputs_hash(positions: list[ParticipantPosition]) -> str:
    """Step 4 — ``sha256_canonical`` of the ordered position list."""
    ordered = sorted(positions, key=lambda p: p.participant_id)
    return sha256_canonical(
        [
            {"participant_id": p.participant_id, "net_position_minor": p.net_position_minor}
            for p in ordered
        ]
    )


def netting_run_id_for(settlement_window_id: str, inputs_hash: str) -> str:
    """Content-addressed ``nrun_`` id (idempotent double-fire by construction)."""
    return make_netting_id(
        "nrun", {"settlement_window_id": settlement_window_id, "inputs_hash": inputs_hash}
    )


def obligation_id_for(netting_run_id: str, participant_id: str) -> str:
    return make_netting_id(
        "nobl", {"netting_run_id": netting_run_id, "participant_id": participant_id}
    )


def instruction_connector_ref(netting_run_id: str, participant_id: str) -> str:
    """Content-addressed connector_ref (spec/19 step 6) — the rail dedupe key."""
    digest = sha256_canonical(
        {
            "kind": PSO_INSTRUCTION_KIND,
            "netting_run_id": netting_run_id,
            "participant_id": participant_id,
        }
    )
    return f"psonet_{digest[:24]}"


def instruction_id_for(netting_run_id: str, participant_id: str) -> str:
    """Content-addressed ``sinst_`` id for a netting leg (canonical prefix)."""
    return make_id(
        "sinst",
        {
            "instruction_kind": PSO_INSTRUCTION_KIND,
            "netting_run_id": netting_run_id,
            "participant_id": participant_id,
        },
    )


def participant_position_account_id(participant_id: str) -> str:
    """The content-addressed ledger account id of a participant's position.

    Mirrors ``LedgerService.create_account`` derivation for the canonical
    PSO position account: subtype ``PARTICIPANT_POSITION``, BDT, no purpose
    tag. Deterministic resolution — no query needed (pinned by test).
    """
    return make_id(
        "acct",
        {
            "owner_id": participant_id,
            "account_subtype": "PARTICIPANT_POSITION",
            "currency": "BDT",
            "purpose_tag": None,
        },
    )


def result_pointer_for(settlement_window_id: str, result_hash: str) -> str:
    """Deterministic object-store pointer for the canonical netting file."""
    return f"netting://{settlement_window_id}/{result_hash}.json"


def build_result_document(
    *,
    settlement_window_id: str,
    dns_session: str,
    window_date: date,
    chain_index_at_compute: int,
    chain_hash_at_compute: str,
    positions: list[ParticipantPosition],
    obligations: list[tuple[str, str, int]],
    total_gross_minor: int,
    total_net_pay_minor: int,
    instruction_rail: str,
    account_token_for: dict[str, str],
) -> dict:
    """Step 4 — the canonical netting result document (ledger facts ONLY).

    No clock reads, no randomness (D-19-7). ``window_date`` serializes as the
    ISO date string because canonical JSON admits datetimes but not bare dates
    (errata N19-1). ``account_token_for`` maps participant -> tokenized
    settlement-account reference for the instruction plan (PAY legs carry
    ``sender_ref``; RECEIVE legs carry ``beneficiary_ref``; FLAT emits no
    instruction).
    """
    ordered = sorted(positions, key=lambda p: p.participant_id)
    net_by_participant = {p.participant_id: p.net_position_minor for p in ordered}
    plan: list[dict] = []
    for participant_id, direction, amount_minor in obligations:
        if direction == "FLAT":
            continue
        token = account_token_for.get(participant_id)
        if not token:
            raise NettingError(
                f"no settlement-account token for participant {participant_id}"
            )
        leg: dict = {
            "participant_id": participant_id,
            "direction": direction,
            "amount_minor": amount_minor,
            "rail": instruction_rail,
        }
        if direction == "PAY":
            leg["sender_ref"] = token
        else:
            leg["beneficiary_ref"] = token
        plan.append(leg)
    # Obligation rows carry net_position_minor alongside the absolute
    # obligation — the EXACT shape the WINDOW_REPLAY recomputation rebuilds
    # (bdpay.ledger.replay.window_math.obligations_from_positions), so the
    # stored file and the replay bytes can be identical.
    return {
        "settlement_window_id": settlement_window_id,
        "dns_session": dns_session,
        "window_date": window_date.isoformat(),
        "suite": NETTING_RESULT_SUITE,
        "chain_index_at_compute": chain_index_at_compute,
        "chain_hash_at_compute": chain_hash_at_compute,
        "positions": [
            {"participant_id": p.participant_id, "net_position_minor": p.net_position_minor}
            for p in ordered
        ],
        "obligations": [
            {
                "participant_id": participant_id,
                "net_position_minor": net_by_participant[participant_id],
                "direction": direction,
                "obligation_minor": amount_minor,
            }
            for participant_id, direction, amount_minor in obligations
        ],
        "total_gross_minor": total_gross_minor,
        "total_net_pay_minor": total_net_pay_minor,
        "instruction_plan": plan,
    }
