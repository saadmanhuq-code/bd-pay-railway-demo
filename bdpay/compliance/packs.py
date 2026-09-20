"""RulePack model, seed rules, signing, and the verified activation gate.

spec/06 sections "Rule Engine: Monitoring Rule Definitions" and "RulePack
Activation". A pack is a JSON artifact ``{manifest, rules, signature}``; only
Ed25519-signed packs whose signer is active in the signing key registry may be
loaded, and exactly one pack is ACTIVE at a time (the previous pack is
SUPERSEDED, never deleted — alerts keep the pack id they were raised under).

ID circularity resolution (SPEC_ERRATA-LANE-A-compliance.md E-C2): spec/06
defines ``pack_id = pack_<sha256(canonical_json(manifest))[:24]>`` while the
manifest itself contains ``pack_id`` — the hash cannot include its own
output. Binding form here: ``pack_id`` and ``pack_manifest_hash`` are computed
over the manifest WITHOUT the ``pack_id`` and ``pack_manifest_hash`` fields.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from bdpay.compliance.signing import SigningKeyError, SigningKeyRegistry, sign_content_hash
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import InvalidRequestError
from bdpay.platform.ids import make_id
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = [
    "InMemoryRulePackStore",
    "RulePack",
    "RulePackLoader",
    "RulePackStore",
    "build_signed_pack",
    "evaluate_rule",
    "seed_rules",
    "validate_rules",
]

PRODUCER = "aml-monitor@1.0.0"

#: Canonical seed rule ids (spec/06) — must not be renamed.
SEED_RULE_IDS = (
    "rule_cdd_cash_identification",
    "rule_wire_originator_verification",
    "rule_ctr_threshold",
    "rule_str_suspicion_any_amount",
    "rule_structuring_detection_3day",
    "rule_velocity_24h_spike",
    "rule_mule_account_pattern",
    "rule_sanctions_confirmed_hit",
)

_VALID_MODALITIES = ("obligation", "prohibition")
_PACK_STATUSES = ("ACTIVE", "SUPERSEDED", "REJECTED")


def _signature_error(detail: str) -> InvalidRequestError:
    return InvalidRequestError(
        f"rule pack rejected: {detail}", code="rule_pack_signature_invalid"
    )


# ---------------------------------------------------------------------------
# Seed rules (spec/06 "Seed rule definitions (BD-PSP/PSO v1 pack)" — verbatim
# semantics; money in integer paisa; threshold_ref values resolve against the
# YAML thresholds artifact at evaluation time, never hardcoded here)
# ---------------------------------------------------------------------------


def seed_rules() -> dict[str, dict]:
    """The BD-PSP/PSO v1 seed rule definitions, keyed by canonical rule_id."""
    bfiu_guidance = "https://www.bfiu.org.bd/index.php/legislation/guidanceNote"
    bfiu_faq = "https://www.bfiu.org.bd/index.php/others/faq"
    payatlas = "https://payatlas.com/countries/bangladesh-bd"
    return {
        "rule_cdd_cash_identification": {
            "rule_id": "rule_cdd_cash_identification",
            "rule_family_id": "family_cdd",
            "modality": "obligation",
            "action": "verify_customer_identity_for_cash",
            "actor": "psp_pso",
            "object": "cash transaction identity verification",
            "authority_class": "circular",
            "certification_tier": "legal",
            "conditions": [
                {
                    "condition_id": "cond_cash_high",
                    "left": "transaction_amount_minor",
                    "operator": ">",
                    "right": 50_000_000,
                    "value_type": "integer_paisa",
                },
                {
                    "condition_id": "cond_channel_cash",
                    "left": "payment_channel",
                    "operator": "in",
                    "right": ["CASH_AGENT", "CASH_ATM"],
                    "value_type": "enum_list",
                },
                {
                    "condition_id": "cond_not_verified",
                    "left": "customer_cdd_verified",
                    "operator": "==",
                    "right": False,
                    "value_type": "bool",
                },
            ],
            "outcome": {
                "outcome_id": "out_cdd_cash_breach",
                "outcome_type": "obligation",
                "description": (
                    "PSP/PSOs shall verify customer identity for any single cash "
                    "transaction exceeding BDT 500,000 (50,000,000 paisa)."
                ),
                "source_url": bfiu_guidance,
            },
            "status": "legally_certified",
        },
        "rule_wire_originator_verification": {
            "rule_id": "rule_wire_originator_verification",
            "rule_family_id": "family_aml_cft",
            "modality": "prohibition",
            "action": "process_wire_without_originator",
            "actor": "psp_pso",
            "object": "high-value wire transfer",
            "authority_class": "circular",
            "certification_tier": "legal",
            "condition": {
                "kind": "all",
                "children": [
                    {
                        "kind": "atom",
                        "fact_key": "transaction_amount_minor",
                        "operator": ">",
                        "value": 100_000_000,
                        "value_type": "integer_paisa",
                    },
                    {
                        "kind": "atom",
                        "fact_key": "payment_method",
                        "operator": "in",
                        "value": ["NPSB_IBFT", "BEFTN_CREDIT", "RTGS"],
                        "value_type": "enum_list",
                    },
                    {
                        "kind": "any",
                        "children": [
                            {
                                "kind": "atom",
                                "fact_key": "originator_identity_verified",
                                "operator": "==",
                                "value": False,
                                "value_type": "bool",
                            },
                            {
                                "kind": "atom",
                                "fact_key": "beneficiary_identity_verified",
                                "operator": "==",
                                "value": False,
                                "value_type": "bool",
                            },
                        ],
                    },
                ],
            },
            "outcome": {
                "outcome_id": "out_wire_originator_breach",
                "outcome_type": "prohibition",
                "description": (
                    "No PSP/PSO shall process a wire transfer exceeding BDT 1,000,000 "
                    "(100,000,000 paisa) without verified originator and beneficiary "
                    "information."
                ),
                "source_url": bfiu_guidance,
            },
            "status": "legally_certified",
        },
        "rule_ctr_threshold": {
            "rule_id": "rule_ctr_threshold",
            "rule_family_id": "family_ctr",
            "modality": "obligation",
            "action": "file_ctr_report",
            "actor": "psp_pso",
            "object": "cash transaction report",
            "authority_class": "guidance",
            "certification_tier": "legal",
            "conditions": [
                {
                    "condition_id": "cond_daily_cash_high",
                    "left": "daily_cash_aggregate_minor",
                    "operator": ">=",
                    "right": 100_000_000,
                    "value_type": "integer_paisa",
                },
                {
                    "condition_id": "cond_ctr_not_filed",
                    "left": "ctr_filed_today",
                    "operator": "==",
                    "right": False,
                    "value_type": "bool",
                },
            ],
            "outcome": {
                "outcome_id": "out_ctr_breach",
                "outcome_type": "obligation",
                "description": (
                    "PSP/PSOs must file a Cash Transaction Report for any account with "
                    "cumulative daily cash movements of BDT 1,000,000 (100,000,000 "
                    "paisa) or more."
                ),
                "source_url": bfiu_faq,
            },
            "status": "legally_certified",
        },
        "rule_str_suspicion_any_amount": {
            "rule_id": "rule_str_suspicion_any_amount",
            "rule_family_id": "family_str",
            "modality": "obligation",
            "action": "file_str_report",
            "actor": "psp_pso",
            "object": "suspicious transaction",
            "authority_class": "regulation",
            "certification_tier": "legal",
            "conditions": [
                {
                    "condition_id": "cond_suspicious",
                    "left": "transaction_suspicious_flagged",
                    "operator": "==",
                    "right": True,
                    "value_type": "bool",
                },
                {
                    "condition_id": "cond_str_not_filed",
                    "left": "str_filed",
                    "operator": "==",
                    "right": False,
                    "value_type": "bool",
                },
            ],
            "outcome": {
                "outcome_id": "out_str_suspicion_breach",
                "outcome_type": "obligation",
                "description": (
                    "Suspicious transactions must be reported to BFIU immediately upon "
                    "detection, regardless of transaction amount."
                ),
                "source_url": bfiu_guidance,
            },
            "note": (
                "STR trigger is suspicion-based ONLY. Amount thresholds (BDT 500k CDD, "
                "BDT 1M wire) are CDD/originator-verification gates, NOT STR triggers."
            ),
            "status": "legally_certified",
        },
        "rule_structuring_detection_3day": {
            "rule_id": "rule_structuring_detection_3day",
            "rule_family_id": "family_structuring",
            "modality": "obligation",
            "action": "raise_aml_alert_structuring",
            "actor": "psp_pso",
            "object": "structuring pattern",
            "authority_class": "guidance",
            "certification_tier": "legal",
            "conditions": [
                {
                    "condition_id": "cond_72h_count_high",
                    "left": "txn_count_72h_below_threshold",
                    "operator": ">=",
                    "right": 3,
                    "value_type": "integer",
                },
                {
                    "condition_id": "cond_72h_sum_high",
                    "left": "txn_sum_72h_minor",
                    "operator": ">=",
                    "right": 100_000_000,
                    "value_type": "integer_paisa",
                },
                {
                    "condition_id": "cond_each_below_threshold",
                    "left": "max_single_txn_72h_minor",
                    "operator": "<",
                    "right": 100_000_000,
                    "value_type": "integer_paisa",
                },
            ],
            "parameters": {
                "window_hours": 72,
                "min_txn_count": 3,
                "aggregate_threshold_minor": 100_000_000,
            },
            "outcome": {
                "outcome_id": "out_structuring_alert",
                "outcome_type": "obligation",
                "description": (
                    "Raise an AML alert when 3 or more transactions below BDT 1,000,000 "
                    "(100,000,000 paisa) are detected within a 72-hour rolling window "
                    "with an aggregate sum at or above the CTR threshold."
                ),
                "source_url": payatlas,
            },
            "status": "legally_certified",
        },
        "rule_velocity_24h_spike": {
            "rule_id": "rule_velocity_24h_spike",
            "rule_family_id": "family_velocity",
            "modality": "obligation",
            "action": "raise_aml_alert_velocity",
            "actor": "psp_pso",
            "object": "velocity anomaly",
            "authority_class": "guidance",
            "certification_tier": "engineering",
            "conditions": [
                {
                    "condition_id": "cond_velocity_count_or_amount",
                    "operator": "any_of",
                    "children": [
                        {
                            "left": "txn_count_24h",
                            "operator": ">=",
                            "right": {"threshold_ref": "VELOCITY_24H_COUNT_THRESHOLD"},
                            "value_type": "integer",
                        },
                        {
                            "left": "txn_sum_24h_minor",
                            "operator": ">=",
                            "right": {"threshold_ref": "VELOCITY_24H_AMOUNT_THRESHOLD_MINOR"},
                            "value_type": "integer_paisa",
                        },
                    ],
                }
            ],
            "outcome": {
                "outcome_id": "out_velocity_alert",
                "outcome_type": "obligation",
                "description": (
                    "Raise an AML alert when transaction velocity (count or volume) "
                    "within 24 hours exceeds the configured thresholds."
                ),
                "source_url": payatlas,
            },
            "status": "engineering_certified",
        },
        "rule_mule_account_pattern": {
            "rule_id": "rule_mule_account_pattern",
            "rule_family_id": "family_mule",
            "modality": "obligation",
            "action": "raise_aml_alert_mule",
            "actor": "psp_pso",
            "object": "mule account pattern",
            "authority_class": "guidance",
            "certification_tier": "engineering",
            "conditions": [
                {
                    "condition_id": "cond_fan_in",
                    "left": "unique_sender_count_24h",
                    "operator": ">=",
                    "right": {"threshold_ref": "MULE_FAN_IN_SENDER_COUNT"},
                },
                {
                    "condition_id": "cond_rapid_forward",
                    "left": "time_to_forward_median_seconds",
                    "operator": "<=",
                    "right": {"threshold_ref": "MULE_FORWARD_WINDOW_SECONDS"},
                },
                {
                    "condition_id": "cond_few_destinations",
                    "left": "unique_recipient_count_24h",
                    "operator": "<=",
                    "right": {"threshold_ref": "MULE_FAN_OUT_RECIPIENT_COUNT"},
                },
            ],
            "outcome": {
                "outcome_id": "out_mule_alert",
                "outcome_type": "obligation",
                "description": (
                    "Raise an AML alert when fan-in/fan-out mule account pattern is "
                    "detected."
                ),
                "source_url": payatlas,
            },
            "status": "engineering_certified",
        },
        "rule_sanctions_confirmed_hit": {
            "rule_id": "rule_sanctions_confirmed_hit",
            "rule_family_id": "family_sanctions",
            "modality": "obligation",
            "action": "raise_aml_alert_sanctions_and_seed_str",
            "actor": "psp_pso",
            "object": "confirmed sanctions hit",
            "authority_class": "statute",
            "certification_tier": "legal",
            "conditions": [
                {
                    "condition_id": "cond_hit_confirmed",
                    "left": "sanctions_hit_status",
                    "operator": "==",
                    "right": "CONFIRMED",
                    "value_type": "string",
                },
                {
                    "condition_id": "cond_camlco_declared",
                    "left": "camlco_declaration",
                    "operator": "==",
                    "right": True,
                    "value_type": "bool",
                },
            ],
            "outcome": {
                "outcome_id": "out_sanctions_confirmed",
                "outcome_type": "obligation",
                "description": (
                    "A CAMLCO-confirmed sanctions hit must raise a CRITICAL AML alert, "
                    "open a case, and pre-seed an STR in PENDING_CAMLCO_REVIEW; the "
                    "freeze persists pending BFIU written instruction."
                ),
                "source_url": bfiu_guidance,
            },
            "note": (
                "Fired only via the spec 07 CAMLCO confirm API. The created aml_alerts "
                "row uses this rule_id; its idempotency_key derives from the sanc_id so "
                "duplicate confirmations cannot create duplicate alerts."
            ),
            "status": "legally_certified",
        },
    }


# ---------------------------------------------------------------------------
# Rule schema validation
# ---------------------------------------------------------------------------


def _validate_atom(atom: Mapping[str, Any], where: str) -> None:
    fact = atom.get("fact_key", atom.get("left"))
    if not isinstance(fact, str) or not fact:
        raise _signature_error(f"{where}: condition is missing a fact key")
    if not isinstance(atom.get("operator"), str):
        raise _signature_error(f"{where}: condition is missing an operator")
    value = atom.get("value", atom.get("right"))
    if isinstance(value, float):
        raise _signature_error(
            f"{where}: float condition values are forbidden (money is integer paisa)"
        )
    if atom.get("value_type") == "integer_paisa" and not (
        isinstance(value, int) and not isinstance(value, bool)
    ):
        if not (isinstance(value, Mapping) and "threshold_ref" in value):
            raise _signature_error(
                f"{where}: integer_paisa condition value must be int or threshold_ref"
            )


def _validate_condition_tree(node: Mapping[str, Any], where: str) -> None:
    kind = node.get("kind")
    if kind in ("all", "any"):
        children = node.get("children")
        if not isinstance(children, list) or not children:
            raise _signature_error(f"{where}: {kind} node needs non-empty children")
        for child in children:
            _validate_condition_tree(child, where)
        return
    if kind == "atom" or "left" in node or "fact_key" in node:
        _validate_atom(node, where)
        return
    if node.get("operator") == "any_of" and isinstance(node.get("children"), list):
        for child in node["children"]:
            _validate_atom(child, where)
        return
    raise _signature_error(f"{where}: unrecognized condition node shape")


def validate_rules(rules: Mapping[str, Any]) -> int:
    """Validate the pack's rules object; returns the rule count (fail-closed)."""
    if not isinstance(rules, Mapping) or not rules:
        raise _signature_error("rules object must be a non-empty mapping")
    for rule_id, rule in rules.items():
        where = f"rule {rule_id!r}"
        if not isinstance(rule, Mapping):
            raise _signature_error(f"{where}: definition must be a mapping")
        if rule.get("rule_id") != rule_id:
            raise _signature_error(f"{where}: rule_id field must equal its key")
        if rule.get("modality") not in _VALID_MODALITIES:
            raise _signature_error(f"{where}: modality must be one of {_VALID_MODALITIES}")
        for field_name in ("action", "actor", "object", "authority_class", "certification_tier"):
            if not isinstance(rule.get(field_name), str) or not rule[field_name]:
                raise _signature_error(f"{where}: missing field {field_name!r}")
        outcome = rule.get("outcome")
        if not isinstance(outcome, Mapping):
            raise _signature_error(f"{where}: missing outcome object")
        for field_name in ("outcome_id", "outcome_type", "description", "source_url"):
            if not isinstance(outcome.get(field_name), str) or not outcome[field_name]:
                raise _signature_error(f"{where}: outcome missing {field_name!r}")
        source_url = outcome["source_url"]
        if "example.invalid" in source_url:
            raise _signature_error(f"{where}: source_url must be a real source pointer")
        conditions = rule.get("conditions")
        condition = rule.get("condition")
        if conditions is None and condition is None:
            raise _signature_error(f"{where}: needs 'conditions' list or 'condition' tree")
        if conditions is not None:
            if not isinstance(conditions, list) or not conditions:
                raise _signature_error(f"{where}: conditions must be a non-empty list")
            for cond in conditions:
                _validate_condition_tree(cond, where)
        if condition is not None:
            _validate_condition_tree(condition, where)
    return len(rules)


# ---------------------------------------------------------------------------
# Deterministic rule-condition evaluation (no LLM, no wall clock, no floats)
# ---------------------------------------------------------------------------


def _resolve_value(value: Any, threshold_values: Mapping[str, int]) -> Any:
    if isinstance(value, Mapping) and "threshold_ref" in value:
        ref = value["threshold_ref"]
        if ref not in threshold_values:
            raise InvalidRequestError(
                f"unresolved threshold_ref {ref!r}; the thresholds artifact must "
                "supply every referenced value (fail-closed)",
                code="threshold_ref_unresolved",
            )
        return threshold_values[ref]
    if isinstance(value, float):
        raise InvalidRequestError(
            "float rule values are forbidden (integer paisa only)", code="float_rejected"
        )
    return value


def _eval_atom(
    atom: Mapping[str, Any],
    facts: Mapping[str, Any],
    threshold_values: Mapping[str, int],
) -> bool:
    fact_key = atom.get("fact_key", atom.get("left"))
    operator = atom["operator"]
    expected = _resolve_value(atom.get("value", atom.get("right")), threshold_values)
    if fact_key not in facts:
        # Refusal-first: a missing fact never silently satisfies a condition.
        return False
    actual = facts[fact_key]
    if isinstance(actual, float):
        raise InvalidRequestError(
            "float facts are forbidden (integer paisa only)", code="float_rejected"
        )
    if operator == "==":
        return actual == expected
    if operator == "!=":
        return actual != expected
    if operator == ">":
        return actual > expected
    if operator == ">=":
        return actual >= expected
    if operator == "<":
        return actual < expected
    if operator == "<=":
        return actual <= expected
    if operator == "in":
        return actual in expected
    raise InvalidRequestError(
        f"unknown rule operator {operator!r}", code="unknown_operator"
    )


def _eval_node(
    node: Mapping[str, Any],
    facts: Mapping[str, Any],
    threshold_values: Mapping[str, int],
) -> bool:
    kind = node.get("kind")
    if kind == "all":
        return all(_eval_node(c, facts, threshold_values) for c in node["children"])
    if kind == "any":
        return any(_eval_node(c, facts, threshold_values) for c in node["children"])
    if node.get("operator") == "any_of" and isinstance(node.get("children"), list):
        return any(_eval_atom(c, facts, threshold_values) for c in node["children"])
    return _eval_atom(node, facts, threshold_values)


def evaluate_rule(
    rule: Mapping[str, Any],
    facts: Mapping[str, Any],
    *,
    threshold_values: Mapping[str, int] | None = None,
) -> bool:
    """True when the rule's conditions are ALL satisfied by ``facts``.

    Deterministic by construction: same rule + same facts + same thresholds
    artifact values => same verdict. Missing facts fail closed (the condition
    is unsatisfied, never assumed). Floats anywhere are refused.
    """
    threshold_values = dict(threshold_values or {})
    conditions = rule.get("conditions")
    condition = rule.get("condition")
    if conditions is not None:
        return all(_eval_node(c, facts, threshold_values) for c in conditions)
    if condition is not None:
        return _eval_node(condition, facts, threshold_values)
    raise InvalidRequestError(
        "rule has neither 'conditions' nor 'condition'", code="invalid_rule"
    )


# ---------------------------------------------------------------------------
# Pack building + entity + store
# ---------------------------------------------------------------------------


def build_signed_pack(
    *,
    rules: Mapping[str, Any],
    pack_version: str,
    signer_id: str,
    private_key_hex: str,
    created_at: datetime,
    domain: str = "Bangladesh PSP/PSO AML",
) -> dict:
    """Assemble and sign a pack artifact (the generate_banking_pack adaptation).

    The artifact is pure JSON (wire-ready): ``created_at`` is serialized to
    the RFC3339 millisecond ``Z`` string before hashing, so the pack_id is
    stable across build, transport, and re-verification.
    """
    validate_rules(rules)
    if created_at.tzinfo is None or created_at.tzinfo.utcoffset(created_at) is None:
        raise ValueError("created_at must be timezone-aware (spec 00 section 7)")
    at_utc = created_at.astimezone(UTC)
    created_at_str = (
        f"{at_utc.year:04d}-{at_utc.month:02d}-{at_utc.day:02d}"
        f"T{at_utc.hour:02d}:{at_utc.minute:02d}:{at_utc.second:02d}"
        f".{at_utc.microsecond // 1000:03d}Z"
    )
    content_hash = sha256_canonical(dict(rules))
    manifest_core = {
        "pack_version": pack_version,
        "domain": domain,
        "jurisdiction_scope": "BD",
        "created_at": created_at_str,
        "signer_id": signer_id,
        "signed_pack_content_hash": content_hash,
        "pack_content_hash": content_hash,
        "schema_version": "1.0",
        "source_set_claim": "complete",
        "certification_level": "legal",
    }
    pack_id = make_id("pack", manifest_core)
    manifest = dict(manifest_core)
    manifest["pack_id"] = pack_id
    signature_hex = sign_content_hash(private_key_hex, content_hash)
    return {
        "manifest": manifest,
        "rules": {k: dict(v) for k, v in rules.items()},
        "signature": {
            "signature_id": f"sig_{pack_id}",
            "algorithm": "ed25519",
            "signer_id": signer_id,
            "signed_pack_content_hash": content_hash,
            "signature": signature_hex,
        },
    }


@dataclass(frozen=True)
class RulePack:
    """One rule_packs row (migration 0070)."""

    pack_id: str
    pack_version: str
    domain: str
    jurisdiction_scope: str
    status: str  # ACTIVE | SUPERSEDED | REJECTED
    rule_count: int
    signer_id: str
    signed_pack_content_hash: str
    signature_hex: str
    pack_manifest_hash: str
    rules_json: Mapping[str, Any]
    activated_at: datetime
    activated_by: str
    superseded_at: datetime | None = None
    activation_notes: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.status not in _PACK_STATUSES:
            raise ValueError(f"status must be one of {_PACK_STATUSES}, got {self.status!r}")


@runtime_checkable
class RulePackStore(Protocol):
    """Storage contract for rule packs (one ACTIVE at a time)."""

    def insert(self, pack: RulePack) -> None: ...

    def save(self, pack: RulePack) -> None: ...

    def get(self, pack_id: str) -> RulePack | None: ...

    def active(self) -> RulePack | None: ...

    def list_all(self) -> list[RulePack]: ...


class InMemoryRulePackStore:
    """Deterministic in-memory pack store for unit tests."""

    def __init__(self) -> None:
        self._rows: dict[str, RulePack] = {}
        self._order: list[str] = []

    def insert(self, pack: RulePack) -> None:
        if pack.pack_id in self._rows:
            raise ValueError(f"pack {pack.pack_id!r} already loaded")
        self._rows[pack.pack_id] = pack
        self._order.append(pack.pack_id)

    def save(self, pack: RulePack) -> None:
        if pack.pack_id not in self._rows:
            raise ValueError(f"unknown pack {pack.pack_id!r}")
        self._rows[pack.pack_id] = pack

    def get(self, pack_id: str) -> RulePack | None:
        return self._rows.get(pack_id)

    def active(self) -> RulePack | None:
        for pack_id in self._order:
            if self._rows[pack_id].status == "ACTIVE":
                return self._rows[pack_id]
        return None

    def list_all(self) -> list[RulePack]:
        return [self._rows[pack_id] for pack_id in self._order]


class RulePackLoader:
    """The synchronous verification gate (spec/06 "RulePack Activation").

    Verification order is binding: signer registry lookup -> content-hash
    recomputation -> Ed25519 signature -> rule schema validation -> single
    transactionally-composed persist (supersede previous, audit, outbox).
    Any failure leaves the existing ACTIVE pack untouched.
    """

    def __init__(
        self,
        store: RulePackStore,
        registry: SigningKeyRegistry,
        *,
        clock: Clock,
        audit: AuditPort,
        outbox: OutboxPort,
    ) -> None:
        self._store = store
        self._registry = registry
        self._clock = clock
        self._audit = audit
        self._outbox = outbox

    def verify_and_load(
        self,
        artifact_bytes: bytes,
        *,
        activated_by: str,
        activation_notes: str | None = None,
        conn: Any | None = None,
    ) -> RulePack:
        """Verify a signed pack artifact and activate it (fail-closed)."""
        try:
            artifact = json.loads(artifact_bytes.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _signature_error("artifact is not valid UTF-8 JSON") from exc
        if not isinstance(artifact, dict):
            raise _signature_error("artifact must be a JSON object")
        manifest = artifact.get("manifest")
        rules = artifact.get("rules")
        signature = artifact.get("signature")
        if not isinstance(manifest, dict) or not isinstance(rules, dict):
            raise _signature_error("artifact needs 'manifest' and 'rules' objects")
        if not isinstance(signature, dict):
            raise _signature_error("artifact is missing its 'signature' object")

        signer_id = manifest.get("signer_id")
        if not isinstance(signer_id, str) or not signer_id:
            raise _signature_error("manifest is missing signer_id")
        if signature.get("signer_id") != signer_id:
            raise _signature_error("signature signer_id does not match the manifest")

        declared_hash = manifest.get("signed_pack_content_hash")
        recomputed_hash = sha256_canonical(rules)
        if declared_hash != recomputed_hash:
            raise _signature_error("pack content hash mismatch (rules were modified)")
        if signature.get("signed_pack_content_hash") != recomputed_hash:
            raise _signature_error("signature block hash does not match pack content")

        signature_hex = signature.get("signature")
        if not isinstance(signature_hex, str) or len(signature_hex) != 128:
            raise _signature_error("signature must be a 128-char hex Ed25519 signature")
        try:
            self._registry.verify_signature(
                signer_id=signer_id,
                content_hash_hex=recomputed_hash,
                signature_hex=signature_hex,
            )
        except SigningKeyError as exc:
            raise _signature_error(str(exc)) from exc

        rule_count = validate_rules(rules)

        pack_version = manifest.get("pack_version")
        if not isinstance(pack_version, str) or not pack_version:
            raise _signature_error("manifest is missing pack_version")
        pack_id = manifest.get("pack_id")
        if not isinstance(pack_id, str) or not pack_id.startswith("pack_"):
            raise _signature_error("manifest is missing a pack_<hash> pack_id")
        manifest_for_hash = {
            k: v for k, v in manifest.items() if k not in ("pack_id", "pack_manifest_hash")
        }
        pack_manifest_hash = sha256_canonical(manifest_for_hash)

        now = self._clock.now()
        previous = self._store.active()
        if previous is not None:
            self._store.save(replace(previous, status="SUPERSEDED", superseded_at=now))
        pack = RulePack(
            pack_id=pack_id,
            pack_version=pack_version,
            domain=str(manifest.get("domain", "Bangladesh PSP/PSO AML")),
            jurisdiction_scope=str(manifest.get("jurisdiction_scope", "BD")),
            status="ACTIVE",
            rule_count=rule_count,
            signer_id=signer_id,
            signed_pack_content_hash=recomputed_hash,
            signature_hex=signature_hex,
            pack_manifest_hash=pack_manifest_hash,
            rules_json=rules,
            activated_at=now,
            activated_by=activated_by,
            activation_notes=activation_notes,
        )
        self._store.insert(pack)
        self._audit.append(
            AuditEventSpec(
                event_type="RULE_PACK_ACTIVATED",
                actor_id=activated_by,
                subject_type="RulePack",
                subject_id=pack_id,
                from_state=previous.pack_id if previous else None,
                to_state="ACTIVE",
                payload={
                    "pack_version": pack_version,
                    "signer_id": signer_id,
                    "rule_count": rule_count,
                },
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="rule_pack.activated",
                subject_type="RulePack",
                subject_id=pack_id,
                producer=PRODUCER,
                topic="aml.events",
                payload={
                    "pack_id": pack_id,
                    "pack_version": pack_version,
                    "signer_id": signer_id,
                    "rule_count": rule_count,
                },
                occurred_at=now,
            ),
            conn=conn,
        )
        return pack
