"""PII redaction at write — Bangladesh patterns (spec 00 §4, arch §(g)).

``redact`` is applied to every log line (via :class:`PIIRedactionFilter`) and
every error envelope message. Bengali digits are normalized to ASCII before
matching, so NIDs or mobile numbers written in Bengali numerals are caught.

Patterns, in match priority order:

- email address          -> ``em_`` + sha256(salt + lowercased email)[:12]
- BD mobile              -> ``[REDACTED-bd-mobile]``   (+8801[3-9]XXXXXXXX / 01[3-9]XXXXXXXX)
- NID 17 / 13 / 10 digit -> ``[REDACTED-nid]``         (standalone digit runs)
- PAN 14-16 digit run    -> ``pan_****`` + last 4, including common
  space/hyphen formatting
- account number 11+     -> ``[REDACTED-account]``

A standalone 13-digit run is treated as an NID, not a PAN: in the Bangladesh
context the 13-digit national ID is the dominant 13-digit identifier, and the
NID rule is listed first in spec 00. Replacement keeps the last 4 digits only
where the spec states it (PAN); everything else becomes ``[REDACTED-<kind>]``.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata

from bdpay.platform.bangla import normalize_bengali_digits

__all__ = ["PIIRedactionFilter", "redact"]

# Alternation order is the precedence order. All digit-run patterns are
# anchored with (?<!\d) / (?!\d) so they only hit standalone runs:
#  - email first (its local part may contain digit runs)
#  - mobiles next (an 11-digit 01[3-9] run must not fall through to "account")
#  - NID 17/13/10 before PAN, so 13-digit runs resolve as NID
#  - PAN 13-16 after NID therefore effectively captures 14-16 digit runs
#  - 11+ digit catch-all picks up the rest (11, 12, 18+ digit account numbers)
_PII_RE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"
    r"|(?P<mobile>(?:\+8801[3-9]\d{8}|(?<!\d)01[3-9]\d{8})(?!\d))"
    r"|(?P<nid>(?<!\d)(?:\d{17}|\d{13}|\d{10})(?!\d))"
    r"|(?P<pan>(?<!\d)\d{13,16}(?!\d))"
    r"|(?P<acct>(?<!\d)\d{11,}(?!\d))"
)
_FORMATTED_PAN_RE = re.compile(r"(?<!\d)(?:\d(?:[\s\u2010-\u2015-])?){13,15}\d(?!\d)")


def _hash_email(email: str, salt: str) -> str:
    digest = hashlib.sha256((salt + email.lower()).encode("utf-8")).hexdigest()
    return f"em_{digest[:12]}"


def _decimal_digits(value: str) -> str:
    return "".join(
        str(unicodedata.decimal(char)) for char in value if unicodedata.category(char) == "Nd"
    )


def _replacement(match: re.Match[str], email_salt: str) -> str:
    if match.group("email") is not None:
        return _hash_email(match.group("email"), email_salt)
    if match.group("mobile") is not None:
        return "[REDACTED-bd-mobile]"
    if match.group("nid") is not None:
        return "[REDACTED-nid]"
    if match.group("pan") is not None:
        digits = _decimal_digits(match.group("pan"))
        return f"pan_****{digits[-4:]}"
    return "[REDACTED-account]"


def _formatted_pan_replacement(match: re.Match[str]) -> str:
    digits = _decimal_digits(match.group(0))
    if 14 <= len(digits) <= 16:
        return f"pan_****{digits[-4:]}"
    return match.group(0)


def redact(text: str, *, email_salt: str = "") -> str:
    """Return ``text`` with all Bangladesh PII patterns replaced.

    Bengali digits are normalized to ASCII first, so identifiers written in
    Bengali numerals are matched; the normalized form is what is returned.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")
    normalized = normalize_bengali_digits(text)
    redacted = _PII_RE.sub(lambda match: _replacement(match, email_salt), normalized)
    return _FORMATTED_PAN_RE.sub(_formatted_pan_replacement, redacted)


_OPAQUE_ID_KEY_SUFFIXES = ("_id", "_ref")
_OPAQUE_ID_KEY_EXACT = frozenset({"idempotency_key"})


def is_opaque_identifier_key(key: str) -> bool:
    """True for payload keys that carry opaque, content-addressed identifiers or
    references (``payment_intent_id``, ``merchant_id``, ``rail_transaction_id``,
    ``idempotency_key`` …) rather than PII.

    Such values (``pi_…``, ``mrch_…``, NPSB RRNs) are NOT personal data — spec/00
    §4 keeps PII hashed/encrypted in ``kyc_records``, never in identifiers — yet
    :func:`redact` would corrupt any that contain a standalone 10/13/17-digit run
    (matched as an NID) or a 13–16-digit run (PAN). Exempting these keys keeps
    event/audit linkage intact (e.g. ``monitoring_rule_events.payment_intent_id``,
    alert ``contributing_payment_ids``) while every non-identifier string is still
    redacted. Free-text keys (``description``, ``reason``, ``*_name``) are NOT
    exempt and remain redacted.
    """
    return key in _OPAQUE_ID_KEY_EXACT or key.endswith(_OPAQUE_ID_KEY_SUFFIXES)


class PIIRedactionFilter(logging.Filter):
    """Logging filter that redacts the fully-formatted message in place.

    Attach to every handler (and the root logger) so no PII reaches any log
    sink. The record's args are consumed into the message before redaction,
    so formatting parameters cannot smuggle PII past the filter.
    """

    def __init__(self, name: str = "", *, email_salt: str = "") -> None:
        super().__init__(name)
        self._email_salt = email_salt

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), email_salt=self._email_salt)
        record.args = None
        return True
