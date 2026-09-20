"""Deterministic Nagad PGW v3.3 host simulator (spec/12 §B, §K).

A fake rail HOST behind the ``WireTransport`` port implementing the Nagad
crypto scheme exactly as the merchant kit binds it, so the REAL
``NagadPgwConnector`` SANDBOX/PRODUCTION wire code runs unmodified:

- inbound ``sensitiveData`` is decrypted with NAGAD's private key and the
  ``signature`` verified against the MERCHANT public key — a request that
  fails either check is refused (no silent acceptance);
- responses carry ``sensitiveData`` encrypted to the MERCHANT public key and
  a ``signature`` by NAGAD's private key — which the adapter decrypts with
  its ``rsa_private`` and verifies with ``nagad_verify_public``;
- ``tamper_next_signature()`` corrupts exactly one response signature for the
  spec/12 §K ``signature_invalid_response`` scenario (adapter must FAIL
  closed with ``nagad_signature_invalid`` and archive the raw).

Flow surface: ``initialize/{merchantId}/{orderId}`` (challenge +
paymentReferenceId), ``complete/{paymentReferenceId}``, plain-JSON
``verify/payment/{paymentReferenceId}`` (the truth call), and
``refund/{paymentReferenceId}``. ``settle()`` scripts the customer outcome
(Success / Aborted / Failed). All ids are content-addressed from the orderId;
time comes from the injected Clock; no randomness in any observable branch
(RSA key material is injected fixture state).
"""

from __future__ import annotations

import hashlib

from bdpay.connectors.mfs.nagad import (
    rsa_decrypt_b64,
    rsa_encrypt_b64,
    rsa_sign_b64,
    rsa_verify_b64,
)
from bdpay.connectors.mfs.wire import WireResponse, parse_json_body
from bdpay.platform.canonical import canonical_json
from bdpay.platform.clock import Clock

__all__ = ["NagadSimulatorGateway"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class NagadSimulatorGateway:
    """Deterministic Nagad PGW endpoint behind the ``WireTransport`` port."""

    def __init__(
        self,
        *,
        clock: Clock,
        nagad_private_pem: str,
        nagad_public_pem: str,
        merchant_public_pem: str,
        merchant_id: str = "M0001",
    ) -> None:
        self._clock = clock
        self._nagad_private = nagad_private_pem
        self._nagad_public = nagad_public_pem
        self._merchant_public = merchant_public_pem
        self._merchant_id = merchant_id
        self._tamper_next = False
        self._payments: dict[str, dict] = {}  # orderId -> state
        self._ref_by_prid: dict[str, str] = {}
        self.initialize_calls: list[str] = []
        self.complete_calls: list[str] = []

    # -- scripting ---------------------------------------------------------------

    def tamper_next_signature(self) -> None:
        """Corrupt the next response signature (signature_invalid_response)."""
        self._tamper_next = True

    def settle(self, order_id: str, status: str) -> None:
        """Script the customer outcome: Success | Aborted | Cancelled | Failed."""
        payment = self._payments[order_id]
        payment["status"] = status
        if status == "Success":
            payment["issuerPaymentRefNo"] = f"ISS{_sha(order_id)[:10]}"

    # -- crypto plumbing ------------------------------------------------------------

    def _open_request(self, content: bytes) -> dict | None:
        """Decrypt + verify an inbound signed body; None = refused."""
        body = parse_json_body(content)
        cipher = body.get("sensitiveData")
        signature = body.get("signature")
        if not cipher or not signature:
            return None
        try:
            plaintext = rsa_decrypt_b64(self._nagad_private, str(cipher))
        except ValueError:
            return None
        if not rsa_verify_b64(self._merchant_public, plaintext, str(signature)):
            return None
        return parse_json_body(plaintext)

    def _sealed_response(self, payload: dict, *, status_code: int = 200) -> WireResponse:
        plaintext = canonical_json(payload)
        signature = rsa_sign_b64(self._nagad_private, plaintext)
        if self._tamper_next:
            self._tamper_next = False
            signature = ("A" if signature[0] != "A" else "B") + signature[1:]
        body = canonical_json(
            {
                "sensitiveData": rsa_encrypt_b64(self._merchant_public, plaintext),
                "signature": signature,
            }
        )
        return WireResponse(status_code=status_code, body=body)

    # -- the wire ----------------------------------------------------------------------

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        if "/check-out/initialize/" in url:
            return self._initialize(url, content)
        if "/check-out/complete/" in url:
            return self._complete(url, content)
        if "/verify/payment/" in url:
            return self._verify(url)
        if "/purchase/refund/" in url:
            return self._refund(url, content)
        return WireResponse(
            status_code=404, body=canonical_json({"status": "Failed", "reason": "unknown"})
        )

    def _initialize(self, url: str, content: bytes) -> WireResponse:
        order_id = url.rstrip("/").rsplit("/", 1)[-1]
        self.initialize_calls.append(order_id)
        opened = self._open_request(content)
        if opened is None:
            return self._sealed_response({"status": "Failed", "reason": "crypto_refused"})
        if str(opened.get("merchantId", "")) != self._merchant_id:
            return self._sealed_response({"status": "Failed", "reason": "merchant_unknown"})
        prid = f"NPR{_sha(order_id)[:10]}"
        self._payments.setdefault(
            order_id,
            {"status": "Initiated", "paymentReferenceId": prid, "issuerPaymentRefNo": None},
        )
        self._ref_by_prid[prid] = order_id
        return self._sealed_response(
            {
                "paymentReferenceId": prid,
                "challenge": _sha(f"challenge:{order_id}")[:20],
                "acceptDateTime": self._clock.now().strftime("%Y%m%d%H%M%S"),
            }
        )

    def _complete(self, url: str, content: bytes) -> WireResponse:
        prid = url.rstrip("/").rsplit("/", 1)[-1]
        order_id = self._ref_by_prid.get(prid, "")
        self.complete_calls.append(order_id)
        opened = self._open_request(content)
        if opened is None or not order_id:
            return self._sealed_response({"status": "Failed", "reason": "crypto_refused"})
        if str(opened.get("currencyCode", "")) != "050":
            return self._sealed_response({"status": "Failed", "reason": "currency_invalid"})
        payment = self._payments[order_id]
        payment["amount"] = str(opened.get("amount", ""))
        return self._sealed_response(
            {
                "status": "Initiated",
                "paymentReferenceId": prid,
                "callBackUrl": f"https://sim.nagad/redirect/{prid}",
            }
        )

    def _verify(self, url: str) -> WireResponse:
        prid = url.rstrip("/").rsplit("/", 1)[-1]
        order_id = self._ref_by_prid.get(prid, "")
        payment = self._payments.get(order_id)
        if payment is None:
            return WireResponse(
                status_code=200, body=canonical_json({"status": "Failed", "reason": "unknown"})
            )
        payload: dict = {"status": payment["status"], "orderId": order_id}
        if payment.get("issuerPaymentRefNo"):
            payload["issuerPaymentRefNo"] = payment["issuerPaymentRefNo"]
        return WireResponse(status_code=200, body=canonical_json(payload))

    def _refund(self, url: str, content: bytes) -> WireResponse:
        prid = url.rstrip("/").rsplit("/", 1)[-1]
        order_id = self._ref_by_prid.get(prid, "")
        opened = self._open_request(content)
        if opened is None or not order_id:
            return self._sealed_response({"status": "Failed", "reason": "crypto_refused"})
        payment = self._payments[order_id]
        if payment["status"] != "Success":
            return self._sealed_response({"status": "Failed", "reason": "not_refundable"})
        payment["status"] = "Refunded"
        return self._sealed_response(
            {
                "status": "Refunded",
                "issuerPaymentRefNo": payment["issuerPaymentRefNo"],
                "amount": str(opened.get("amount", "")),
            }
        )
