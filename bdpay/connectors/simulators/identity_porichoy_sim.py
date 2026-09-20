"""Deterministic Porichoy NID-gateway host simulator (spec/12 §E, §K).

A fake Porichoy HTTP endpoint behind the ``WireTransport`` port so the REAL
``PorichoyEkycConnector`` SANDBOX/PRODUCTION wire code runs unmodified.
Behavior is the post-May-2025 binary model: the response carries ONLY
``verified`` (matched / not matched) and a verification reference — no citizen
data ever crosses back.

Determinism: the matched verdict is a pure function of ``sha256(nid + dob)``
(bucket rule identical to the adapter's SIMULATOR mode) unless explicitly
scripted per NID; the verification id is content-addressed; outage and 5xx
storms are explicit scripts driving the §K ``outage`` (breaker OPEN -> typed
unavailable -> spec/09 routes PENDING_HUMAN_REVIEW) and retry-budget paths.
"""

from __future__ import annotations

import hashlib

from bdpay.connectors.identity.ids12 import sha_bucket
from bdpay.connectors.mfs.wire import WireResponse, parse_json_body
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = ["PorichoySimulatorGateway"]


class PorichoySimulatorGateway:
    """Deterministic Porichoy endpoint behind the ``WireTransport`` port."""

    def __init__(self, *, clock: Clock, api_key: str = "porichoy-sim-key") -> None:
        self._clock = clock
        self._api_key = api_key
        self._fail_transport = 0
        self._serve_500 = 0
        self._scripted: dict[str, bool] = {}  # nid -> matched verdict
        self.calls: list[str] = []  # nid values seen (test-only visibility)

    # -- scripting ------------------------------------------------------------------

    def outage(self, count: int) -> None:
        """The next ``count`` calls fail at transport level (breach-era model)."""
        self._fail_transport = count

    def serve_500(self, count: int) -> None:
        self._serve_500 = count

    def script_verdict(self, nid: str, matched: bool) -> None:
        self._scripted[nid] = matched

    # -- the wire ---------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        if self._fail_transport > 0:
            self._fail_transport -= 1
            raise ConnectionError("simulated porichoy outage")
        if self._serve_500 > 0:
            self._serve_500 -= 1
            return WireResponse(
                status_code=500, body=canonical_json({"error": "gateway_unavailable"})
            )
        if headers.get("x-api-key", "") != self._api_key:
            return WireResponse(
                status_code=200,
                body=canonical_json({"verified": False, "error": "org_credential_invalid"}),
            )
        body = parse_json_body(content)
        nid = str(body.get("national_id", ""))
        dob = str(body.get("dob", ""))
        self.calls.append(nid)
        if nid in self._scripted:
            matched = self._scripted[nid]
        else:
            # Same deterministic rule as the adapter's SIMULATOR mode.
            matched = sha_bucket(nid + dob) != 7
        verification_id = "POR" + hashlib.sha256(f"{nid}:{dob}".encode()).hexdigest()[:12]
        return WireResponse(
            status_code=200,
            body=canonical_json({"verified": matched, "verification_id": verification_id}),
        )
