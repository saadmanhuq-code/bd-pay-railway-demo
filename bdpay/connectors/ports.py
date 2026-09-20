"""Narrow local ports for the connectors package (spec/10 Scope).

``bdpay.platform.interfaces`` / ``outbox`` do not exist yet (other lane), so
this package defines its own minimal Protocols and accepts them via
constructor injection with deterministic in-memory defaults. CERT-03 forbids
importing kernel/ledger/compliance here; the kernel handoff is a port.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from bdpay.connectors.sdk import ConnectorResult

__all__ = [
    "AcceptAllKernelHandoff",
    "AuditSink",
    "CompositeCredentialResolver",
    "CredentialMissError",
    "CredentialResolver",
    "EnvCredentialResolver",
    "EventSink",
    "InMemoryAuditSink",
    "InMemoryEventSink",
    "InMemoryObjectStore",
    "KernelHandoff",
    "ObjectStore",
    "OpenBaoKvCredentialResolver",
    "RecordingKernelHandoff",
    "StaticCredentialResolver",
    "raw_object_key",
]


class CredentialMissError(LookupError):
    """A credential ref could not be resolved (fail-closed trigger, spec/10)."""


def raw_object_key(connector_id: str, raw_response_hash: str) -> str:
    """Content-addressed object-store key for an archived raw wire response.

    The adapter (or pipeline) archives raw bytes here; ``connector_results``
    rows point at it via ``raw_response_pointer``; CERT-04 re-fetches and
    recomputes ``sha256_canonical`` from it.
    """
    return f"raw/{connector_id}/{raw_response_hash}"


@runtime_checkable
class AuditSink(Protocol):
    """Append-only audit trail port (spec/10 side_effects columns)."""

    def record(
        self, action: str, *, actor_id: str, occurred_at: datetime, detail: dict
    ) -> None: ...


@runtime_checkable
class EventSink(Protocol):
    """Outbox-bound event emission port (spec/10 Events Produced)."""

    def emit(self, event_type: str, payload: dict) -> None: ...


@runtime_checkable
class ObjectStore(Protocol):
    """Raw-payload archive port (12-yr retention object store, spec/10)."""

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, key: str) -> bytes: ...

    def exists(self, key: str) -> bool: ...


@runtime_checkable
class CredentialResolver(Protocol):
    """OpenBao-shaped resolver: returns the secret or raises on any miss."""

    def resolve(self, ref: str) -> str: ...


@runtime_checkable
class KernelHandoff(Protocol):
    """In-process kernel handoff port (spec/10 WebhookInbound FSM).

    Returns True when the kernel matched ``connector_ref`` and committed its
    transaction; False when no matching ``connector_ref`` exists (PARKED).
    """

    async def handle_connector_result(self, result: ConnectorResult) -> bool: ...


class InMemoryAuditSink:
    """Deterministic in-memory audit trail (append-only list)."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def record(self, action: str, *, actor_id: str, occurred_at: datetime, detail: dict) -> None:
        self.entries.append(
            {
                "action": action,
                "actor_id": actor_id,
                "occurred_at": occurred_at,
                "detail": dict(detail),
            }
        )

    def actions(self) -> list[str]:
        return [entry["action"] for entry in self.entries]


class InMemoryEventSink:
    """Deterministic in-memory event sink (append-only list)."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append({"type": event_type, "payload": dict(payload)})

    def types(self) -> list[str]:
        return [event["type"] for event in self.events]


class InMemoryObjectStore:
    """Deterministic in-memory object store keyed by string."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError(f"object store data must be bytes, got {type(data).__name__}")
        self._objects[key] = data
        return key

    def get(self, key: str) -> bytes:
        return self._objects[key]

    def exists(self, key: str) -> bool:
        return key in self._objects


class StaticCredentialResolver:
    """Resolver over a fixed ref -> secret mapping; any miss raises."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = dict(secrets or {})

    def resolve(self, ref: str) -> str:
        if ref not in self._secrets:
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        return self._secrets[ref]


class EnvCredentialResolver:
    """Resolve ``env:VAR_NAME`` refs from an environment mapping.

    Deployment config stores refs, not secret values. Any unsupported ref
    scheme, missing variable, or blank value fails closed.
    """

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def resolve(self, ref: str) -> str:
        if not isinstance(ref, str) or not ref.startswith("env:"):
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        name = ref.removeprefix("env:")
        if not name:
            raise CredentialMissError("credential ref unresolvable: env:")
        value = self._env.get(name)
        if value is None or not str(value).strip():
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        return str(value)


class CompositeCredentialResolver:
    """Try multiple resolvers in order; all misses remain fail-closed."""

    def __init__(self, resolvers: tuple[CredentialResolver, ...]) -> None:
        if not resolvers:
            raise ValueError("CompositeCredentialResolver requires at least one resolver")
        self._resolvers = resolvers

    def resolve(self, ref: str) -> str:
        last_error: Exception | None = None
        for resolver in self._resolvers:
            try:
                return resolver.resolve(ref)
            except CredentialMissError as exc:
                last_error = exc
        raise CredentialMissError(f"credential ref unresolvable: {ref}") from last_error


class OpenBaoKvCredentialResolver:
    """Resolve ``openbao:<mount>/<path>`` refs from OpenBao KV.

    KV v2 is tried first at ``/v1/<mount>/data/<path>``. A 404 then falls back
    to KV v1 at ``/v1/<mount>/<path>``. Secret data may be stored under a
    conventional ``value`` field, under the final path segment, or as the only
    field in the secret object. Ambiguous multi-field documents fail closed.
    """

    def __init__(
        self,
        *,
        addr: str,
        token: str,
        timeout_s: float = 5.0,
        client=None,
    ) -> None:
        if not addr or not addr.strip():
            raise ValueError("OpenBao addr must be non-empty")
        if not token or not token.strip():
            raise ValueError("OpenBao token must be non-empty")
        self._addr = addr.rstrip("/")
        self._token = token
        self._client = client
        self._timeout_s = timeout_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenBaoKvCredentialResolver | None:
        source = os.environ if env is None else env
        addr = (
            source.get("OPENBAO_ADDR")
            or source.get("BAO_ADDR")
            or source.get("VAULT_ADDR")
            or ""
        )
        token = (
            source.get("OPENBAO_TOKEN")
            or source.get("BAO_TOKEN")
            or source.get("VAULT_TOKEN")
            or ""
        )
        if not addr or not token:
            return None
        return cls(addr=addr, token=token)

    def resolve(self, ref: str) -> str:
        mount, path = self._parse_ref(ref)
        v2_path = f"/v1/{quote(mount, safe='')}/data/{quote(path, safe='/')}"
        data = self._read_json(v2_path)
        if data is not None:
            secret_data = data.get("data", {}).get("data")
            return self._extract_value(ref, path, secret_data)
        v1_path = f"/v1/{quote(mount, safe='')}/{quote(path, safe='/')}"
        data = self._read_json(v1_path)
        if data is not None:
            return self._extract_value(ref, path, data.get("data"))
        raise CredentialMissError(f"credential ref unresolvable: {ref}")

    @staticmethod
    def _parse_ref(ref: str) -> tuple[str, str]:
        if not isinstance(ref, str) or not ref.startswith("openbao:"):
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        target = ref.removeprefix("openbao:").strip("/")
        parts = target.split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        return parts[0], parts[1]

    def _headers(self) -> dict[str, str]:
        return {"X-Vault-Token": self._token}

    def _client_get(self, path: str):
        if self._client is not None:
            return self._client.get(path, headers=self._headers())
        import httpx

        with httpx.Client(
            base_url=self._addr, timeout=self._timeout_s, headers=self._headers()
        ) as client:
            return client.get(path)

    def _read_json(self, path: str) -> dict | None:
        try:
            response = self._client_get(path)
        except Exception as exc:
            raise CredentialMissError(f"credential ref read failed at {path}") from exc
        if response.status_code == 404:
            return None
        try:
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            raise CredentialMissError(f"credential ref read failed at {path}") from exc
        if not isinstance(data, dict):
            raise CredentialMissError(f"credential ref read failed at {path}")
        return data

    @staticmethod
    def _extract_value(ref: str, path: str, data) -> str:
        if not isinstance(data, dict) or not data:
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        leaf = path.rstrip("/").rsplit("/", 1)[-1]
        if "value" in data:
            value = data["value"]
        elif leaf in data:
            value = data[leaf]
        elif len(data) == 1:
            value = next(iter(data.values()))
        else:
            raise CredentialMissError(f"credential ref is ambiguous: {ref}")
        if value is None or not str(value).strip():
            raise CredentialMissError(f"credential ref unresolvable: {ref}")
        return str(value)


class AcceptAllKernelHandoff:
    """Kernel port that accepts every handoff (certification default)."""

    def __init__(self) -> None:
        self.results: list[ConnectorResult] = []

    async def handle_connector_result(self, result: ConnectorResult) -> bool:
        self.results.append(result)
        return True


class RecordingKernelHandoff:
    """Kernel port that accepts only known connector_refs (PARKED testing)."""

    def __init__(self, known_refs: set[str] | None = None) -> None:
        self.known_refs: set[str] = set(known_refs or set())
        self.results: list[ConnectorResult] = []

    async def handle_connector_result(self, result: ConnectorResult) -> bool:
        if result.connector_ref not in self.known_refs:
            return False
        self.results.append(result)
        return True
