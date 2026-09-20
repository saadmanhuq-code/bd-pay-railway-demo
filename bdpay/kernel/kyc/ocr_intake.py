"""Document-OCR intake extension for the spec/09 NID pipeline.

The spec/09 §2 direct path (client-side OCR posts structured ``_raw`` fields)
stays untouched. This module adds the provider path: the kernel consumes raw
Bengali OCR TEXT through :class:`DocumentOcrPort` (shape-compatible with the
``veridyn_ocr_v1`` connector — ``bdpay.connectors.identity.ocr``), extracts
the NID fields with the existing identity-core port logic
(:mod:`bdpay.kernel.kyc.nid` — 10/13/17 forms, label rules, DOB cross-check),
and feeds the SAME ``submit_nid_ocr`` intake so T2/T3 guards, hashing,
encryption and minor routing have exactly one implementation.

Failure posture (binding):

- Provider failure of ANY kind (outage, breaker open, auth rejection,
  contract mismatch) -> ``PENDING_HUMAN_REVIEW`` with review reason
  ``OCR_UNAVAILABLE`` — the spec/09 T10 non-blocking-fallback posture applied
  to the OCR port. Onboarding is never hard-blocked on OCR availability.
- Extraction problems in a SUCCESSFUL OCR result (no NID found, bad format,
  DOB contradiction, missing name) are REFUSALS — the record stays
  ``SUBMITTED`` and the customer recaptures, exactly like the spec/09
  confidence-gate recapture path. A degraded document is the customer's to
  re-photograph; a degraded provider is ours to absorb.

PII: the OCR text transits memory only; what persists is what the existing
intake persists (``nid_hash`` + ``nid_encrypted`` + normalised names). The
audit row carries the provider's ``raw_response_hash``, never the text.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.kyc.nid import verify_nid_intake
from bdpay.platform.errors import InvalidRequestError

__all__ = [
    "DocumentOcrPort",
    "OCR_UNAVAILABLE_REASON",
    "derive_nid_submission",
    "iso_to_dob_raw",
]

#: The KycManualReview reason recorded when the OCR provider is unavailable
#: (additive to the spec/09 §7 catalogue — errata OCR-1).
OCR_UNAVAILABLE_REASON = "OCR_UNAVAILABLE"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@runtime_checkable
class DocumentOcrPort(Protocol):
    """The document-OCR provider surface the kyc-service depends on.

    Shape-compatible with ``bdpay.connectors.identity.ocr.OcrConnector``:
    returns an object exposing ``raw_text`` (str), ``confidence`` (Decimal —
    floats are rejected downstream), and ``raw_response_hash`` (str). Any
    raised exception means "provider unavailable" and routes the record to
    ``PENDING_HUMAN_REVIEW`` — identical convention to
    ``IdentityVerificationPort``.
    """

    async def extract_document(self, object_key: str) -> Any: ...


def iso_to_dob_raw(dob_iso: str) -> str:
    """ISO ``YYYY-MM-DD`` -> the intake's ``DD/MM/YYYY`` wire form.

    Format-shape conversion only; calendar validity is enforced by the one
    existing intake validator (``_normalise_dob`` fails closed on impossible
    dates), so the OCR path cannot grow a second, divergent DOB rule.
    """
    if not isinstance(dob_iso, str) or not _ISO_DATE_RE.fullmatch(dob_iso):
        raise InvalidRequestError(
            "date of birth must be ISO YYYY-MM-DD", code="dob_invalid"
        )
    yyyy, mm, dd = dob_iso.split("-")
    return f"{dd}/{mm}/{yyyy}"


def derive_nid_submission(
    raw_text: str,
    *,
    nid_type: str,
    confidence: Decimal,
    stated_dob_iso: str | None = None,
    fallback_name_en: str | None = None,
):
    """OCR text -> the spec/09 §2 submission, or a typed refusal (fail-closed).

    Returns a :class:`~bdpay.kernel.kyc.kyc.NidOcrSubmission`. Refusal codes:

    - ``ocr_text_invalid``       OCR result text is not a string
    - ``nid_extraction_failed``  no NID number found in the text (recapture)
    - ``nid_format_invalid``     a candidate was found but fails 10/13/17
    - ``nid_dob_mismatch``       embedded year or extracted DOB contradicts
                                 the stated DOB
    - ``dob_invalid``            no DOB extracted and none stated
    - ``name_extraction_failed`` no English holder name extracted and no
                                 ``fallback_name_en`` supplied
    """
    from bdpay.kernel.kyc.kyc import NidOcrSubmission  # local: avoids module cycle

    if not isinstance(raw_text, str):
        raise InvalidRequestError(
            "OCR text must be a string", code="ocr_text_invalid"
        )
    if stated_dob_iso is not None and not _ISO_DATE_RE.fullmatch(stated_dob_iso):
        raise InvalidRequestError(
            "stated date of birth must be ISO YYYY-MM-DD", code="dob_invalid"
        )
    intake = verify_nid_intake(raw_text, stated_dob_iso=stated_dob_iso)
    if intake.status == "extraction_failed":
        raise InvalidRequestError(
            "no NID number could be extracted from the document text; recapture",
            code="nid_extraction_failed",
        )
    if intake.status == "invalid_format":
        raise InvalidRequestError(
            "extracted NID fails the 10/13/17 format rule",
            code="nid_format_invalid",
        )
    if intake.status == "dob_mismatch":
        raise InvalidRequestError(
            "NID and stated date of birth do not agree", code="nid_dob_mismatch"
        )
    assert intake.nid is not None  # document_parsed_pending_government carries fields

    extracted_dob = intake.nid.dob
    if (
        extracted_dob is not None
        and stated_dob_iso is not None
        and extracted_dob != stated_dob_iso
    ):
        # Both sources present and contradictory: fail closed, never pick one.
        raise InvalidRequestError(
            "extracted and stated dates of birth do not agree",
            code="nid_dob_mismatch",
        )
    resolved_dob = extracted_dob or stated_dob_iso
    if resolved_dob is None:
        raise InvalidRequestError(
            "no date of birth in the document text and none stated",
            code="dob_invalid",
        )

    extracted_name = intake.nid.name
    name_en: str | None = None
    name_bn: str | None = None
    if extracted_name is not None and extracted_name.isascii():
        name_en = extracted_name
    elif extracted_name is not None:
        name_bn = extracted_name
    if name_en is None:
        name_en = fallback_name_en
    if not name_en or not name_en.strip():
        raise InvalidRequestError(
            "no English holder name extracted; supply fallback_name_en",
            code="name_extraction_failed",
        )

    return NidOcrSubmission(
        nid_number_raw=intake.nid.number,
        name_en_raw=name_en,
        name_bn_raw=name_bn,
        dob_raw=iso_to_dob_raw(resolved_dob),
        nid_type=nid_type,
        ocr_confidence_score=confidence,
    )
