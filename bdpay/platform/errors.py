"""BDPayError hierarchy mirroring the spec 00 §4 error envelope (binding).

Every non-2xx response body is exactly::

    {
      "error": {
        "type": "<envelope type>",
        "code": "snake_case_specific_code",
        "message": "human-readable, NO PII, NO raw connector text",
        "request_id": "req_<id>",
        "doc_url": "https://docs.bdpay.example/errors/<code>"
      }
    }

``message`` is guaranteed PII-free: :func:`~bdpay.platform.pii.redact` is
applied in ``__init__`` before the message is stored, so no raw NID, mobile,
email, PAN, or account number can reach a response or a log via an exception.
HTTP status mirrors ``type`` per the binding table in spec 00 §4.
"""

from __future__ import annotations

import re
from typing import ClassVar

from bdpay.platform.pii import redact

__all__ = [
    "AmlBlockError",
    "AuthenticationError",
    "AuthorizationError",
    "BDPayError",
    "ConflictError",
    "ConnectorError",
    "IdempotencyConflictError",
    "InternalError",
    "InvalidRequestError",
    "LimitExceededError",
    "NotFoundError",
    "RateLimitError",
    "RequestTooLargeError",
    "SanctionsBlockError",
    "ServiceUnavailableError",
]

_DOC_URL_BASE = "https://docs.bdpay.example/errors/"
_SNAKE_CASE_RE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")

#: Binding type -> HTTP status table from spec 00 §4.
HTTP_STATUS_BY_TYPE: dict[str, int] = {
    "invalid_request": 400,
    "authentication": 401,
    "authorization": 403,
    "sanctions_block": 403,
    "aml_block": 403,
    "not_found": 404,
    "conflict": 409,
    "idempotency_conflict": 409,
    "limit_exceeded": 422,
    "rate_limit": 429,
    "request_too_large": 413,
    "connector_error": 502,
    "internal": 500,
    # spec/16 envelope amendment (additive to conventions §4): capacity
    # refusals defined by spec/16 only (e.g. ``kyc_queue_full``), carrying a
    # ``Retry-After`` header. See SPEC_ERRATA-LANE-A-spec16.md.
    "service_unavailable": 503,
}


class BDPayError(Exception):
    """Base of the envelope hierarchy. Instantiate a concrete subclass."""

    type: ClassVar[str]
    default_code: ClassVar[str]

    def __init__(self, message: str, *, code: str | None = None) -> None:
        if type(self) is BDPayError:
            raise TypeError("BDPayError is abstract; raise a concrete subclass")
        if not isinstance(message, str):
            raise TypeError(f"message must be str, got {type(message).__name__}")
        resolved_code = self.default_code if code is None else code
        if not _SNAKE_CASE_RE.fullmatch(resolved_code):
            raise ValueError(f"error code must be snake_case, got {resolved_code!r}")
        clean_message = redact(message)
        super().__init__(clean_message)
        self.message = clean_message
        self.code = resolved_code

    @property
    def http_status(self) -> int:
        return HTTP_STATUS_BY_TYPE[self.type]

    @property
    def doc_url(self) -> str:
        return f"{_DOC_URL_BASE}{self.code}"

    def to_envelope(self, request_id: str) -> dict:
        """The exact binding JSON envelope shape (spec 00 §4)."""
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("request_id must be a non-empty string")
        return {
            "error": {
                "type": self.type,
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
                "doc_url": self.doc_url,
            }
        }


class InvalidRequestError(BDPayError):
    """400 — malformed or semantically invalid request."""

    type = "invalid_request"
    default_code = "invalid_request"


class AuthenticationError(BDPayError):
    """401 — missing or invalid credentials."""

    type = "authentication"
    default_code = "authentication_failed"


class AuthorizationError(BDPayError):
    """403 — authenticated but not permitted."""

    type = "authorization"
    default_code = "not_authorized"


class RateLimitError(BDPayError):
    """429 — token-bucket limit exceeded."""

    type = "rate_limit"
    default_code = "rate_limit_exceeded"


class RequestTooLargeError(BDPayError):
    """413 — request body exceeds the configured edge cap."""

    type = "request_too_large"
    default_code = "request_body_too_large"


class IdempotencyConflictError(BDPayError):
    """409 — Idempotency-Key replayed with mismatched parameters."""

    type = "idempotency_conflict"
    default_code = "idempotency_key_conflict"


class LimitExceededError(BDPayError):
    """422 — tier/transaction/balance limit exceeded."""

    type = "limit_exceeded"
    default_code = "limit_exceeded"


class SanctionsBlockError(BDPayError):
    """403 — sanctions screening hit; refusal-first."""

    type = "sanctions_block"
    default_code = "sanctions_screen_hit"


class AmlBlockError(BDPayError):
    """403 — AML/CFT rule block; refusal-first."""

    type = "aml_block"
    default_code = "aml_rule_block"


class ConnectorError(BDPayError):
    """502 — downstream rail/connector failure (raw response never echoed)."""

    type = "connector_error"
    default_code = "connector_failure"


class ConflictError(BDPayError):
    """409 — state conflict (e.g. FSM transition denied)."""

    type = "conflict"
    default_code = "conflict"


class NotFoundError(BDPayError):
    """404 — resource does not exist or is not visible to the caller."""

    type = "not_found"
    default_code = "not_found"


class InternalError(BDPayError):
    """500 — unexpected platform failure."""

    type = "internal"
    default_code = "internal_error"


class ServiceUnavailableError(BDPayError):
    """503 — capacity refusal (spec/16 envelope amendment, additive).

    Used ONLY for the capacity refusals spec/16 defines (``kyc_queue_full``).
    ``retry_after_seconds`` rides the exception so the edge can emit the
    mandated ``Retry-After`` header without re-deriving drain projections.
    """

    type = "service_unavailable"
    default_code = "service_unavailable"

    def __init__(
        self, message: str, *, code: str | None = None, retry_after_seconds: int = 900
    ) -> None:
        if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int):
            raise TypeError("retry_after_seconds must be an int")
        if retry_after_seconds < 1:
            raise ValueError("retry_after_seconds must be >= 1")
        super().__init__(message, code=code)
        self.retry_after_seconds = retry_after_seconds
