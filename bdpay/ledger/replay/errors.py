"""Replay-package error taxonomy (extends the spec/03 ledger error hierarchy).

The package cannot edit ``bdpay/ledger/errors.py`` (another lane's file), so
the replay-specific classes are defined here, all rooted at
:class:`bdpay.ledger.errors.LedgerError` so callers catch one hierarchy.
"""

from __future__ import annotations

from bdpay.ledger.errors import LedgerError

__all__ = [
    "BundleAssemblyError",
    "ReplayError",
    "ReplayInvalidTransitionError",
    "ReplayNotFoundError",
]


class ReplayError(LedgerError):
    """Base class for replay-service failures (fail closed)."""


class ReplayNotFoundError(ReplayError):
    """The named replay record / bundle does not exist."""


class ReplayInvalidTransitionError(ReplayError):
    """Refusal-first FSM default: the requested transition is not in the table."""


class BundleAssemblyError(ReplayError):
    """A dispute-bundle assembly gate failed; carries the named fail_reason."""

    def __init__(self, fail_reason: str) -> None:
        super().__init__(fail_reason)
        self.fail_reason = fail_reason
