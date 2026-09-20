"""RoutingEngine — method + rail selection (spec/02 §Routing algorithm).

Maps a payment method to its candidate connector ids, filters on connector
health/circuit/window state, and degrades gracefully: BEFTN supports
store-and-forward when its window is closed; every other method declines
with a typed connector error (``502 connector_error /
rail_temporarily_unavailable``) — never a silent default.

The health view is a kernel-owned read port; spec/10's connector registry
satisfies it structurally at wiring time (the registry owns circuit breaker,
health cache, and window gates — the kernel only asks "may I dispatch to
this connector right now?").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.routing_models import (
    RoutingCandidateScore,
    RoutingCandidateSignal,
    RoutingPolicyRecord,
    RoutingPolicyStore,
    RoutingSignalStore,
    make_routing_decision,
)
from bdpay.platform.clock import Clock
from bdpay.platform.errors import ConnectorError, InvalidRequestError

__all__ = [
    "METHOD_CONNECTORS",
    "STORE_AND_FORWARD",
    "ConnectorHealthPort",
    "NoConnectorAvailableError",
    "RoutingEngine",
]

#: spec/02 routing rules — method -> ordered candidate connector ids (v1 is
#: single-candidate per method; the failover slot is the list shape).
METHOD_CONNECTORS: dict[str, tuple[str, ...]] = {
    "NPSB_IBFT": ("npsb_iso8583_v28",),
    "BEFTN_CREDIT": ("beftn_batch_v2",),
    "BKASH": ("bkash_pgw_v2",),
    "NAGAD": ("nagad_pgw_v33",),
    "CARD": ("card_acquirer_v1",),
    "BANGLA_QR": ("npsb_iso8583_v28",),  # QR routes via NPSB
    "ROCKET": ("rocket_aggregator_v1",),
}

#: Sentinel returned when the method supports deferral instead of declining.
STORE_AND_FORWARD = "STORE_AND_FORWARD"

#: Methods that may defer to the next rail window instead of declining.
_DEFERRABLE_METHODS = frozenset({"BEFTN_CREDIT"})


class NoConnectorAvailableError(ConnectorError):
    """No healthy connector for the method — graceful decline (502)."""

    default_code = "rail_temporarily_unavailable"


@runtime_checkable
class ConnectorHealthPort(Protocol):
    """May the kernel dispatch to this connector right now?

    Implementations answer False when the circuit breaker is open, the last
    health check failed, the rail window is closed, or the connector mode is
    disabled. Fails closed: unknown connector ids answer False.
    """

    def is_dispatchable(self, connector_id: str, *, clock: Clock) -> bool: ...


class AlwaysHealthy:
    """Default health view for unit composition: everything dispatchable."""

    def is_dispatchable(self, connector_id: str, *, clock: Clock) -> bool:
        return connector_id in {c for cands in METHOD_CONNECTORS.values() for c in cands}


class RoutingEngine:
    """Select the dispatch connector for an intent (spec/02 algorithm)."""

    def __init__(
        self,
        health: ConnectorHealthPort | None = None,
        *,
        smart_routing_enabled: bool = False,
        routing_policies: RoutingPolicyStore | None = None,
        routing_signals: RoutingSignalStore | None = None,
        method_connectors: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._health = health if health is not None else AlwaysHealthy()
        self._smart_routing_enabled = smart_routing_enabled
        self._routing_policies = routing_policies
        self._routing_signals = routing_signals
        self._method_connectors = _normalize_method_connectors(
            METHOD_CONNECTORS if method_connectors is None else method_connectors
        )

    def select_connector(
        self,
        method: str,
        *,
        clock: Clock,
        amount_minor: int | None = None,
        payment_attempt_id: str | None = None,
        conn: Any | None = None,
    ) -> str:
        """Return a connector id, or :data:`STORE_AND_FORWARD` for deferrable
        methods with no healthy connector; raise
        :class:`NoConnectorAvailableError` otherwise (refusal, not fallback).
        """
        candidates = self._method_connectors.get(method)
        if candidates is None:
            raise InvalidRequestError(
                f"unknown payment method {method!r}", code="method_unsupported"
            )
        passing = tuple(
            connector_id
            for connector_id in candidates
            if self._health.is_dispatchable(connector_id, clock=clock)
        )
        if not passing:
            return self._no_passing_connector(method)
        first_passing = passing[0]
        if not self._smart_routing_enabled or len(passing) < 2:
            return first_passing
        if self._routing_policies is None or self._routing_signals is None:
            return first_passing
        policy = self._routing_policies.get_active_policy(method, conn=conn)
        if policy is None:
            return first_passing
        now = clock.now()
        if amount_minor is None:
            return self._fallback_to_first(
                policy,
                method=method,
                passing=passing,
                selected=first_passing,
                decided_at=now,
                payment_attempt_id=payment_attempt_id,
                reason="amount_missing",
                conn=conn,
            )
        window_start = now - timedelta(hours=policy.window_hours)
        signals: list[RoutingCandidateSignal] = []
        for connector_id in passing:
            signal = self._routing_signals.signal_for(
                connector_id,
                method=method,
                amount_minor=amount_minor,
                window_start=window_start,
                now=now,
                conn=conn,
            )
            if signal is None:
                return self._fallback_to_first(
                    policy,
                    method=method,
                    passing=passing,
                    selected=first_passing,
                    decided_at=now,
                    payment_attempt_id=payment_attempt_id,
                    reason="missing_routing_signal",
                    conn=conn,
                )
            signals.append(signal)
        scores = _score_candidates(policy, signals)
        selected = scores[0].connector_id
        if payment_attempt_id is not None:
            snapshot = _inputs_snapshot(
                policy,
                method=method,
                amount_minor=amount_minor,
                passing=passing,
                signals=tuple(signals),
            )
            decision = make_routing_decision(
                payment_attempt_id=payment_attempt_id,
                policy=policy,
                candidates=scores,
                selected_connector_id=selected,
                inputs_snapshot=snapshot,
                decided_at=now,
            )
            self._routing_policies.record_decision(decision, conn=conn)
        return selected

    def _no_passing_connector(self, method: str) -> str:
        if method in _DEFERRABLE_METHODS:
            return STORE_AND_FORWARD
        raise NoConnectorAvailableError(
            f"no healthy connector for method {method}; payment cannot be dispatched"
        )

    def _fallback_to_first(
        self,
        policy: RoutingPolicyRecord,
        *,
        method: str,
        passing: tuple[str, ...],
        selected: str,
        decided_at,
        payment_attempt_id: str | None,
        reason: str,
        conn: Any | None,
    ) -> str:
        if payment_attempt_id is not None:
            fallback_scores = tuple(
                RoutingCandidateScore(
                    connector_id=connector_id,
                    score_bps=0,
                    success_rate_bps=0,
                    health_bps=0,
                    fee_minor=0,
                    latency_ms=0,
                    fee_rank_bps=0,
                    latency_rank_bps=0,
                )
                for connector_id in passing
            )
            snapshot = {
                "method": method,
                "policy_id": policy.policy_id,
                "policy_version": policy.version,
                "window_hours": policy.window_hours,
                "passing_connectors": list(passing),
                "fallback_reason": reason,
            }
            decision = make_routing_decision(
                payment_attempt_id=payment_attempt_id,
                policy=policy,
                candidates=fallback_scores,
                selected_connector_id=selected,
                inputs_snapshot=snapshot,
                decided_at=decided_at,
                fallback=True,
                fallback_reason=reason,
            )
            self._routing_policies.record_decision(decision, conn=conn)
        return selected


def _normalize_method_connectors(
    method_connectors: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    normalized: dict[str, tuple[str, ...]] = {}
    for method, connectors in method_connectors.items():
        if not isinstance(method, str) or not method:
            raise InvalidRequestError("routing method keys must be non-empty strings")
        candidate_tuple = tuple(connectors)
        if not candidate_tuple or any(not isinstance(c, str) or not c for c in candidate_tuple):
            raise InvalidRequestError("routing connector candidates must be non-empty strings")
        normalized[method] = candidate_tuple
    return normalized


def _rank_low_is_good(values: Mapping[str, int]) -> dict[str, int]:
    unique_values = sorted(set(values.values()))
    if len(unique_values) == 1:
        return {connector_id: 0 for connector_id in values}
    rank_by_value = {
        value: (index * 10_000) // (len(unique_values) - 1)
        for index, value in enumerate(unique_values)
    }
    return {connector_id: rank_by_value[value] for connector_id, value in values.items()}


def _score_candidates(
    policy: RoutingPolicyRecord, signals: list[RoutingCandidateSignal]
) -> tuple[RoutingCandidateScore, ...]:
    fee_ranks = _rank_low_is_good({signal.connector_id: signal.fee_minor for signal in signals})
    latency_ranks = _rank_low_is_good(
        {signal.connector_id: signal.latency_ms for signal in signals}
    )
    weights = policy.weights
    scored = []
    for signal in signals:
        fee_rank = fee_ranks[signal.connector_id]
        latency_rank = latency_ranks[signal.connector_id]
        score = (
            weights.success_bps * signal.success_rate_bps
            + weights.health_bps * signal.health_bps
            + weights.fee_bps * (10_000 - fee_rank)
            + weights.latency_bps * (10_000 - latency_rank)
        ) // 10_000
        scored.append(
            RoutingCandidateScore(
                connector_id=signal.connector_id,
                score_bps=score,
                success_rate_bps=signal.success_rate_bps,
                health_bps=signal.health_bps,
                fee_minor=signal.fee_minor,
                latency_ms=signal.latency_ms,
                fee_rank_bps=fee_rank,
                latency_rank_bps=latency_rank,
            )
        )
    return tuple(sorted(scored, key=lambda row: (-row.score_bps, row.connector_id)))


def _inputs_snapshot(
    policy: RoutingPolicyRecord,
    *,
    method: str,
    amount_minor: int,
    passing: tuple[str, ...],
    signals: tuple[RoutingCandidateSignal, ...],
) -> dict[str, object]:
    return {
        "method": method,
        "amount_minor": amount_minor,
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "weights": policy.weights.as_payload(),
        "window_hours": policy.window_hours,
        "passing_connectors": list(passing),
        "signals": [signal.as_snapshot() for signal in signals],
    }
