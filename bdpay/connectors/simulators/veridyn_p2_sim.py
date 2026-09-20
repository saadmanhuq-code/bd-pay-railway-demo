"""Deterministic Veridyn P2 compliance sidecar simulator (spec/12 §H, §K).

Two pieces for ``veridyn_p2_compliance_v1``:

- :class:`SimulatedP2Sidecar` — a fake SIDECAR HOST, not a fake adapter: it
  speaks the recorded ``/governance/check`` response shape (signed
  ``authoritative_output_envelope`` + detached Ed25519 signature, LB13), so
  the adapter's ingest/verify path runs unmodified in SIMULATOR mode with
  only the transport swapped.
- :class:`SimulatedP2Endpoint` — the same sidecar behind the ``WireTransport``
  port (bearer-token + tenant-header checked, fail-closed), so the REAL
  SANDBOX/PRODUCTION wire code is exercised against a scripted host without
  ever touching the live VM.

No randomness anywhere: the signing keypair derives from a fixed seed
(Ed25519 signatures are deterministic per RFC 8032), verdicts branch on
sha256 buckets of the fact bundle, and time comes from the injected Clock.
Scenario scripting rides a ``sim_scenario`` key inside the fact bundle
(``envelope_bad_signature`` / ``outage`` / ``hang``) — content, not mocks.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bdpay.connectors.identity.ids12 import sha_bucket
from bdpay.connectors.mfs.wire import WireResponse, parse_json_body, rfc3339
from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock

__all__ = [
    "SIM_SCENARIO_KEY",
    "SimulatedP2Endpoint",
    "SimulatedP2Sidecar",
    "build_signed_envelope_response",
    "sim_pack_hash",
]

#: Fact-bundle key that scripts simulator behavior (deterministic content).
SIM_SCENARIO_KEY = "sim_scenario"

SCENARIO_BAD_SIGNATURE = "envelope_bad_signature"
SCENARIO_OUTAGE = "outage"
SCENARIO_HANG = "hang"

#: Fixed 32-byte Ed25519 seed — same seed, same keypair, same signatures.
_SIM_KEY_SEED = hashlib.sha256(b"veridyn-p2-sim-signing-seed").digest()


def sim_pack_hash(pack_hint: str) -> str:
    """Deterministic compiled-pack identity echo for a pack hint."""
    return "pack_" + sha256_canonical({"pack_hint": pack_hint})[:24]


def build_signed_envelope_response(
    envelope: dict, private_key: Ed25519PrivateKey
) -> dict:
    """The recorded LB13 response shape: envelope + detached Ed25519 signature.

    The signature preimage is the ``sha256_canonical`` hex of the envelope,
    UTF-8 encoded — the same sign-the-content-hash convention the platform's
    pack-signing lineage uses, so one verification idiom serves both.
    """
    signature_hex = private_key.sign(
        sha256_canonical(envelope).encode("utf-8")
    ).hex()
    return {
        "authoritative_output_envelope": envelope,
        "envelope_signature": signature_hex,
    }


class SimulatedP2Sidecar:
    """Deterministic P2 sidecar host speaking the recorded response shape."""

    def __init__(self, *, clock: Clock, key_seed: bytes = _SIM_KEY_SEED) -> None:
        self._clock = clock
        self._key = Ed25519PrivateKey.from_private_bytes(key_seed)
        self.public_key_b64 = base64.b64encode(
            self._key.public_key().public_bytes_raw()
        ).decode("ascii")
        #: fact_bundle_hash -> number of checks actually served (rail effects).
        self.effects: dict[str, int] = {}

    def effect_count(self, fact_bundle_hash: str) -> int:
        return self.effects.get(fact_bundle_hash, 0)

    def _envelope(self, facts: dict, domain: str, pack_hint: str) -> dict:
        fact_bundle_hash = sha256_canonical(facts)
        bucket = sha_bucket(fact_bundle_hash)
        verdict = "REQUIRES_REVIEW" if bucket == 7 else "COMPLIANT"
        return {
            "schema": "authoritative_output_envelope/v1",
            "domain": domain,
            "pack_hash": sim_pack_hash(pack_hint),
            "fact_bundle_hash": fact_bundle_hash,
            "verdict": verdict,
            "citations": [
                {
                    "instrument": "PSS Act 2024",
                    "section": f"s{(bucket % 9) + 1}",
                },
                {
                    "instrument": "BPSSR 2014",
                    "regulation": f"r{(bucket % 5) + 1}",
                },
            ],
            "issued_at": rfc3339(self._clock.now()),
            # Open-schema pass-through probe: a field no client whitelist
            # knows about MUST survive verbatim into the stored envelope.
            "x_sim_unknown_extension": {"preserved": True, "bucket": bucket},
        }

    async def governance_check(self, body: dict) -> dict:
        """Serve one ``/governance/check`` body; scripted via the fact bundle."""
        facts = body.get("facts")
        if not isinstance(facts, dict):
            raise ValueError("governance_check body requires a dict 'facts'")
        scenario = str(facts.get(SIM_SCENARIO_KEY, ""))
        if scenario == SCENARIO_OUTAGE:
            raise ConnectionError("simulated P2 sidecar outage")
        if scenario == SCENARIO_HANG:
            await asyncio.Event().wait()  # never responds; hard timeout fires
        domain = str(body.get("domain", "payment_systems_bd"))
        pack_hint = str(body.get("pack_hint", "pack_veridyn_bd_full_compiled"))
        envelope = self._envelope(facts, domain, pack_hint)
        response = build_signed_envelope_response(envelope, self._key)
        if scenario == SCENARIO_BAD_SIGNATURE:
            # Tamper AFTER signing: the envelope arrives intact but the
            # signature can no longer verify against the pinned key.
            signature = response["envelope_signature"]
            flipped = "0" if signature[-1] != "0" else "1"
            response["envelope_signature"] = signature[:-1] + flipped
        self.effects[envelope["fact_bundle_hash"]] = (
            self.effects.get(envelope["fact_bundle_hash"], 0) + 1
        )
        return response


class SimulatedP2Endpoint:
    """The sidecar behind the ``WireTransport`` port (live wire-code bench).

    Auth posture is fail-closed like the real sidecar: a missing/wrong bearer
    token or a missing tenant header is a 401/400, never a signed envelope.
    """

    def __init__(
        self,
        sidecar: SimulatedP2Sidecar,
        *,
        expected_token: str,
        expected_tenant: str = "bdpay",
    ) -> None:
        self._sidecar = sidecar
        self._expected_token = expected_token
        self._expected_tenant = expected_tenant
        self.requests: list[dict] = []

    async def request(
        self, method: str, url: str, *, headers: dict, content: bytes
    ) -> WireResponse:
        normalized_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        self.requests.append(
            {"method": method, "url": url, "headers": normalized_headers}
        )
        if method == "GET" and url.rstrip("/").endswith("/health"):
            return WireResponse(
                status_code=200, body=canonical_json({"status": "ok"})
            )
        if normalized_headers.get("authorization") != f"Bearer {self._expected_token}":
            return WireResponse(
                status_code=401,
                body=canonical_json({"error": "invalid_client_token"}),
            )
        if normalized_headers.get("x-tenant-id") != self._expected_tenant:
            return WireResponse(
                status_code=400,
                body=canonical_json({"error": "tenant_header_missing"}),
            )
        if method != "POST" or not url.rstrip("/").endswith("/governance/check"):
            return WireResponse(
                status_code=404, body=canonical_json({"error": "unknown_route"})
            )
        response = await self._sidecar.governance_check(parse_json_body(content))
        return WireResponse(status_code=200, body=canonical_json(response))
