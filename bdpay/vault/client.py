"""Vault-proxy client — the egress contract `card_acquirer_v1` consumes
(spec/14 §Connector contract B).

The card connector never performs its own HTTPS call for PAN-bearing
messages: it builds the full acquirer request template and delegates egress
here. Two implementations of the binding Protocol:

- :class:`LocalVaultEgressClient` — in-process, wraps a
  :class:`~bdpay.vault.service.VaultService` directly (simulator-mode
  certification and unit tests; deterministic with an injected Clock).
- :class:`HttpVaultEgressClient` — the real wire client (sandbox/live):
  signs the shared-secret channel headers, posts to
  ``/vault/v1/detokenize-egress`` with ``Idempotency-Key = connector_ref``,
  and maps the error envelope back to the binding exceptions. Activation
  needs only base URL + channel secret configuration.

Exceptions are the binding pair from spec/14: ``VaultEgressDenied(code)``
for guard denials (the connector maps to ``ConnectorStatus.REJECTED``) and
``VaultEgressUpstreamError(code)`` for acquirer-unreachable/timeout (maps to
``TIMED_OUT`` — spec/11 owns the mapping).
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import httpx

from bdpay.platform.clock import Clock
from bdpay.platform.errors import BDPayError, ConnectorError
from bdpay.vault.channel import (
    CALLER_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    sign_channel_request,
)
from bdpay.vault.service import EGRESS_CALLER_SAN, VaultService, rfc3339

__all__ = [
    "HttpVaultEgressClient",
    "LocalVaultEgressClient",
    "VaultEgressClient",
    "VaultEgressDenied",
    "VaultEgressUpstreamError",
]


class VaultEgressDenied(Exception):
    """A detokenize-egress guard denied the call (connector: REJECTED)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class VaultEgressUpstreamError(Exception):
    """The vault made no/partial acquirer contact (connector: TIMED_OUT)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@runtime_checkable
class VaultEgressClient(Protocol):
    async def detokenize_egress(
        self,
        instruction_id: str,
        connector_ref: str,
        token: str,
        intake_session_id: str | None,
        egress_grant: dict,
        egress: dict,
    ) -> dict: ...


def _request_body(
    instruction_id: str,
    connector_ref: str,
    token: str,
    intake_session_id: str | None,
    egress_grant: dict,
    egress: dict,
) -> dict:
    return {
        "instruction_id": instruction_id,
        "connector_ref": connector_ref,
        "token": token,
        "intake_session_id": intake_session_id,
        "egress_grant": egress_grant,
        "egress": egress,
    }


class LocalVaultEgressClient:
    """In-process vault-proxy: calls the service as the card vault-proxy SAN."""

    def __init__(self, service: VaultService) -> None:
        self._service = service

    async def detokenize_egress(
        self,
        instruction_id: str,
        connector_ref: str,
        token: str,
        intake_session_id: str | None,
        egress_grant: dict,
        egress: dict,
    ) -> dict:
        try:
            return await self._service.detokenize_egress(
                caller_san=EGRESS_CALLER_SAN,
                request=_request_body(
                    instruction_id, connector_ref, token, intake_session_id, egress_grant, egress
                ),
            )
        except ConnectorError as exc:
            raise VaultEgressUpstreamError(exc.code) from exc
        except BDPayError as exc:
            raise VaultEgressDenied(exc.code) from exc


class HttpVaultEgressClient:
    """Real wire vault-proxy (sandbox/live; activation = config only).

    ``transport`` injection keeps the full wire path testable offline."""

    _PATH = "/vault/v1/detokenize-egress"

    def __init__(
        self,
        *,
        base_url: str,
        channel_secret: str,
        clock: Clock,
        caller_san: str = EGRESS_CALLER_SAN,
        verify: bool | str = True,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = channel_secret
        self._clock = clock
        self._caller = caller_san
        self._verify = verify
        self._transport = transport
        self._timeout_s = timeout_s

    async def detokenize_egress(
        self,
        instruction_id: str,
        connector_ref: str,
        token: str,
        intake_session_id: str | None,
        egress_grant: dict,
        egress: dict,
    ) -> dict:
        body = json.dumps(
            _request_body(
                instruction_id, connector_ref, token, intake_session_id, egress_grant, egress
            ),
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        ts = rfc3339(self._clock.now())
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": connector_ref,
            CALLER_HEADER: self._caller,
            TIMESTAMP_HEADER: ts,
            SIGNATURE_HEADER: sign_channel_request(
                self._secret,
                caller=self._caller,
                method="POST",
                path=self._PATH,
                body=body,
                ts=ts,
            ),
        }
        try:
            async with httpx.AsyncClient(
                verify=self._verify,
                transport=self._transport,
                timeout=httpx.Timeout(self._timeout_s),
            ) as client:
                response = await client.post(
                    self._base_url + self._PATH, content=body, headers=headers
                )
        except httpx.TimeoutException as exc:
            raise VaultEgressUpstreamError("vault_unreachable_timeout") from exc
        except httpx.TransportError as exc:
            raise VaultEgressUpstreamError("vault_unreachable") from exc
        if response.status_code < 300:
            return response.json()
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        code = str(error.get("code", "vault_error"))
        if response.status_code == 502:
            raise VaultEgressUpstreamError(code)
        raise VaultEgressDenied(code)
