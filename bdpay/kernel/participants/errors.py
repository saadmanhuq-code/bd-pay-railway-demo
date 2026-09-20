"""Typed refusals for the spec/19 PSO-1 participant onboarding surface.

``PsoModeError`` is the engine-layer mode gate spec/19 §Scope demands:
``ParticipantOnboardingService`` (v2 transitions) raises it when invoked
under ``PLATFORM_MODE=PSP``, and it is never caught-and-continued. It maps
to the binding API refusal ``403 authorization / platform_mode_pso_required``.

The ledger package defines its own ``PsoModeError`` for ledger-side PSO
operations; cross-package exception types live in ``bdpay.platform`` and the
platform error module is frozen for this lane, so the kernel defines its own
(errata row P19-9, pinned by tests).
"""

from __future__ import annotations

from bdpay.platform.errors import AuthorizationError

__all__ = ["PsoModeError"]


class PsoModeError(AuthorizationError):
    """A PSO-only operation was attempted while PLATFORM_MODE != PSO (refused)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="platform_mode_pso_required")
