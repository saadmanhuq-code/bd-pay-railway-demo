"""goAML STR/CTR payload assembly + local schema checks (spec/06 contract).

The wire submission is the ``goaml_reporter_v1`` connector's job (spec/12,
``AmlFilingConnector`` sub-protocol, spec/00 §10). This module owns what
compliance passes to it: the exact payload shapes from spec/06 "Connector
Contract", validated locally BEFORE any filing attempt so a malformed payload
can never consume a filing retry.

PII discipline: durable rows carry internal refs, hashes, and object-store
pointers only — never a raw name, NID, mobile, or narrative text.  The STR
handoff payload may include a resolved ``suspicion_narrative`` only at the
connector boundary, so the goAML XML renderer can place it into the archived
XML object without persisting it in ``str_reports`` / ``goaml_filings`` rows.

The next certainty level — rendering these payloads into goAML report XML
and validating against the pinned OFFICIAL goAML XSD artifact — lives in
:mod:`bdpay.compliance.goaml_xml` (divergences between the spec corpus and
the real schema are recorded in SPEC_ERRATA-LANE-B.md).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.errors import InvalidRequestError
from bdpay.platform.pii import redact

__all__ = [
    "FILING_INSTITUTION",
    "build_ctr_payload",
    "build_str_payload",
    "validate_ctr_payload",
    "validate_str_payload",
]

FILING_INSTITUTION = "BD-PAY"

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _schema_error(detail: str) -> InvalidRequestError:
    return InvalidRequestError(
        f"goAML payload schema check failed: {detail}", code="goaml_payload_invalid"
    )


def _require_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise _schema_error(f"{key} must be a non-empty string")
    return value


def _require_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _schema_error(f"{key} must be an integer (floats are rejected; paisa)")
    if value < 0:
        raise _schema_error(f"{key} must be >= 0")
    return value


def _reject_floats(obj: Any, where: str) -> None:
    if isinstance(obj, float):
        raise _schema_error(f"{where} contains a float (money is integer paisa)")
    if isinstance(obj, dict):
        for key, value in obj.items():
            _reject_floats(value, f"{where}.{key}")
    if isinstance(obj, list | tuple):
        for index, value in enumerate(obj):
            _reject_floats(value, f"{where}[{index}]")


def _require_pii_free(payload: dict, key: str) -> None:
    value = payload.get(key)
    if isinstance(value, str) and redact(value) != value:
        raise _schema_error(f"{key} contains raw PII; payloads carry refs and hashes only")


# ---------------------------------------------------------------------------
# STR payload (spec/06 Connector contract — file_str shape)
# ---------------------------------------------------------------------------


def build_str_payload(
    *,
    str_id: str,
    camlco_id: str,
    subject_type: str,
    subject_id: str,
    transaction_ids: list[str],
    suspicion_narrative_pointer: str,
    goaml_payload_pointer: str,
    raised_at: datetime,
    filing_at: datetime,
    suspicion_narrative: str | None = None,
) -> dict:
    """Assemble the STR filing payload passed to ``file_str(payload)``."""
    goaml_subject = "ENTITY" if subject_type == "Merchant" else "INDIVIDUAL"
    payload = {
        "report_type": "STR",
        "report_ref": str_id,
        "filing_institution": FILING_INSTITUTION,
        "camlco_name_hash": sha256_canonical({"camlco_id": camlco_id}),
        "subject_type": goaml_subject,
        "subject_ref": subject_id,
        "transaction_ids": list(transaction_ids),
        "suspicion_narrative_pointer": suspicion_narrative_pointer,
        "goaml_payload_pointer": goaml_payload_pointer,
        "raised_at": raised_at.isoformat(),
        "filing_at": filing_at.isoformat(),
    }
    if suspicion_narrative is not None:
        payload["suspicion_narrative"] = suspicion_narrative
    return payload


def validate_str_payload(payload: dict) -> None:
    """Local STR schema check (fail-closed; never spends a filing attempt)."""
    if not isinstance(payload, dict):
        raise _schema_error("payload must be a dict")
    _reject_floats(payload, "payload")
    if _require_str(payload, "report_type") != "STR":
        raise _schema_error("report_type must be 'STR'")
    report_ref = _require_str(payload, "report_ref")
    if not report_ref.startswith("str_"):
        raise _schema_error("report_ref must be a str_<hash> internal reference")
    if _require_str(payload, "filing_institution") != FILING_INSTITUTION:
        raise _schema_error(f"filing_institution must be {FILING_INSTITUTION!r}")
    camlco_hash = _require_str(payload, "camlco_name_hash")
    if not _HEX64_RE.fullmatch(camlco_hash):
        raise _schema_error("camlco_name_hash must be a 64-char sha256 hex (PII-hashed)")
    if _require_str(payload, "subject_type") not in ("INDIVIDUAL", "ENTITY"):
        raise _schema_error("subject_type must be INDIVIDUAL or ENTITY")
    _require_str(payload, "subject_ref")
    _require_pii_free(payload, "subject_ref")
    txn_ids = payload.get("transaction_ids")
    if not isinstance(txn_ids, list):
        raise _schema_error("transaction_ids must be a list of payment intent IDs")
    for txn in txn_ids:
        if not isinstance(txn, str) or not txn.startswith("pi_"):
            raise _schema_error("every transaction_id must be a pi_<hash> reference")
    _require_str(payload, "suspicion_narrative_pointer")
    _require_str(payload, "goaml_payload_pointer")
    if "suspicion_narrative" in payload:
        value = payload.get("suspicion_narrative")
        if not isinstance(value, str) or not value.strip():
            raise _schema_error("suspicion_narrative must be a non-empty string")
    for key in ("raised_at", "filing_at"):
        value = _require_str(payload, key)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise _schema_error(f"{key} must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            # Naive datetimes are banned (spec/00 §7) and only partially
            # ordered against the goAML schema's sql_date facets.
            raise _schema_error(f"{key} must be timezone-aware (UTC)")


# ---------------------------------------------------------------------------
# CTR payload (spec/06 Connector contract — file_ctr shape)
# ---------------------------------------------------------------------------


def build_ctr_payload(
    *,
    ctr_id: str,
    account_id: str,
    aggregation_date: date,
    total_cash_in_minor: int,
    total_cash_out_minor: int,
    contributing_txn_count: int,
) -> dict:
    """Assemble the CTR filing payload passed to ``file_ctr(payload)``."""
    return {
        "report_type": "CTR",
        "report_ref": ctr_id,
        "filing_institution": FILING_INSTITUTION,
        "account_ref": account_id,
        "aggregation_date": aggregation_date.isoformat(),
        "total_cash_in_minor": total_cash_in_minor,
        "total_cash_out_minor": total_cash_out_minor,
        "currency": "BDT",
        "contributing_txn_count": contributing_txn_count,
    }


def validate_ctr_payload(payload: dict) -> None:
    """Local CTR schema check (fail-closed)."""
    if not isinstance(payload, dict):
        raise _schema_error("payload must be a dict")
    _reject_floats(payload, "payload")
    if _require_str(payload, "report_type") != "CTR":
        raise _schema_error("report_type must be 'CTR'")
    report_ref = _require_str(payload, "report_ref")
    if not report_ref.startswith("ctr_"):
        raise _schema_error("report_ref must be a ctr_<hash> internal reference")
    if _require_str(payload, "filing_institution") != FILING_INSTITUTION:
        raise _schema_error(f"filing_institution must be {FILING_INSTITUTION!r}")
    account_ref = _require_str(payload, "account_ref")
    if not account_ref.startswith("acct_"):
        raise _schema_error("account_ref must be an acct_<hash> reference")
    agg_date = _require_str(payload, "aggregation_date")
    if not _DATE_RE.fullmatch(agg_date):
        raise _schema_error("aggregation_date must be YYYY-MM-DD")
    try:
        date.fromisoformat(agg_date)
    except ValueError as exc:
        raise _schema_error("aggregation_date is not a real calendar date") from exc
    _require_int(payload, "total_cash_in_minor")
    _require_int(payload, "total_cash_out_minor")
    if _require_str(payload, "currency") != "BDT":
        raise _schema_error("currency must be BDT (v1)")
    _require_int(payload, "contributing_txn_count")
