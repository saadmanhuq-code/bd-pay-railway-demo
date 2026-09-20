"""Porichoy manual-review queue-depth cap (spec/16 §API I — LR-5).

Before accepting a KYC submission whose processing WOULD enter
``PENDING_HUMAN_REVIEW`` with ``review_reason = PORICHOY_UNAVAILABLE``
(spec/09 T10 — the identity provider is unavailable), the entry path checks
the open Porichoy-outage review depth against
``MANUAL_REVIEW_MAX_QUEUE_DEPTH``. At cap, the submission is refused with
``503 service_unavailable / kyc_queue_full`` and a ``Retry-After`` of the
projected drain time::

    retry_after = max(900, queue_depth * avg_review_seconds / active_reviewers)

The cap applies ONLY to the Porichoy-outage reason. Sanctions-adjacent,
minor-policy, and retrospective-hit reviews are NEVER refused — refusing a
compliance-mandated review is the worse outcome (spec/16 §I, binding).

Queue depth and the drain projection are exported as metrics inputs for the
spec/15 ops dashboard (``queue_depth()`` / ``projected_drain_seconds()``).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from bdpay.platform.config import Settings
from bdpay.platform.errors import ServiceUnavailableError

__all__ = [
    "PORICHOY_OUTAGE_REASON",
    "RETRY_AFTER_FLOOR_SECONDS",
    "PorichoyReviewQueueGuard",
]

PORICHOY_OUTAGE_REASON = "PORICHOY_UNAVAILABLE"

#: spec/16 §I: drain-time projection floor (seconds).
RETRY_AFTER_FLOOR_SECONDS = 900


@runtime_checkable
class ReviewDepthPort(Protocol):
    """The one read the guard needs (KycStore satisfies it)."""

    def count_open_reviews(
        self, review_reason: str, *, conn: Any | None = None
    ) -> int: ...


class PorichoyReviewQueueGuard:
    """Depth cap + 503 escape valve for the Porichoy-outage review queue."""

    def __init__(self, store: ReviewDepthPort, *, settings: Settings) -> None:
        self._store = store
        self._max_depth = settings.manual_review_max_queue_depth
        self._avg_review_seconds = settings.manual_review_avg_review_seconds
        self._active_reviewers = settings.manual_review_active_reviewers

    # -- metrics inputs (spec/15 dashboard panel) -----------------------------

    def queue_depth(self, *, conn: Any | None = None) -> int:
        return self._store.count_open_reviews(PORICHOY_OUTAGE_REASON, conn=conn)

    def projected_drain_seconds(self, depth: int) -> int:
        """``queue_depth × avg_review_seconds / active_reviewers``, floor 900."""
        projected = (depth * self._avg_review_seconds) // max(self._active_reviewers, 1)
        return max(RETRY_AFTER_FLOOR_SECONDS, projected)

    # -- the escape valve ------------------------------------------------------

    def check_capacity(self, *, conn: Any | None = None) -> None:
        """Refuse (503 ``kyc_queue_full``) when the outage queue is at cap.

        Called ONLY on the T10 entry path (Porichoy unavailable). All other
        review reasons bypass the guard entirely.
        """
        depth = self.queue_depth(conn=conn)
        if depth < self._max_depth:
            return
        raise ServiceUnavailableError(
            "identity verification is temporarily unavailable and the manual "
            "review queue is full; retry after the indicated interval",
            code="kyc_queue_full",
            retry_after_seconds=self.projected_drain_seconds(depth),
        )
