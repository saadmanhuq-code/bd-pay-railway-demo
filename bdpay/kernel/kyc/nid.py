"""Bangladesh NID extraction + format validation (spec/09 NID intake).

Ported from ``portfolio-core/packages/identity-core`` (DIRECT LIFT logic,
per PORTING-MAP.md): ``src/nid.ts`` (extraction, format validation, DOB and
name extraction, Bengali digit normalisation) and ``src/verify.ts`` /
``src/types.ts`` (the fail-closed status taxonomy with its honest-labeling
rule). The TypeScript source's test vectors travel with the port in
``tests/kernel/test_kyc_nid.py``.

Adaptations to BD-PAY conventions:

- Bengali digit mapping uses :func:`bdpay.platform.bangla.normalize_bengali_digits`
  (the platform's single implementation) instead of a local regex.
- spec/09 accepts the 10-digit smart-card NID form in addition to the
  source's 13/17 forms; format validation accepts 10/13/17. Free-text
  extraction stays at label-prefixed 10/13/17 plus standalone 17/13 runs —
  a standalone 10-digit run is indistinguishable from a phone-number tail
  and fails closed to "not found" (errata K-12).
- A date-of-birth cross-check is added per the build brief: a 17-digit NID
  embeds the birth year as its first four digits; a mismatch fails closed.

Every result is flagged ``review_required=True`` (the source's
honest-labeling rule): local parsing is NEVER a government verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bdpay.platform.bangla import normalize_bengali_digits

__all__ = [
    "NID_LENGTHS",
    "NidFields",
    "NidIntakeResult",
    "dob_cross_check",
    "extract_dob",
    "extract_name",
    "extract_nid_number",
    "is_valid_nid_format",
    "normalize_nid_number",
    "verify_nid_intake",
]

#: Accepted NID digit counts (spec/09: 10 = smart card short form,
#: 13 = pre-2016, 17 = year-prefixed long form).
NID_LENGTHS: tuple[int, ...] = (10, 13, 17)

#: Label patterns preceding an NID number — English and Bangla forms
#: (ported from identity-core ``NID_LABEL_RX``).
_NID_LABEL = (
    r"(?:NID|National\s*ID(?:entification)?|জাতীয়\s*পরিচয়পত্র|ভোটার\s*আইডি|ক্রমিক\s*নং)"
    r"\s*[:\-–—]?\s*"
)
_LABELED_RE = re.compile(_NID_LABEL + r"(\d{17}|\d{13}|\d{10})(?!\d)", re.IGNORECASE)
_SMART_RE = re.compile(r"(?<!\d)(\d{17})(?!\d)")
_OLD_RE = re.compile(r"(?<!\d)(\d{13})(?!\d)")
_DMY_RE = re.compile(r"\b(\d{2})[/\-](\d{2})[/\-](\d{4})\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_NAME_RE = re.compile(r"(?:Name|নাম)\s*[:：]\s*(.+?)(?:\n|$)", re.IGNORECASE)


def normalize_nid_number(raw: str) -> str:
    """Bengali digits -> ASCII; whitespace and hyphens stripped."""
    if not isinstance(raw, str):
        raise TypeError(f"raw must be str, got {type(raw).__name__}")
    western = normalize_bengali_digits(raw)
    return "".join(c for c in western if c.isdigit())


def is_valid_nid_format(candidate: str) -> bool:
    """Exactly 10, 13 or 17 ASCII digits — FORMAT check only, never a
    government-database verification (identity-core honest-labeling rule)."""
    return bool(re.fullmatch(r"\d{10}|\d{13}|\d{17}", candidate or ""))


def extract_nid_number(raw_text: str) -> str | None:
    """First NID number in OCR text, or None (fails closed).

    Strategy (ported): 1) normalise Bengali digits; 2) label-prefixed run
    (10/13/17, higher precision); 3) standalone 17-digit run; 4) standalone
    13-digit run. Standalone 10-digit runs are NOT extracted (errata K-12).
    """
    if not raw_text or not raw_text.strip():
        return None
    text = normalize_bengali_digits(raw_text)
    labeled = _LABELED_RE.search(text)
    if labeled:
        return labeled.group(1)
    smart = _SMART_RE.search(text)
    if smart:
        return smart.group(1)
    old = _OLD_RE.search(text)
    if old:
        return old.group(1)
    return None


def extract_dob(raw_text: str) -> str | None:
    """Best-effort DOB extraction -> ISO-8601 ``YYYY-MM-DD`` or None.

    Recognises DD/MM/YYYY, DD-MM-YYYY and ISO YYYY-MM-DD (ported).
    """
    if not raw_text:
        return None
    text = normalize_bengali_digits(raw_text)
    dmy = _DMY_RE.search(text)
    if dmy:
        dd, mm, yyyy = dmy.groups()
        return f"{yyyy}-{mm}-{dd}"
    iso = _ISO_RE.search(text)
    if iso:
        return iso.group(0)
    return None


def extract_name(raw_text: str) -> str | None:
    """Best-effort holder-name extraction (``Name:`` / ``নাম:`` labels, ported)."""
    if not raw_text:
        return None
    match = _NAME_RE.search(raw_text)
    if match:
        name = match.group(1).strip()
        return name or None
    return None


def dob_cross_check(nid_number: str, dob_iso: str) -> bool:
    """17-digit NIDs embed the birth year as the first four digits; check it.

    10/13-digit forms carry no year — they pass vacuously. An unparseable
    DOB fails closed (False).
    """
    if not is_valid_nid_format(nid_number):
        return False
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob_iso or ""):
        return False
    if len(nid_number) != 17:
        return True
    return nid_number[:4] == dob_iso[:4]


@dataclass(frozen=True)
class NidFields:
    """Parsed NID fields (ported from identity-core ``NidFields``)."""

    number: str
    dob: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class NidIntakeResult:
    """Fail-closed intake verdict (ported status taxonomy + ``dob_mismatch``).

    ``status`` is one of:

    - ``document_parsed_pending_government`` — parsed and format-valid;
      NOT government-verified (the caller must run the identity provider).
    - ``extraction_failed`` — no NID found in the text.
    - ``invalid_format`` — a candidate was found but the format check failed.
    - ``dob_mismatch`` — the embedded birth year contradicts the stated DOB.

    ``review_required`` is ALWAYS True — the honest-labeling rule travels
    with the port.
    """

    status: str
    review_reason: str
    nid: NidFields | None = None
    review_required: bool = True


def verify_nid_intake(
    extracted_text: str, *, stated_dob_iso: str | None = None
) -> NidIntakeResult:
    """Parse + locally validate an NID document text (fail-closed, ported).

    Mirrors identity-core ``verifyNidDocument`` for the pre-extracted-text
    path, with the spec/09 DOB cross-check added. When in doubt the result
    is a refusal status — never a synthetic match.
    """
    number = extract_nid_number(extracted_text)
    if number is None:
        return NidIntakeResult(
            status="extraction_failed", review_reason="extraction_failed"
        )
    if not is_valid_nid_format(number):
        return NidIntakeResult(
            status="invalid_format", review_reason="invalid_nid_format"
        )
    dob = extract_dob(extracted_text)
    if stated_dob_iso is not None and not dob_cross_check(number, stated_dob_iso):
        return NidIntakeResult(status="dob_mismatch", review_reason="dob_mismatch")
    name = extract_name(extracted_text)
    return NidIntakeResult(
        status="document_parsed_pending_government",
        review_reason="document_parsed_awaiting_government_check",
        nid=NidFields(number=number, dob=dob, name=name),
    )
