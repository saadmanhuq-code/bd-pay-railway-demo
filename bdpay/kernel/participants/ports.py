"""Narrow injected ports for the spec/19 PSO-1 onboarding service.

The onboarding service owns the participant FSM; everything it needs from
other packages arrives through these Protocols (the IMPLEMENTATION.md
storage/port posture — cross-package contracts are Protocols, wired at
composition). Each port ships a deterministic in-memory implementation used
by the unit suite and by simulator-only deployments.

- ``ParticipantLedgerPort`` — the spec/08 activation side-effect ("ledger:
  create participant_position + net_debit_cap accounts; DB-trigger cap
  live"); the real implementation is the ledger package's position module.
- ``NetDebitCapPort`` — the activation guard "an active NetDebitCap exists"
  (the cap row itself is spec/05-owned; this port only answers the guard).
- ``PsoBatteryGatePort`` — the spec/19 §API D PSO-readiness gate: every
  participant ``activate`` call requires the latest battery run for the
  current suite version to be ``PASSED`` and newer than the last schema
  migration applied. The battery itself is PSO-4-owned.
- ``ObjectStorePort`` — content storage for conformance evidence/report
  files (structurally identical to the connectors ``ObjectStore``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    "InMemoryNetDebitCapRegistry",
    "InMemoryObjectStore",
    "InMemoryPsoBatteryGate",
    "NetDebitCapPort",
    "ObjectStorePort",
    "ParticipantLedgerPort",
    "PsoBatteryGatePort",
    "RecordingParticipantLedger",
]


@runtime_checkable
class ParticipantLedgerPort(Protocol):
    """Activation side-effect: open the participant's ledger accounts."""

    def open_participant_accounts(
        self, participant_id: str, *, cap_minor: int, conn: object | None = None
    ) -> None: ...


@runtime_checkable
class NetDebitCapPort(Protocol):
    """The activation guard source for "an active NetDebitCap exists"."""

    def active_cap_minor(self, participant_id: str) -> int | None: ...


@runtime_checkable
class PsoBatteryGatePort(Protocol):
    """spec/19 §API D readiness gate; ``False`` => 409 ``pso_battery_stale``."""

    def battery_is_fresh_and_passed(self) -> bool: ...


@runtime_checkable
class ObjectStorePort(Protocol):
    """Content-addressed blob storage (put returns the pointer key)."""

    def put(self, key: str, data: bytes) -> str: ...

    def get(self, pointer: str) -> bytes: ...


# ---------------------------------------------------------------------------
# Deterministic in-memory implementations (unit suite + simulator posture)
# ---------------------------------------------------------------------------


class RecordingParticipantLedger:
    """In-memory ledger port; records every account-opening side-effect."""

    def __init__(self) -> None:
        self.opened: list[tuple[str, int]] = []

    def open_participant_accounts(
        self, participant_id: str, *, cap_minor: int, conn: object | None = None
    ) -> None:
        if isinstance(cap_minor, bool) or not isinstance(cap_minor, int) or cap_minor <= 0:
            raise ValueError(f"cap_minor must be a positive int (paisa), got {cap_minor!r}")
        self.opened.append((participant_id, cap_minor))


class InMemoryNetDebitCapRegistry:
    """In-memory NDC guard source (integer paisa caps)."""

    def __init__(self) -> None:
        self._caps: dict[str, int] = {}

    def set_cap(self, participant_id: str, cap_minor: int) -> None:
        if isinstance(cap_minor, bool) or not isinstance(cap_minor, int) or cap_minor <= 0:
            raise ValueError(f"cap_minor must be a positive int (paisa), got {cap_minor!r}")
        self._caps[participant_id] = cap_minor

    def active_cap_minor(self, participant_id: str) -> int | None:
        return self._caps.get(participant_id)


class InMemoryPsoBatteryGate:
    """In-memory battery gate; fails closed until explicitly marked green."""

    def __init__(self, *, fresh_and_passed: bool = False) -> None:
        self._green = fresh_and_passed

    def mark(self, *, fresh_and_passed: bool) -> None:
        self._green = fresh_and_passed

    def battery_is_fresh_and_passed(self) -> bool:
        return self._green


class InMemoryObjectStore:
    """Deterministic blob store (pointer == key)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        if not isinstance(data, bytes):
            raise TypeError(f"data must be bytes, got {type(data).__name__}")
        self._blobs[key] = data
        return key

    def get(self, pointer: str) -> bytes:
        if pointer not in self._blobs:
            raise KeyError(f"no object at {pointer!r}")
        return self._blobs[pointer]
