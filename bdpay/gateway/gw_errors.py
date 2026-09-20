"""Gateway-specific error envelope refinements (errata G-2).

spec/00 §4 contains an internal contradiction: its status table maps
``idempotency_conflict`` to 409, while its idempotency bullet — and spec/01's
binding middleware algorithm, and the platform ``IdempotencyReservation``
docstring — all say a replay with mismatched parameters returns **422**.
Resolution (errata row G-2): the param-mismatch case keeps the envelope type
``idempotency_conflict`` and carries HTTP **422**; the duplicate-in-flight
case is ``409 conflict / idempotency_processing``. The platform
``IdempotencyConflictError`` (409) stays untouched for any other use.
"""

from __future__ import annotations

from bdpay.platform.errors import IdempotencyConflictError

__all__ = ["IdempotencyParamMismatchError"]


class IdempotencyParamMismatchError(IdempotencyConflictError):
    """422 — Idempotency-Key replayed with different request parameters.

    Envelope type stays ``idempotency_conflict`` (inherited); only the HTTP
    status diverges from the platform table, per errata G-2.
    """

    default_code = "idempotency_param_mismatch"

    @property
    def http_status(self) -> int:
        return 422
