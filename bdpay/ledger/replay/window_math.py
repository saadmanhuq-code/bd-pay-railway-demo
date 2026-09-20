"""Pure netting/window math — spec/19 PSO-2 algorithm steps 1b, 3, 4 + replay steps 1-3.

Every function here is a pure function of its inputs: no clock, no
randomness, no I/O. Integer paisa arithmetic only — ``canonical_json``
rejects floats by construction, and the amount guards below reject
bool/float before any arithmetic happens.

D-19-7 (binding): the canonical result document contains LEDGER FACTS ONLY.
``computed_at`` and every other clock read live in row columns, never inside
the hashed document. :func:`build_result_document` enforces this closed key
set structurally.

Determinism contract note (errata E-S19R-03): the two chain-anchor fields
(``chain_index_at_compute``, ``chain_hash_at_compute``) are attestations of
the specific ledger history and are therefore insert-order-DEPENDENT by
design of the hash chain. The spec's seeded-order-independence contract
(step 5 / PSO-CERT-08) is asserted over ``inputs_hash`` and the
anchor-stripped core document (:func:`result_core`); full-document byte
equality is the replay-vs-stored contract on the SAME ledger.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from bdpay.ledger.replay.errors import ReplayError
from bdpay.ledger.replay.ids import make_replay_id
from bdpay.ledger.replay.types import AttributedEntry, PositionDerivation
from bdpay.platform.canonical import canonical_json, sha256_canonical

__all__ = [
    "RESULT_DOCUMENT_KEYS",
    "SUITE_VERSION",
    "build_instruction_plan",
    "build_result_document",
    "derive_positions",
    "first_difference",
    "inputs_hash_for",
    "make_netting_run_id",
    "obligations_from_positions",
    "position_list",
    "result_core",
    "result_hash_for",
    "total_gross_minor",
    "total_net_pay_minor",
    "zero_sum_delta",
]

SUITE_VERSION = "netting-v1"

#: The closed key set of the canonical result document (spec/19 step 4).
RESULT_DOCUMENT_KEYS: frozenset[str] = frozenset(
    {
        "settlement_window_id",
        "dns_session",
        "window_date",
        "suite",
        "chain_index_at_compute",
        "chain_hash_at_compute",
        "positions",
        "obligations",
        "total_gross_minor",
        "total_net_pay_minor",
        "instruction_plan",
    }
)

#: The order-dependent attestation fields excluded from the order-independence
#: contract (E-S19R-03).
_ANCHOR_KEYS = ("chain_index_at_compute", "chain_hash_at_compute")


def _require_int_paisa(value: object, where: str) -> int:
    """Reject bool/float/str before arithmetic (spec/00 §6)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReplayError(f"{where} must be int paisa, got {type(value).__name__}")
    return value


def derive_positions(
    entries: Sequence[AttributedEntry],
    *,
    participant_for_account: Callable[[str], str | None],
) -> tuple[PositionDerivation, ...]:
    """Algorithm step 1b verbatim: re-derive per-participant net positions.

    CREDIT counts +, DEBIT counts -, summed over postings whose account is a
    ``PARTICIPANT_POSITION`` account (resolved via ``participant_for_account``
    — returns the owning participant_id or ``None`` for non-position
    accounts), grouped by participant, ordered ``participant_id ASC`` (the
    deterministic order). Input ordering does not matter: sums are
    order-independent.
    """
    nets: dict[str, int] = {}
    contributing: dict[str, list[str]] = {}
    for attributed in entries:
        for posting in attributed.postings:
            participant_id = participant_for_account(posting.account_id)
            if participant_id is None:
                continue
            amount = _require_int_paisa(posting.amount_minor, "posting amount_minor")
            delta = amount if posting.side == "CREDIT" else -amount
            nets[participant_id] = nets.get(participant_id, 0) + delta
            seen = contributing.setdefault(participant_id, [])
            if attributed.entry.entry_id not in seen:
                seen.append(attributed.entry.entry_id)
    return tuple(
        PositionDerivation(
            participant_id=participant_id,
            net_position_minor=nets[participant_id],
            contributing_entry_ids=tuple(sorted(contributing[participant_id])),
        )
        for participant_id in sorted(nets)
    )


def position_list(positions: Sequence[PositionDerivation]) -> list[dict[str, object]]:
    """The document/`inputs_hash` shape: ordered ``{participant_id, net_position_minor}``."""
    ordered = sorted(positions, key=lambda p: p.participant_id)
    return [
        {
            "participant_id": p.participant_id,
            "net_position_minor": _require_int_paisa(p.net_position_minor, "net_position_minor"),
        }
        for p in ordered
    ]


def inputs_hash_for(positions: Sequence[PositionDerivation]) -> str:
    """``inputs_hash = sha256_canonical(ordered position list)`` (step 4)."""
    return sha256_canonical(position_list(positions))


def make_netting_run_id(settlement_window_id: str, inputs_hash: str) -> str:
    """Content-addressed ``nrun_`` id (spec/19 Entities; E18-class shim)."""
    return make_replay_id(
        "nrun",
        {"settlement_window_id": settlement_window_id, "inputs_hash": inputs_hash},
    )


def zero_sum_delta(positions: Sequence[PositionDerivation]) -> int:
    """``SUM(net_position_minor)`` — must be exactly 0 (step 2 invariant)."""
    return sum(_require_int_paisa(p.net_position_minor, "net_position_minor") for p in positions)


def obligations_from_positions(
    positions: Sequence[PositionDerivation],
) -> list[dict[str, object]]:
    """Algorithm step 3: pure function of the ordered position list.

    net < 0 -> PAY (obligation = -net); net > 0 -> RECEIVE (obligation = net);
    net == 0 -> FLAT (obligation = 0). Integer paisa only.
    """
    obligations: list[dict[str, object]] = []
    for p in sorted(positions, key=lambda p: p.participant_id):
        net = _require_int_paisa(p.net_position_minor, "net_position_minor")
        if net < 0:
            direction, obligation = "PAY", -net
        elif net > 0:
            direction, obligation = "RECEIVE", net
        else:
            direction, obligation = "FLAT", 0
        obligations.append(
            {
                "participant_id": p.participant_id,
                "net_position_minor": net,
                "direction": direction,
                "obligation_minor": obligation,
            }
        )
    return obligations


def total_gross_minor(
    entries: Sequence[AttributedEntry],
    *,
    participant_for_account: Callable[[str], str | None],
) -> int:
    """Sum of |all position movements| — the window volume (spec/19 columns)."""
    total = 0
    for attributed in entries:
        for posting in attributed.postings:
            if participant_for_account(posting.account_id) is None:
                continue
            total += _require_int_paisa(posting.amount_minor, "posting amount_minor")
    return total


def total_net_pay_minor(obligations: Sequence[Mapping[str, object]]) -> int:
    """Sum of |PAY obligations| (== sum of RECEIVE obligations when the
    zero-sum invariant holds; the invariant itself is asserted by the step-2
    integrity check / :func:`zero_sum_delta`, never silently here — a broken
    sum must surface as a named ``ZERO_SUM_VIOLATION`` verdict, not an
    accessor exception)."""
    return sum(
        _require_int_paisa(o["obligation_minor"], "obligation_minor")
        for o in obligations
        if o["direction"] == "PAY"
    )


def build_instruction_plan(
    obligations: Sequence[Mapping[str, object]],
    *,
    rail: str,
    settlement_account_token: Callable[[str], str],
) -> list[dict[str, object]]:
    """Step-4 instruction plan rows: PAY carries ``sender_ref`` (the net
    debtor's settlement account is debited), RECEIVE carries
    ``beneficiary_ref``; FLAT participants get no instruction (D-19-4:
    ``settlement_instruction_id`` NULL for FLAT)."""
    plan: list[dict[str, object]] = []
    for obligation in obligations:
        direction = str(obligation["direction"])
        if direction == "FLAT":
            continue
        participant_id = str(obligation["participant_id"])
        amount = _require_int_paisa(obligation["obligation_minor"], "obligation_minor")
        row: dict[str, object] = {
            "participant_id": participant_id,
            "direction": direction,
            "amount_minor": amount,
            "rail": rail,
        }
        ref_key = "sender_ref" if direction == "PAY" else "beneficiary_ref"
        row[ref_key] = settlement_account_token(participant_id)
        plan.append(row)
    return plan


def build_result_document(
    *,
    settlement_window_id: str,
    dns_session: str,
    window_date: str,
    chain_index_at_compute: int,
    chain_hash_at_compute: str,
    positions: Sequence[PositionDerivation],
    obligations: Sequence[Mapping[str, object]],
    gross_minor: int,
    net_pay_minor: int,
    instruction_plan: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """The canonical result document (spec/19 step 4) — ledger facts ONLY.

    D-19-7: no clock value may appear in this document. The key set is closed
    (``RESULT_DOCUMENT_KEYS``); ``window_date`` is the ISO ``YYYY-MM-DD``
    string form.
    """
    document: dict[str, object] = {
        "settlement_window_id": settlement_window_id,
        "dns_session": dns_session,
        "window_date": window_date,
        "suite": SUITE_VERSION,
        "chain_index_at_compute": chain_index_at_compute,
        "chain_hash_at_compute": chain_hash_at_compute,
        "positions": position_list(positions),
        "obligations": [dict(o) for o in obligations],
        "total_gross_minor": _require_int_paisa(gross_minor, "total_gross_minor"),
        "total_net_pay_minor": _require_int_paisa(net_pay_minor, "total_net_pay_minor"),
        "instruction_plan": [dict(row) for row in instruction_plan],
    }
    if set(document) != RESULT_DOCUMENT_KEYS:
        raise ReplayError("result document key set drifted from the spec/19 step-4 shape")
    canonical_json(document)  # rejects floats / naive datetimes by construction
    return document


def result_hash_for(document: Mapping[str, object]) -> str:
    """``result_hash = sha256_canonical(result document)`` (step 4)."""
    return sha256_canonical(dict(document))


def result_core(document: Mapping[str, object]) -> dict[str, object]:
    """The anchor-stripped core: the order-independence surface (E-S19R-03)."""
    return {key: value for key, value in dict(document).items() if key not in _ANCHOR_KEYS}


def first_difference(
    expected_bytes: bytes,
    actual_bytes: bytes,
    expected_document: Mapping[str, object],
    actual_document: Mapping[str, object],
) -> dict[str, object] | None:
    """First differing byte offset + field path (spec/19 replay step 3)."""
    if expected_bytes == actual_bytes:
        return None
    limit = min(len(expected_bytes), len(actual_bytes))
    offset = next(
        (i for i in range(limit) if expected_bytes[i] != actual_bytes[i]),
        limit,
    )
    return {
        "kind": "RESULT_BYTES_MISMATCH",
        "byte_offset": offset,
        "field_path": _first_field_diff(expected_document, actual_document, path="$"),
        "expected_sha256": sha256_canonical(dict(expected_document)),
        "actual_sha256": sha256_canonical(dict(actual_document)),
    }


def _first_field_diff(expected: object, actual: object, *, path: str) -> str:
    """Depth-first (sorted-key) walk naming the first differing field path."""
    if isinstance(expected, Mapping) and isinstance(actual, Mapping):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected or key not in actual:
                return f"{path}.{key}"
            if expected[key] != actual[key]:
                return _first_field_diff(expected[key], actual[key], path=f"{path}.{key}")
        return path
    if (
        isinstance(expected, Sequence)
        and isinstance(actual, Sequence)
        and not isinstance(expected, str | bytes)
        and not isinstance(actual, str | bytes)
    ):
        for index in range(min(len(expected), len(actual))):
            if expected[index] != actual[index]:
                return _first_field_diff(expected[index], actual[index], path=f"{path}[{index}]")
        if len(expected) != len(actual):
            return f"{path}[{min(len(expected), len(actual))}]"
        return path
    return path
