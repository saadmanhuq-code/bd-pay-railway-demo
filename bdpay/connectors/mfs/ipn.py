"""Query-confirmed IPN handler shared by the MFS adapters (spec/12 binding rule).

An IPN **alone never settles a payment**: (1) HMAC signature verification is
fail-closed — ``False`` on a missing header, bad timestamp, stale timestamp or
mismatch (the pipeline REJECTs; the edge response gives no oracle); (2)
``parse_event`` extracts the ``connector_ref`` and hands kernel a result built
from the injected TRUTH PORT (a server-side status re-query / recorded rail
truth), never from the IPN body. A forged-but-validly-signed IPN with wrong
claims therefore cannot move money (CERT-M1). HMAC lineage: the
Agentic-Transformation m12 verify-then-persist ingestion pattern (ADAPT,
re-implemented in Python per spec/12 module-reuse table).

``raw_response_hash`` is computed over the parsed IPN body, so CERT-04's
re-fetch of the archived raw bytes recomputes the hash exactly.
"""

from __future__ import annotations

import hmac as hmac_mod
import json
from datetime import datetime
from hashlib import sha256

from bdpay.connectors.mfs.wire import rfc3339
from bdpay.connectors.sdk import ConnectorResult, ConnectorStatus
from bdpay.connectors.webhooks import normalize_numeric_fields
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock

__all__ = ["QueryConfirmedIpnHandler"]


class QueryConfirmedIpnHandler:
    """``ConnectorWebhookHandler`` with HMAC verify + query-truth parsing.

    Header names/key are config (the SIMULATOR-mode defaults match the scenario
    engine's signed callbacks: HMAC-SHA256 over ``timestamp + "." + body``).
    """

    def __init__(
        self,
        connector_id: str,
        key: bytes,
        *,
        clock: Clock,
        truth_for=None,
        ref_fields: tuple[str, ...] = ("connector_ref",),
        numeric_fields: tuple[str, ...] = ("amount", "amount_minor"),
        max_skew_s: int = 300,
        signature_header: str = "x-sim-signature",
        timestamp_header: str = "x-sim-timestamp",
    ) -> None:
        self.connector_id = connector_id
        self._key = key
        self._clock = clock
        self._truth_for = truth_for
        self._ref_fields = ref_fields
        self._numeric_fields = numeric_fields
        self._max_skew_s = max_skew_s
        self._signature_header = signature_header
        self._timestamp_header = timestamp_header

    def verify_signature(self, headers: dict, body: bytes) -> bool:
        signature = headers.get(self._signature_header)
        timestamp = headers.get(self._timestamp_header)
        if not signature or not timestamp:
            return False
        try:
            stamped = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return False
        if stamped.tzinfo is None:
            return False
        if abs((self._clock.now() - stamped).total_seconds()) > self._max_skew_s:
            return False
        expected = hmac_mod.new(
            self._key, str(timestamp).encode("utf-8") + b"." + body, sha256
        ).hexdigest()
        return hmac_mod.compare_digest(expected, str(signature))

    def parse_event(self, headers: dict, body: bytes) -> ConnectorResult:
        payload = json.loads(body.decode("utf-8"), parse_float=str)
        normalized = normalize_numeric_fields(payload, self._numeric_fields)
        connector_ref = ""
        for field in self._ref_fields:
            value = normalized.get(field)
            if value:
                connector_ref = str(value)
                break
        if not connector_ref:
            raise ValueError(f"IPN carries none of the ref fields {self._ref_fields}")
        raw_hash = sha256_canonical(payload)
        truth = self._truth_for(connector_ref) if self._truth_for is not None else None
        if truth is not None:
            return ConnectorResult(
                instruction_id=truth.instruction_id,
                connector_ref=connector_ref,
                status=truth.status,
                rail_transaction_id=truth.rail_transaction_id,
                responded_at=rfc3339(self._clock.now()),
                error_code=truth.error_code,
                raw_response_hash=raw_hash,
            )
        return ConnectorResult(
            instruction_id=str(normalized.get("instruction_id", "")),
            connector_ref=connector_ref,
            status=ConnectorStatus.PENDING,
            rail_transaction_id=None,
            responded_at=rfc3339(self._clock.now()),
            error_code=None,
            raw_response_hash=raw_hash,
        )
