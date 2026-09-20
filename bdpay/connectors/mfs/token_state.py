"""MFS token cache FSM (spec/12 §State Machines 1) — bKash shape.

Nagad/aggregator adapters reuse the same FSM with ``refresh`` ≡ ``re-grant``.
States: NO_TOKEN / GRANTED / REFRESHING / EXPIRED / GRANT_FAILED; refusal-first
— any transition not in the spec table is DENIED. Binding fail-closed rule:
``submit()`` is refused (``error_code="mfs_auth_unavailable"``) while no valid
token exists — an unauthenticated financial message is never sent.

Token VALUES live only in this process's memory (prod target: encrypted Redis,
TTL 60min — never Postgres); the persisted/observable ``mfs_token_states`` row
carries the sha256 token hash only (CONN-03 / BP-G4b:
``PostgresTokenStateStore``). All time arrives via the injected Clock; refresh
backoff (5s, 15s, 45s) runs through the injected sleep so tests are instant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.connectors.mfs.spec12_ids import make_spec12_id
from bdpay.connectors.ports import (
    AuditSink,
    EventSink,
    InMemoryAuditSink,
    InMemoryEventSink,
)
from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConflictError, ConnectorError

__all__ = [
    "GrantOutcome",
    "MfsAuthUnavailableError",
    "MfsTokenRow",
    "TokenCache",
    "TokenStateStore",
    "InMemoryTokenStateStore",
    "PostgresTokenStateStore",
    "REFRESH_AFTER_S",
    "REFRESH_BACKOFF_S",
    "REFRESH_TOKEN_TTL_S",
]

#: Proactive refresh point: id_token TTL is 60min, refresh at 50min (spec/12).
REFRESH_AFTER_S = 50 * 60
#: Refresh-failure backoff ladder (spec/12 FSM table).
REFRESH_BACKOFF_S = (5, 15, 45)
#: bKash refresh_token lifetime: 28 days.
REFRESH_TOKEN_TTL_S = 28 * 24 * 3600

_STATES = frozenset({"NO_TOKEN", "GRANTED", "REFRESHING", "EXPIRED", "GRANT_FAILED"})

_ALLOWED = frozenset(
    {
        ("NO_TOKEN", "GRANTED"),
        ("NO_TOKEN", "GRANT_FAILED"),
        ("GRANTED", "REFRESHING"),
        ("REFRESHING", "GRANTED"),
        ("REFRESHING", "REFRESHING"),
        ("REFRESHING", "NO_TOKEN"),
        ("GRANTED", "EXPIRED"),
        ("EXPIRED", "GRANTED"),
        ("EXPIRED", "GRANT_FAILED"),
        ("GRANT_FAILED", "NO_TOKEN"),
    }
)

_SYSTEM_ACTOR = "system:mfs-token-cache"


class MfsAuthUnavailableError(ConnectorError):
    """Fail-closed refusal: no valid MFS token, submits must not reach the rail."""

    default_code = "mfs_auth_unavailable"


class TokenTransitionDeniedError(ConflictError):
    default_code = "mfs_token_transition_denied"


@dataclass(frozen=True)
class GrantOutcome:
    """One grant/refresh wire outcome handed to the FSM by the adapter."""

    ok: bool
    id_token: str | None = None
    refresh_token: str | None = None
    error_code: str | None = None


@dataclass
class MfsTokenRow:
    """One ``mfs_token_states`` row (hashes only — never token values)."""

    token_state_id: str
    connector_id: str
    mode: str
    state: str = "NO_TOKEN"
    token_hash: str | None = None
    granted_at: datetime | None = None
    refresh_expires_at: datetime | None = None
    consecutive_failures: int = 0
    updated_at: datetime | None = None
    schema_version: int = 1


@runtime_checkable
class TokenStateStore(Protocol):
    def load(self, connector_id: str, mode: str) -> MfsTokenRow | None: ...

    def save(self, row: MfsTokenRow) -> None: ...


ConnectionFactory = Callable[[], Any]


class InMemoryTokenStateStore:
    """Process-local token FSM rows. Pass a shared ``backing`` dict to simulate restart."""

    def __init__(
        self,
        backing: dict[tuple[str, str], MfsTokenRow] | None = None,
    ) -> None:
        self._rows: dict[tuple[str, str], MfsTokenRow] = (
            backing if backing is not None else {}
        )

    def load(self, connector_id: str, mode: str) -> MfsTokenRow | None:
        return self._rows.get((connector_id, mode))

    def save(self, row: MfsTokenRow) -> None:
        self._rows[(row.connector_id, row.mode)] = row


class PostgresTokenStateStore:
    """Postgres-backed ``mfs_token_states`` (migration ``0052_mfs_identity_connectors``).

    Persists FSM metadata + token *hashes* only — never id_token / refresh_token
    values (those remain process-memory / Redis).
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connect = connection_factory

    @staticmethod
    def _to_row(row: Any) -> MfsTokenRow:
        return MfsTokenRow(
            token_state_id=row["token_state_id"],
            connector_id=row["connector_id"],
            mode=row["mode"],
            state=row["state"],
            token_hash=row["token_hash"],
            granted_at=row["granted_at"],
            refresh_expires_at=row["refresh_expires_at"],
            consecutive_failures=int(row["consecutive_failures"]),
            updated_at=row["updated_at"],
            schema_version=int(row["schema_version"]),
        )

    def load(self, connector_id: str, mode: str) -> MfsTokenRow | None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT token_state_id, connector_id, mode, state, token_hash,
                           granted_at, refresh_expires_at, consecutive_failures,
                           updated_at, schema_version
                    FROM mfs_token_states
                    WHERE connector_id = %s AND mode = %s
                    """,
                    (connector_id, mode),
                )
                row = cur.fetchone()
        return None if row is None else self._to_row(row)

    def save(self, row: MfsTokenRow) -> None:
        from psycopg.rows import dict_row

        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    INSERT INTO mfs_token_states (
                        token_state_id, connector_id, mode, state, token_hash,
                        granted_at, refresh_expires_at, consecutive_failures,
                        updated_at, schema_version
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (connector_id, mode) DO UPDATE SET
                        state = EXCLUDED.state,
                        token_hash = EXCLUDED.token_hash,
                        granted_at = EXCLUDED.granted_at,
                        refresh_expires_at = EXCLUDED.refresh_expires_at,
                        consecutive_failures = EXCLUDED.consecutive_failures,
                        updated_at = EXCLUDED.updated_at,
                        schema_version = EXCLUDED.schema_version
                    """,
                    (
                        row.token_state_id,
                        row.connector_id,
                        row.mode,
                        row.state,
                        row.token_hash,
                        row.granted_at,
                        row.refresh_expires_at,
                        row.consecutive_failures,
                        row.updated_at,
                        row.schema_version,
                    ),
                )
            conn.commit()


class TokenCache:
    """Per-connector+mode token cache driving the spec/12 §1 FSM.

    ``grant`` and ``refresh`` are injected async callables returning a
    :class:`GrantOutcome` — the wire calls live in the adapter; this class owns
    only the FSM, the secrecy rule (hash persisted, value in memory), and the
    fail-closed posture.
    """

    def __init__(
        self,
        connector_id: str,
        mode: str,
        *,
        clock: Clock,
        grant,
        refresh=None,
        store: TokenStateStore | None = None,
        audit: AuditSink | None = None,
        events: EventSink | None = None,
        sleep=None,
    ) -> None:
        self.connector_id = connector_id
        self.mode = mode
        self._clock = clock
        self._grant = grant
        self._refresh = refresh if refresh is not None else grant
        self._store = store if store is not None else InMemoryTokenStateStore()
        self._audit = audit if audit is not None else InMemoryAuditSink()
        self._events = events if events is not None else InMemoryEventSink()
        self._sleep = sleep if sleep is not None else _no_sleep
        self._id_token: str | None = None
        self._refresh_token: str | None = None

    # -- row access -------------------------------------------------------------

    def row(self) -> MfsTokenRow:
        existing = self._store.load(self.connector_id, self.mode)
        if existing is not None:
            return existing
        fresh = MfsTokenRow(
            token_state_id=make_spec12_id(
                "mtok", {"connector_id": self.connector_id, "mode": self.mode}
            ),
            connector_id=self.connector_id,
            mode=self.mode,
            updated_at=self._clock.now(),
        )
        self._store.save(fresh)
        return fresh

    @property
    def state(self) -> str:
        return self.row().state

    @property
    def current_id_token(self) -> str | None:
        """In-memory ``id_token`` from the last grant/refresh (never persisted).

        Used by sync IPN truth ports (:class:`~bdpay.connectors.mfs.bkash.BkashSyncStatusTruth`)
        that cannot ``await ensure_token``. Absent after process restart until the
        next async grant cycle — fail-closed callers must treat ``None`` as
        ``mfs_auth_unavailable``.
        """
        return self._id_token

    @property
    def current_refresh_token(self) -> str | None:
        """The live ``refresh_token`` from the last grant/refresh (memory-only).

        The adapter's injected refresh callable reads this to send the real
        token on ``POST .../token/refresh`` — bKash requires the live value
        (cross-validated against 5 independent SDKs), never a literal."""
        return self._refresh_token

    # -- FSM advance (the only state writer) -------------------------------------

    def _advance(self, row: MfsTokenRow, to_state: str, *, audit_action: str | None = None) -> None:
        if to_state not in _STATES:
            raise ValueError(f"unknown token state {to_state!r}")
        if (row.state, to_state) not in _ALLOWED:
            raise TokenTransitionDeniedError(
                f"token FSM transition {row.state} -> {to_state} is DENIED (refusal-first)"
            )
        from_state = row.state
        row.state = to_state
        row.updated_at = self._clock.now()
        self._store.save(row)
        if audit_action is not None:
            self._audit.record(
                audit_action,
                actor_id=_SYSTEM_ACTOR,
                occurred_at=row.updated_at,
                detail={
                    "connector_id": self.connector_id,
                    "mode": self.mode,
                    "from": from_state,
                    "to": to_state,
                },
            )
        self._events.emit(
            "mfs_token.state_changed",
            {
                "connector_id": self.connector_id,
                "mode": self.mode,
                "from": from_state,
                "to": to_state,
            },
        )

    def _store_tokens(self, row: MfsTokenRow, outcome: GrantOutcome) -> None:
        self._id_token = outcome.id_token
        if outcome.refresh_token is not None:
            self._refresh_token = outcome.refresh_token
            row.refresh_expires_at = self._clock.now() + timedelta(seconds=REFRESH_TOKEN_TTL_S)
        row.token_hash = sha256_canonical({"token": outcome.id_token})
        row.granted_at = self._clock.now()
        row.consecutive_failures = 0

    # -- grant / refresh cycles ---------------------------------------------------

    async def _full_grant(self, row: MfsTokenRow) -> str:
        outcome: GrantOutcome = await self._grant()
        if outcome.ok and outcome.id_token:
            self._store_tokens(row, outcome)
            self._advance(row, "GRANTED", audit_action="MFS_TOKEN_GRANTED")
            return outcome.id_token
        self._id_token = None
        self._advance(row, "GRANT_FAILED", audit_action="MFS_TOKEN_GRANT_FAILED")
        raise MfsAuthUnavailableError(
            f"{self.connector_id} token grant failed; submits refused fail-closed"
        )

    async def _refresh_cycle(self, row: MfsTokenRow) -> str:
        """REFRESHING with backoff 5s/15s/45s; refresh_dead -> NO_TOKEN re-grant."""
        now = self._clock.now()
        refresh_dead = (
            row.refresh_expires_at is not None and now >= row.refresh_expires_at
        ) or self._refresh_token is None
        failures = 0
        while not refresh_dead:
            outcome: GrantOutcome = await self._refresh()
            if outcome.ok and outcome.id_token:
                self._store_tokens(row, outcome)
                self._advance(row, "GRANTED", audit_action="MFS_TOKEN_REFRESHED")
                return outcome.id_token
            failures += 1
            row.consecutive_failures = failures
            self._store.save(row)
            if failures >= len(REFRESH_BACKOFF_S):
                break
            self._advance(row, "REFRESHING")
            await self._sleep(REFRESH_BACKOFF_S[failures - 1])
        # refresh_token expired (28d) or 3 consecutive failures: full re-grant.
        self._id_token = None
        self._refresh_token = None
        self._advance(row, "NO_TOKEN")
        return await self._full_grant(row)

    # -- public surface -------------------------------------------------------------

    async def ensure_token(self) -> str:
        """Return a usable id_token or raise :class:`MfsAuthUnavailableError`.

        Drives the FSM: fresh grant from NO_TOKEN, proactive refresh past the
        50-minute point, re-grant from EXPIRED, and the 60s sweep gate out of
        GRANT_FAILED (``sweep`` must run first — fail-closed in between).
        """
        row = self.row()
        if row.state == "GRANT_FAILED":
            raise MfsAuthUnavailableError(
                f"{self.connector_id} token grant previously failed; awaiting 60s retry sweep"
            )
        if row.state == "NO_TOKEN":
            return await self._full_grant(row)
        if row.state == "EXPIRED":
            outcome: GrantOutcome = await self._grant()
            if outcome.ok and outcome.id_token:
                self._store_tokens(row, outcome)
                self._advance(row, "GRANTED", audit_action="MFS_TOKEN_GRANTED")
                return outcome.id_token
            self._advance(row, "GRANT_FAILED", audit_action="MFS_TOKEN_GRANT_FAILED")
            raise MfsAuthUnavailableError(f"{self.connector_id} re-grant failed; fail-closed")
        if row.state in {"GRANTED", "REFRESHING"}:
            age_s = (
                (self._clock.now() - row.granted_at).total_seconds()
                if row.granted_at is not None
                else REFRESH_AFTER_S
            )
            if row.state == "REFRESHING" or age_s >= REFRESH_AFTER_S:
                if row.state == "GRANTED":
                    self._advance(row, "REFRESHING")
                return await self._refresh_cycle(row)
            if self._id_token is None:
                # Process restart: row says GRANTED but the in-memory value is
                # gone (it is never persisted). Fail-closed into a re-grant.
                self._advance(row, "EXPIRED")
                return await self.ensure_token()
            return self._id_token
        raise MfsAuthUnavailableError(f"{self.connector_id} token state {row.state} unusable")

    def handle_rail_401(self) -> None:
        """Any rail call returning an auth error: GRANTED -> EXPIRED (spec table)."""
        row = self.row()
        if row.state == "GRANTED":
            self._id_token = None
            self._advance(row, "EXPIRED", audit_action="MFS_TOKEN_EXPIRED_ON_RAIL_401")

    def sweep(self) -> None:
        """The 60s retry sweep: GRANT_FAILED -> NO_TOKEN (next ensure re-grants)."""
        row = self.row()
        if row.state == "GRANT_FAILED":
            self._advance(row, "NO_TOKEN")


async def _no_sleep(_seconds: float) -> None:
    return None
