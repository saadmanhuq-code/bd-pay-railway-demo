"""Refusal-first FSMs — spec/13 §State machines (conventions §8 notation).

Any (state, trigger) pair not in the table is DENIED with a ConflictError.
Side effects (audit/event emission) are executed by the service layer; this
module is the pure transition authority so the tables are unit-pinnable.
"""

from __future__ import annotations

from bdpay.platform.errors import ConflictError
from bdpay.qr.model import DynamicPayloadState, MerchantQrState

__all__ = [
    "DYNAMIC_PAYLOAD_TRANSITIONS",
    "MERCHANT_QR_TRANSITIONS",
    "advance_dynamic_payload",
    "advance_merchant_qr",
]

#: spec/13 FSM 1 — MerchantQr. REVOKED is terminal.
MERCHANT_QR_TRANSITIONS: dict[tuple[MerchantQrState, str], MerchantQrState] = {
    (MerchantQrState.DRAFT, "activate"): MerchantQrState.ACTIVE,
    (MerchantQrState.ACTIVE, "suspend"): MerchantQrState.SUSPENDED,
    (MerchantQrState.SUSPENDED, "reactivate"): MerchantQrState.ACTIVE,
    (MerchantQrState.ACTIVE, "revoke"): MerchantQrState.REVOKED,
    (MerchantQrState.SUSPENDED, "revoke"): MerchantQrState.REVOKED,
}

#: spec/13 FSM 2 — dynamic QrPayload. PAID/EXPIRED/CANCELLED are terminal.
DYNAMIC_PAYLOAD_TRANSITIONS: dict[tuple[DynamicPayloadState, str], DynamicPayloadState] = {
    (DynamicPayloadState.ISSUED, "scanned"): DynamicPayloadState.SCANNED,
    (DynamicPayloadState.ISSUED, "paid"): DynamicPayloadState.PAID,
    (DynamicPayloadState.SCANNED, "paid"): DynamicPayloadState.PAID,
    (DynamicPayloadState.ISSUED, "ttl_expired"): DynamicPayloadState.EXPIRED,
    (DynamicPayloadState.SCANNED, "ttl_expired"): DynamicPayloadState.EXPIRED,
    (DynamicPayloadState.ISSUED, "cancelled"): DynamicPayloadState.CANCELLED,
    (DynamicPayloadState.SCANNED, "cancelled"): DynamicPayloadState.CANCELLED,
}


def advance_merchant_qr(state: MerchantQrState, trigger: str) -> MerchantQrState:
    """Next MerchantQr state, or ConflictError (refusal-first)."""
    next_state = MERCHANT_QR_TRANSITIONS.get((state, trigger))
    if next_state is None:
        raise ConflictError(
            f"merchant qr transition denied: {state.value} --[{trigger}]-->",
            code="qr_invalid_transition",
        )
    return next_state


def advance_dynamic_payload(state: DynamicPayloadState, trigger: str) -> DynamicPayloadState:
    """Next dynamic payload state, or ConflictError (refusal-first)."""
    next_state = DYNAMIC_PAYLOAD_TRANSITIONS.get((state, trigger))
    if next_state is None:
        raise ConflictError(
            f"dynamic payload transition denied: {state.value} --[{trigger}]-->",
            code="qr_invalid_transition",
        )
    return next_state
