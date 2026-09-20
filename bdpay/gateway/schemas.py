"""Request body validation models (pydantic v2) — spec/01 §API surface.

Validation rules enforced here (binding):

- Money fields are integer paisa. Floats are REJECTED (never silently
  truncated); Bengali-numeral strings are normalized to ASCII before parsing
  (spec/00 §7 / spec/01 §C validation rules).
- ``metadata`` is string -> string with PII redaction applied server-side;
  a change of any value flags the ``W-PII-Stripped`` response header.
- ``statement_descriptor``: max 22 chars, printable ASCII, with the
  angle-bracket / backtick / backslash characters refused.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.functional_validators import BeforeValidator

from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.pii import redact

__all__ = [
    "CancelRequest",
    "CaptureRequest",
    "ConfirmRequest",
    "CustomerCreate",
    "DinerPayRequest",
    "MerchantCreate",
    "OperatorTotpVerifyRequest",
    "PaymentIntentCreate",
    "RefundCreate",
    "redact_metadata",
]

_FORBIDDEN_DESCRIPTOR_CHARS = set("<>`\\")


def _paisa(value: Any) -> int:
    """Integer paisa: int (not bool/float) or a digit string (Bengali ok)."""
    if isinstance(value, bool):
        raise ValueError("amount_minor must be an integer paisa amount")
    if isinstance(value, int):
        amount = value
    elif isinstance(value, float):
        raise ValueError("amount_minor must not be a float (integer paisa only)")
    elif isinstance(value, str):
        normalized = normalize_bengali_digits(value).strip()
        if not normalized.isdigit():
            raise ValueError("amount_minor string must contain only digits")
        amount = int(normalized, 10)
    else:
        raise ValueError("amount_minor must be an integer paisa amount")
    if amount <= 0:
        raise ValueError("amount_minor must be > 0")
    return amount


PaisaAmount = Annotated[int, BeforeValidator(_paisa)]


def redact_metadata(metadata: dict[str, str]) -> tuple[dict[str, str], bool]:
    """Server-side PII strip of metadata values (spec/01 §C); flags changes."""
    cleaned: dict[str, str] = {}
    changed = False
    for key, value in metadata.items():
        redacted = redact(value)
        if redacted != value:
            changed = True
        cleaned[key] = redacted
    return cleaned, changed


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class PaymentIntentCreate(_StrictModel):
    merchant_id: str = Field(min_length=1)
    amount_minor: PaisaAmount
    currency: Literal["BDT"] = "BDT"
    payment_method_types: list[str] = Field(min_length=1)
    capture_method: Literal["automatic", "manual"] = "automatic"
    confirm: bool = False
    customer_id: str | None = None
    description: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    return_url: str | None = None
    statement_descriptor: str | None = None

    @field_validator("statement_descriptor")
    @classmethod
    def _descriptor(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if len(value) > 22:
            raise ValueError("statement_descriptor must be at most 22 characters")
        for char in value:
            if not (32 <= ord(char) <= 126) or char in _FORBIDDEN_DESCRIPTOR_CHARS:
                raise ValueError(
                    "statement_descriptor must be printable ASCII without < > ` \\"
                )
        return value


class DinerPayRequest(_StrictModel):
    """Sandbox-only demo request: a diner pays a merchant in one tap.

    This surface exists ONLY in SANDBOX_PUBLIC / memory:// simulator deployments
    (spec/16 LR-4 §API F demo family). It deliberately does NOT accept a real
    customer-initiated payment authorization; the gateway internally reuses the
    merchant-scoped orchestrator path.
    """

    merchant_id: str = Field(min_length=1)
    customer_id: str = Field(min_length=1)
    amount_minor: PaisaAmount
    offer_id: str | None = None
    payment_method: str = Field(default="NPSB_IBFT", min_length=1)


class ConfirmRequest(_StrictModel):
    payment_method: dict[str, str] = Field(min_length=1)
    return_url: str | None = None
    otp_session_token: str | None = None


class CaptureRequest(_StrictModel):
    amount_minor: PaisaAmount


class CancelRequest(_StrictModel):
    cancellation_reason: Literal[
        "requested_by_customer", "duplicate", "fraudulent", "abandoned"
    ]


class RefundCreate(_StrictModel):
    payment_intent_id: str = Field(min_length=1)
    amount_minor: PaisaAmount
    currency: Literal["BDT"] = "BDT"
    reason: Literal["requested_by_customer", "duplicate", "fraudulent"]
    metadata: dict[str, str] = Field(default_factory=dict)


class MerchantCreate(_StrictModel):
    """Gateway validates the KYB envelope only; spec/08 owns the payload."""

    legal_name: str = Field(min_length=1)
    trade_name: str = Field(min_length=1)
    registration_number: str = Field(min_length=1)
    tin: str = Field(min_length=1)
    mcc: str = Field(min_length=1)
    settlement_bank_account: dict[str, str] = Field(min_length=1)
    contact: dict[str, str] = Field(min_length=1)
    ubo_nids: list[str] = Field(default_factory=list)
    legal_name_bn: str | None = None
    merchant_display_name: str | None = None
    business_city: str | None = None
    merchant_pan: str | None = None
    bangla_qr_enabled: bool = False


class CustomerCreate(_StrictModel):
    """eKYC submission envelope (spec/01 §G); NID forwarded in-process only."""

    phone: str = Field(min_length=1)
    name_en: str = Field(min_length=1)
    name_bn: str | None = None
    nid: str = Field(min_length=1)
    dob: str = Field(min_length=1)
    merchant_id: str | None = None


class OperatorTotpVerifyRequest(_StrictModel):
    operator_id: str = Field(min_length=1)
    totp_code: str = Field(min_length=1)
