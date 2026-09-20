"""Durable subject-control effects for compliance freezes and halts.

The compliance FSMs decide *when* a subject must be frozen or released. This
module owns the Postgres-backed effect state that the kernel preflight can read
before permitting money movement.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bdpay.platform.clock import Clock

__all__ = ["PostgresSubjectControl"]

ConnectionFactory = Callable[[], Any]

_FREEZE_SUBJECT = "freeze_subject"
_HALT_ONBOARDING = "halt_onboarding"
_FREEZE_PAYOUTS = "freeze_payouts"


class PostgresSubjectControl:
    """Postgres implementation of SubjectControlPort plus preflight reads."""

    def __init__(self, connection_factory: ConnectionFactory, *, clock: Clock) -> None:
        self._connect = connection_factory
        self._clock = clock

    def freeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._apply(
            _FREEZE_SUBJECT,
            subject_type,
            subject_id,
            reason=reason,
            reference_id=reference_id,
        )

    def unfreeze_subject(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        now = self._clock.now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT control_type
                FROM subject_controls
                WHERE subject_type = %s
                  AND subject_id = %s
                  AND released_at IS NULL
                ORDER BY control_type
                """,
                (subject_type, subject_id),
            ).fetchall()
            conn.execute(
                """
                UPDATE subject_controls
                SET released_at = %s,
                    release_reason = %s,
                    release_reference_id = %s
                WHERE subject_type = %s
                  AND subject_id = %s
                  AND released_at IS NULL
                """,
                (now, reason, reference_id, subject_type, subject_id),
            )
            for (control_type,) in rows:
                self._append_event(
                    conn,
                    subject_type,
                    subject_id,
                    control_type,
                    action="RELEASED",
                    reason=reason,
                    reference_id=reference_id,
                    occurred_at=now,
                )
            conn.commit()

    def halt_onboarding(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._apply(
            _HALT_ONBOARDING,
            subject_type,
            subject_id,
            reason=reason,
            reference_id=reference_id,
        )

    def freeze_payouts(
        self, subject_type: str, subject_id: str, *, reason: str, reference_id: str
    ) -> None:
        self._apply(
            _FREEZE_PAYOUTS,
            subject_type,
            subject_id,
            reason=reason,
            reference_id=reference_id,
        )

    def is_frozen(self, subject_type: str, subject_id: str) -> bool:
        return self._is_active(_FREEZE_SUBJECT, subject_type, subject_id)

    def is_onboarding_halted(self, subject_type: str, subject_id: str) -> bool:
        return self._is_active(_HALT_ONBOARDING, subject_type, subject_id)

    def is_payouts_frozen(self, subject_type: str, subject_id: str) -> bool:
        return self._is_active(_FREEZE_PAYOUTS, subject_type, subject_id)

    def _apply(
        self,
        control_type: str,
        subject_type: str,
        subject_id: str,
        *,
        reason: str,
        reference_id: str,
    ) -> None:
        now = self._clock.now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO subject_controls (
                    subject_type,
                    subject_id,
                    control_type,
                    reason,
                    reference_id,
                    applied_at,
                    released_at,
                    release_reason,
                    release_reference_id,
                    schema_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL, NULL, 1)
                ON CONFLICT (subject_type, subject_id, control_type)
                DO UPDATE SET
                    reason = EXCLUDED.reason,
                    reference_id = EXCLUDED.reference_id,
                    applied_at = EXCLUDED.applied_at,
                    released_at = NULL,
                    release_reason = NULL,
                    release_reference_id = NULL,
                    schema_version = EXCLUDED.schema_version
                """,
                (subject_type, subject_id, control_type, reason, reference_id, now),
            )
            self._append_event(
                conn,
                subject_type,
                subject_id,
                control_type,
                action="APPLIED",
                reason=reason,
                reference_id=reference_id,
                occurred_at=now,
            )
            conn.commit()

    def _is_active(self, control_type: str, subject_type: str, subject_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM subject_controls
                WHERE subject_type = %s
                  AND subject_id = %s
                  AND control_type = %s
                  AND released_at IS NULL
                """,
                (subject_type, subject_id, control_type),
            ).fetchone()
        return row is not None

    @staticmethod
    def _append_event(
        conn: Any,
        subject_type: str,
        subject_id: str,
        control_type: str,
        *,
        action: str,
        reason: str,
        reference_id: str,
        occurred_at: object,
    ) -> None:
        conn.execute(
            """
            INSERT INTO subject_control_events (
                subject_type,
                subject_id,
                control_type,
                action,
                reason,
                reference_id,
                occurred_at,
                schema_version
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, 1)
            ON CONFLICT DO NOTHING
            """,
            (
                subject_type,
                subject_id,
                control_type,
                action,
                reason,
                reference_id,
                occurred_at,
            ),
        )
