"""BEFTN return-file consumer: releases merchant holds on verified returns.

P0 bd-pay-beftn-settlement-completion (return-half ONLY).  This sweep polls
the sponsor-bank ``returns/`` directory, ingests each return/confirmation CSV,
and drives DISPATCHED disbursement items to RETURNED when the bank provides
positive evidence of a received return.  Amount mismatches, unmatched traces,
and late returns are escalated to ops and never auto-reversed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bdpay.connectors.rails.beftn import BeftnBatchConnector, BeftnReturnRow
from bdpay.kernel.disbursement.service import DisbursementService
from bdpay.kernel.disbursement.states import ITEM_TERMINAL_STATES
from bdpay.kernel.fsm import TransitionDeniedError
from bdpay.platform.clock import Clock
from bdpay.platform.errors import NotFoundError
from bdpay.platform.observability import MetricsRegistry

__all__ = ["BeftnReturnConsumer", "OpsAlertFn"]

OpsAlertFn = Callable[[str, dict[str, object]], None]

_RETURNS_DIR = "returns"
_DEFAULT_POLL_INTERVAL_SECONDS = 60.0


class BeftnReturnConsumer:
    """Scheduled consumer for BEFTN return files.

    Parameters
    ----------
    connector:
        Configured :class:`~bdpay.connectors.rails.beftn.BeftnBatchConnector`
        with a live (or simulator) SFTP transport.
    disbursement_service:
        The disbursement kernel service; ``mark_item_returned`` is the only
        state-changing money path touched here.
    clock:
        Injectable clock (no wall-clock reads anywhere else).
    metrics:
        Optional metrics registry; counters are emitted per disposition bucket.
    ops_alert:
        Optional human-pageable ops alert callable ``(kind, payload)``.  When
        unwired it defaults to a safe no-op, matching the sandbox signup
        service pattern.
    poll_interval_seconds:
        Sleep interval for the long-running ``run()`` loop.
    logger:
        Optional logger; defaults to ``bdpay.kernel.disbursement.beftn_return_consumer``.
    """

    def __init__(
        self,
        connector: BeftnBatchConnector,
        disbursement_service: DisbursementService,
        *,
        clock: Clock,
        metrics: MetricsRegistry | None = None,
        ops_alert: OpsAlertFn | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        self._connector = connector
        self._service = disbursement_service
        self._clock = clock
        self._metrics = metrics
        self._ops_alert = ops_alert or (lambda _kind, _payload: None)
        self._poll_interval_seconds = float(poll_interval_seconds)
        self._log = logger or logging.getLogger("bdpay.kernel.disbursement.beftn_return_consumer")
        #: In-memory guard against re-fetching the same unchanged file within
        #: one process lifetime.  Correctness (exactly-one JE) does NOT depend
        #: on this; the FSM/JE idempotency layer below is the real guard.
        self._processed_hashes: set[str] = set()

    async def run(self, *, max_iterations: int | None = None) -> dict[str, int]:
        """Long-running sweep loop.  ``max_iterations`` is TEST-ONLY."""
        total: dict[str, int] = {}
        iterations = 0
        while True:
            summary = await self.process_once()
            for key, value in summary.items():
                if isinstance(value, int):
                    total[key] = total.get(key, 0) + value
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return total
            await asyncio.sleep(self._poll_interval_seconds)

    async def process_once(self) -> dict[str, int]:
        """Poll ``returns/`` once and apply every row according to contract.

        Returns a structured summary with keys ``returned``, ``already_processed``,
        ``mismatch``, ``unmatched``, ``late``, and ``file_errors``.  A row that
        raises an unexpected exception is logged/alerted, the sweep continues,
        and is counted in ``file_errors``.
        """
        summary = {
            "returned": 0,
            "already_processed": 0,
            "mismatch": 0,
            "unmatched": 0,
            "late": 0,
            "file_errors": 0,
        }

        try:
            paths = await self._connector.list_return_files()
        except FileNotFoundError:
            # No returns directory yet; nothing to do.
            return summary
        except Exception as exc:  # noqa: BLE001 - sweep isolation
            self._log.exception("beftn-return sweep failed to list returns directory")
            self._ops_alert(
                "beftn_return_listdir_failed",
                {"error": repr(exc), "directory": _RETURNS_DIR},
            )
            summary["file_errors"] += 1
            self._inc("file_errors")
            return summary

        # Deterministic order; only CSV files are return/confirmation payloads.
        for path in sorted(p for p in paths if p.endswith(".csv")):
            try:
                await self._process_file(path, summary)
            except Exception as exc:  # noqa: BLE001 - per-file isolation
                self._log.exception("beftn-return sweep failed to process %s", path)
                self._ops_alert(
                    "beftn_return_file_failed",
                    {"file_path": path, "error": repr(exc)},
                )
                summary["file_errors"] += 1
                self._inc("file_errors")

        return summary

    async def _process_file(self, path: str, summary: dict[str, int]) -> None:
        data = await self._connector.get_return_file(path)
        report = self._connector.ingest_confirmation_csv(data)

        if report.raw_response_hash in self._processed_hashes:
            self._log.debug(
                "skipping already-processed return file %s (hash=%s)",
                path,
                report.raw_response_hash,
            )
            return

        self._log.info(
            "processing beftn return file %s: matched=%d unmatched=%d late=%d",
            path,
            len(report.matched),
            len(report.unmatched),
            len(report.late),
        )

        for row in report.matched:
            await self._process_matched_row(row, path, summary)

        for row in report.unmatched:
            self._log.warning(
                "beftn return unmatched row in %s: trace=%s return_code=%s",
                path,
                row.original_trace_number,
                row.return_code,
            )
            self._ops_alert(
                "beftn_return_unmatched",
                {"file_path": path, "row": self._row_payload(row)},
            )
            summary["unmatched"] += 1
            self._inc("unmatched")

        for row in report.late:
            self._log.warning(
                "beftn return late row in %s: trace=%s return_code=%s",
                path,
                row.original_trace_number,
                row.return_code,
            )
            self._ops_alert(
                "beftn_return_late",
                {"file_path": path, "row": self._row_payload(row)},
            )
            summary["late"] += 1
            self._inc("late")

        # Only mark the file after the full pass completes.  Correctness does
        # not depend on this in-memory guard (the FSM/JE idempotency layer is
        # the real guard); leaving the file unmarked on an unexpected row
        # error lets the next sweep retry the remaining rows.
        self._processed_hashes.add(report.raw_response_hash)

    async def _process_matched_row(
        self, row: BeftnReturnRow, path: str, summary: dict[str, int]
    ) -> None:
        if row.amount_mismatch:
            self._log.warning(
                "beftn return amount mismatch in %s: trace=%s amount_minor=%d",
                path,
                row.original_trace_number,
                row.amount_minor,
            )
            self._ops_alert(
                "beftn_return_amount_mismatch",
                {"file_path": path, "row": self._row_payload(row)},
            )
            summary["mismatch"] += 1
            self._inc("mismatch")
            return

        if row.entry_ref is None:
            # Defensive: ingest_confirmation_csv should never put None in matched.
            self._log.error(
                "beftn return matched row has no entry_ref: trace=%s",
                row.original_trace_number,
            )
            self._ops_alert(
                "beftn_return_unmatched",
                {"file_path": path, "row": self._row_payload(row)},
            )
            summary["unmatched"] += 1
            self._inc("unmatched")
            return

        try:
            self._service.mark_item_returned(
                row.entry_ref,
                rail_transaction_id=row.return_id,
                reason_code=row.error_code,
            )
        except TransitionDeniedError as exc:
            if exc.from_state in ITEM_TERMINAL_STATES:
                if exc.from_state == "RETURNED":
                    self._log.info(
                        "beftn return row already processed (item %s RETURNED)",
                        row.entry_ref,
                    )
                else:
                    self._log.warning(
                        "beftn return row for item %s in terminal state %s",
                        row.entry_ref,
                        exc.from_state,
                    )
                    self._ops_alert(
                        "beftn_return_terminal_state",
                        {
                            "file_path": path,
                            "row": self._row_payload(row),
                            "from_state": exc.from_state,
                        },
                    )
                summary["already_processed"] += 1
                self._inc("already_processed")
                return
            raise
        except NotFoundError:
            self._log.warning("beftn return matched row references unknown item %s", row.entry_ref)
            self._ops_alert(
                "beftn_return_unmatched",
                {"file_path": path, "row": self._row_payload(row)},
            )
            summary["unmatched"] += 1
            self._inc("unmatched")
            return

        self._log.info(
            "beftn return item %s marked RETURNED (return_id=%s reason=%s)",
            row.entry_ref,
            row.return_id,
            row.error_code,
        )
        summary["returned"] += 1
        self._inc("returned")

    @staticmethod
    def _row_payload(row: BeftnReturnRow) -> dict[str, object]:
        return {
            "return_id": row.return_id,
            "original_trace_number": row.original_trace_number,
            "return_code": row.return_code,
            "error_code": row.error_code,
            "amount_minor": row.amount_minor,
            "received_in_session": row.received_in_session,
            "entry_ref": row.entry_ref,
            "amount_mismatch": row.amount_mismatch,
            "late_return": row.late_return,
        }

    def _inc(self, bucket: str) -> None:
        if self._metrics is not None:
            self._metrics.increment(f"beftn_return_{bucket}_total")
