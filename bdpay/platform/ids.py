"""Content-addressed entity IDs per spec/00-conventions.md §3 (binding).

All IDs are ``<prefix>_<sha256_canonical(payload)[:24]>``. Deterministic by
construction: the same payload always yields the same ID. Random UUIDs are
banned across the entire codebase (scripts/check_bans.py enforces); wall-clock
reads never appear in an ID path — any timestamp inside a payload arrives via
the injectable clock and serializes through ``canonical_json``.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical

__all__ = ["HASH_LENGTH", "PREFIXES", "IdPrefixError", "make_id"]

#: Exact prefix table from spec/00-conventions.md §3.
PREFIXES: frozenset[str] = frozenset(
    {
        "mrch",  # Merchant
        "cust",  # Customer
        "part",  # Participant (PSO)
        "pi",  # PaymentIntent
        "pa",  # PaymentAttempt
        "rfnd",  # Refund
        "acct",  # Account
        "je",  # JournalEntry
        "post",  # Posting
        "chn",  # LedgerChainEntry
        "aud",  # AuditEvent
        "sbatch",  # SettlementBatch
        "swin",  # SettlementWindow (PSO)
        "sinst",  # SettlementInstruction
        "recon",  # ReconciliationRecord
        "rexc",  # ReconciliationException
        "posn",  # PositionAccount (PSO)
        "ndc",  # NetDebitCap (PSO)
        "kyc",  # KycRecord
        "kyb",  # KybRecord
        "aml",  # AmlAlert
        "str",  # StrReport
        "ctr",  # CtrAggregation
        "sanc",  # SanctionsHit
        "pack",  # RulePack
        "cres",  # ConnectorResult
        "idem",  # IdempotencyKey
        "obx",  # OutboxEvent
        "tcsa",  # TcsaSnapshot
        "appr",  # ApprovalRequest
        "ins",  # PaymentInstruction (connector boundary)
        # Additive prefixes registered by spec/10-connector-sdk-and-registry.md
        # ("New ID prefixes registered by this spec", Entities table), folded
        # into the canonical table per SPEC_ERRATA.md E18.
        "creg",  # ConnectorRegistration (spec/10)
        "cmode",  # ConnectorModeChange (spec/10)
        "chlth",  # ConnectorHealthSample (spec/10)
        "cbrk",  # CircuitBreakerState (spec/10)
        "winb",  # WebhookInbound (spec/10)
        "simsc",  # SimulatorScenario (spec/10)
        "certr",  # CertificationRun (spec/10)
        # Additive prefix registered by spec/16-launch-readiness.md (LR-3
        # FactCorrection register), folded in per the SPEC_ERRATA E18 pattern.
        "fcor",  # FactCorrection (spec/16)
    }
)

#: Truncated hex length of the sha256 digest embedded in every ID.
HASH_LENGTH = 24


class IdPrefixError(ValueError):
    """Raised when ``make_id`` is called with a prefix outside the spec 00 §3 table."""


def make_id(prefix: str, payload: dict) -> str:
    """Return ``<prefix>_<sha256_canonical(payload)[:24]>``.

    ``prefix`` must be a member of :data:`PREFIXES`; ``payload`` must be a
    ``dict`` serializable by ``canonical_json`` (floats, sets, and naive
    datetimes are rejected there).
    """
    if not isinstance(prefix, str):
        raise IdPrefixError(f"prefix must be str, got {type(prefix).__name__}")
    if prefix not in PREFIXES:
        raise IdPrefixError(
            f"unknown id prefix {prefix!r}; allowed prefixes are the spec 00 §3 table"
        )
    if not isinstance(payload, dict):
        raise TypeError(f"payload must be dict, got {type(payload).__name__}")
    return f"{prefix}_{sha256_canonical(payload)[:HASH_LENGTH]}"
