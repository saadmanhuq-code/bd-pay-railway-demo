"""Deterministic BEFTN sponsor-bank simulator (spec/11 §F beftn scenarios).

Models the sponsor-bank SFTP gateway as an in-memory file area: uploaded
files are validated against the or2020 layout dictionary by the SAME
parse-back pass the adapter uses (a file the bank cannot parse is REJECTED),
acknowledgment files are emitted as confirmation CSVs, and return files are
generated on demand for the return-window scenarios:

- ``partial_return_file``: returns for 2 of N entries (R01, R03) plus one
  unmatched trace number (recon-exception path);
- ``ack_total_mismatch``: the bank claims a control-total mismatch and the
  file is REJECTED (file immutability asserted adapter-side);
- ``late_return_after_window``: a return emitted 3 sessions after settlement
  (the adapter marks ``late_return=true`` and never auto-reverses).

Session time is an explicit integer counter advanced by the test/cert
driver — fully deterministic, no wall clock.
"""

from __future__ import annotations

from bdpay.connectors.rails.beftn_layout import BeftnParseError, parse_file
from bdpay.platform.clock import Clock

__all__ = ["BeftnSimulatorBank"]

_ACK_HEADER = "file_name,status,reason,entry_count,total_credit_minor,total_debit_minor"
_RETURN_HEADER = "original_trace_number,return_code,amount_minor,received_in_session"


class BeftnSimulatorBank:
    """In-memory SFTP area + deterministic ack/return generation."""

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._files: dict[str, bytes] = {}
        self.claim_total_mismatch = False
        self.session_counter = 0

    # -- the SFTP surface (consumed by the simulator transport) ----------------------

    async def put(self, remote_path: str, data: bytes) -> None:
        self._files[remote_path] = data
        if remote_path.startswith("outbound/") and remote_path.endswith(".beftn"):
            self._acknowledge(remote_path, data)

    async def get(self, remote_path: str) -> bytes:
        if remote_path not in self._files:
            raise FileNotFoundError(remote_path)
        return self._files[remote_path]

    async def listdir(self, remote_dir: str) -> list[str]:
        prefix = remote_dir.rstrip("/") + "/"
        return sorted(name for name in self._files if name.startswith(prefix))

    # -- bank behavior -----------------------------------------------------------------

    def advance_session(self, count: int = 1) -> int:
        self.session_counter += count
        return self.session_counter

    def _ack_name(self, file_path: str) -> str:
        base = file_path.rsplit("/", 1)[-1]
        return f"ack/{base}.ack.csv"

    def _acknowledge(self, file_path: str, data: bytes) -> None:
        base = file_path.rsplit("/", 1)[-1]
        signature = self._files.get(file_path + ".sig")
        if signature is None or not signature:
            self._write_ack(base, "REJECTED", "missing_signature", 0, 0, 0)
            return
        try:
            parsed = parse_file(data)
        except BeftnParseError as exc:
            self._write_ack(base, "REJECTED", f"layout:{exc}", 0, 0, 0)
            return
        if self.claim_total_mismatch:
            self._write_ack(base, "REJECTED", "control_total_mismatch",
                            parsed.entry_count, 0, 0)
            return
        self._write_ack(
            base,
            "ACCEPTED",
            "",
            parsed.entry_count,
            parsed.total_credit_minor,
            parsed.total_debit_minor,
        )

    def _write_ack(self, base: str, status: str, reason: str, entries: int,
                   credit: int, debit: int) -> None:
        line = f"{base},{status},{reason},{entries},{credit},{debit}"
        self._files[f"ack/{base}.ack.csv"] = (_ACK_HEADER + "\n" + line + "\n").encode("ascii")

    def emit_partial_returns(self, file_path: str) -> str:
        """Scenario ``partial_return_file``: R01 + R03 + one unmatched trace."""
        parsed = parse_file(self._files[file_path])
        if parsed.entry_count < 2:
            raise ValueError("partial_return_file needs at least 2 entries")
        session = self.session_counter
        rows = [
            f"{parsed.trace_numbers[0]},R01,{parsed.amounts_minor[0]},{session}",
            f"{parsed.trace_numbers[1]},R03,{parsed.amounts_minor[1]},{session}",
            # unmatched trace: same ODFI prefix, sequence far outside the file
            f"{parsed.trace_numbers[0][:8]}9999999,R02,1010,{session}",
        ]
        name = f"returns/{file_path.rsplit('/', 1)[-1]}.returns.csv"
        self._files[name] = (_RETURN_HEADER + "\n" + "\n".join(rows) + "\n").encode("ascii")
        return name

    def emit_late_return(self, file_path: str) -> str:
        """Scenario ``late_return_after_window``: one R01 in the CURRENT session
        (drive ``advance_session`` past the window before calling)."""
        parsed = parse_file(self._files[file_path])
        session = self.session_counter
        rows = [f"{parsed.trace_numbers[0]},R01,{parsed.amounts_minor[0]},{session}"]
        name = f"returns/{file_path.rsplit('/', 1)[-1]}.late.csv"
        self._files[name] = (_RETURN_HEADER + "\n" + "\n".join(rows) + "\n").encode("ascii")
        return name
