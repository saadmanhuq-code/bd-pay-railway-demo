"""BEFTN fixed-width record layouts, renderer and parse-back (spec/11 §B.1).

All records are exactly 94 ASCII characters, ``\\n``-terminated; the file is
padded with ``9``-filled records to a multiple of 10 (blocking factor 10).
Amounts are zero-padded INTEGER PAISA (conventions §6, 1x — no scaling).
The header ``reference_code`` field (pos 87-94) is zero-filled at hash time:
``file_content_hash`` = sha256 over the rendered bytes with that field
zeroed; the final file carries the first 8 hex chars of the hash there.

Renderer invariants (CERT-R3, tested): rendered bytes are a pure function of
(rows, layout config, effective date, file modifier, creation instant);
an independent parse-back pass reproduces entry count, entry hash and
debit/credit paisa totals exactly, byte-identically on re-render.

Validation-code seam adapted from Talent_1.0 ``beftn.ts`` Half-1
(``collectBeftnExportRows`` — per-row code list, invalid rows never enter the
file); the renderer itself is fresh (source Half-2 was unimplemented,
research 07). Row-mapping pitfalls carried over: routing must be exactly 9
digits with a valid 3-7-1 check digit; amounts must be positive integers;
names are length-capped; unresolved account tokens block the row.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime

from bdpay.connectors.rails.ports import DHAKA_TZ, Detokenizer
from bdpay.platform.bangla import normalize_bengali_digits
from bdpay.platform.canonical import sha256_canonical

__all__ = [
    "BEFTN_LAYOUT_OR2020",
    "BeftnEntryRow",
    "BeftnFileConfig",
    "BeftnParseError",
    "ParsedBeftnFile",
    "RECORD_LEN",
    "beftn_file_name",
    "file_content_hash",
    "normalize_individual_name",
    "parse_file",
    "render_file",
    "routing_check_digit",
    "validate_entry",
]

RECORD_LEN = 94
BLOCKING_FACTOR = 10
_PAD_RECORD = "9" * RECORD_LEN

#: Versioned layout document (``rail_field_dictionaries`` rail=BEFTN,
#: name=beftn_record_layout, version=or2020) — the binding default of B.1.
BEFTN_LAYOUT_OR2020: dict = {
    "rail": "BEFTN",
    "name": "beftn_record_layout",
    "version": "or2020",
    "record_len": RECORD_LEN,
    "blocking_factor": BLOCKING_FACTOR,
    "record_types": {"file_header": "1", "batch_header": "5", "entry": "6",
                      "addenda": "7", "batch_control": "8", "file_control": "9"},
    "transaction_codes": ["22", "27", "32", "37"],
    "amount_len": 10,
    "name_len": 22,
    "individual_id_len": 15,
    "trace_len": 15,
}

VALID_TRANSACTION_CODES = frozenset(BEFTN_LAYOUT_OR2020["transaction_codes"])

#: Entry validation codes (spec/11 B.1 renderer invariants table).
CODE_MISSING_ROUTING = "MISSING_ROUTING"
CODE_BAD_ROUTING_CHECK_DIGIT = "BAD_ROUTING_CHECK_DIGIT"
CODE_NAME_TOO_LONG = "NAME_TOO_LONG"
CODE_ZERO_AMOUNT = "ZERO_AMOUNT"
CODE_ACCOUNT_REF_UNRESOLVED = "ACCOUNT_REF_UNRESOLVED"
CODE_INVALID_TRANSACTION_CODE = "INVALID_TRANSACTION_CODE"
CODE_AMOUNT_TOO_LARGE = "AMOUNT_TOO_LARGE"
CODE_MISSING_ENTRY_REF = "MISSING_ENTRY_REF"
CODE_MISSING_NAME = "MISSING_NAME"


class BeftnParseError(ValueError):
    """Parse-back refusal (format or control-total violation, fail-closed)."""


def routing_check_digit(first8: str) -> int:
    """Standard 3-7-1 weighting over the first 8 digits (spec/11 B.1)."""
    if len(first8) != 8 or not first8.isdigit():
        raise ValueError("routing prefix must be exactly 8 digits")
    weights = (3, 7, 1, 3, 7, 1, 3, 7)
    total = sum(int(ch) * w for ch, w in zip(first8, weights, strict=True))
    return (10 - (total % 10)) % 10


def normalize_individual_name(name: str) -> str:
    """ASCII-normalize a beneficiary name for the 22-char field.

    Bengali digits map to ASCII; remaining non-ASCII codepoints are dropped
    (the spec/07 transliteration step is a compliance-owned seam — inject a
    richer normalizer there at integration); whitespace collapses; uppercase.
    """
    ascii_name = normalize_bengali_digits(name)
    ascii_name = "".join(ch for ch in ascii_name if ch.isascii() and ch.isprintable())
    return " ".join(ascii_name.split()).upper()


@dataclass(frozen=True)
class BeftnEntryRow:
    """One validated Entry Detail row (the seam between validation + render)."""

    entry_ref: str
    transaction_code: str
    receiving_routing: str  # 9 digits, check digit verified
    account_number: str     # detokenized at render; never persisted raw
    amount_minor: int
    individual_id: str
    individual_name: str    # already ASCII-normalized, <= 22 chars


def validate_entry(entry: dict, *, detokenizer: Detokenizer) -> tuple[BeftnEntryRow | None,
                                                                       list[str]]:
    """Validate one inbound entry dict; returns (row, codes). Codes => no row."""
    codes: list[str] = []
    entry_ref = entry.get("entry_ref")
    if not isinstance(entry_ref, str) or not entry_ref:
        codes.append(CODE_MISSING_ENTRY_REF)
        entry_ref = ""
    transaction_code = str(entry.get("transaction_code", ""))
    if transaction_code not in VALID_TRANSACTION_CODES:
        codes.append(CODE_INVALID_TRANSACTION_CODE)
    token = str(entry.get("account_token", ""))
    account_number = ""
    account_details: dict[str, object] = {}
    try:
        details_fn = getattr(detokenizer, "detokenize_account", None)
        if callable(details_fn):
            raw_details = details_fn(token)
            if isinstance(raw_details, dict):
                account_details = raw_details
                account_number = str(
                    raw_details.get("account_number")
                    or raw_details.get("account")
                    or ""
                )
            else:
                account_number = str(raw_details or "")
        else:
            account_number = detokenizer.detokenize(token)
    except Exception:
        codes.append(CODE_ACCOUNT_REF_UNRESOLVED)
    routing_source = (
        entry.get("receiving_routing")
        or account_details.get("receiving_routing")
        or account_details.get("routing_number")
        or account_details.get("routing")
        or ""
    )
    routing = normalize_bengali_digits(str(routing_source)).strip()
    if len(routing) != 9 or not routing.isdigit():
        codes.append(CODE_MISSING_ROUTING)
    elif routing_check_digit(routing[:8]) != int(routing[8]):
        codes.append(CODE_BAD_ROUTING_CHECK_DIGIT)
    amount = entry.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        codes.append(CODE_ZERO_AMOUNT)
        amount = 0
    elif amount > 10**BEFTN_LAYOUT_OR2020["amount_len"] - 1:
        codes.append(CODE_AMOUNT_TOO_LARGE)
    name = normalize_individual_name(str(entry.get("individual_name", "")))
    if not name:
        codes.append(CODE_MISSING_NAME)
    elif len(name) > BEFTN_LAYOUT_OR2020["name_len"]:
        codes.append(CODE_NAME_TOO_LONG)
    if not account_number.strip() or len(account_number) > 17:
        if CODE_ACCOUNT_REF_UNRESOLVED not in codes:
            codes.append(CODE_ACCOUNT_REF_UNRESOLVED)
    if codes:
        return None, codes
    return (
        BeftnEntryRow(
            entry_ref=entry_ref,
            transaction_code=transaction_code,
            receiving_routing=routing,
            account_number=account_number,
            amount_minor=amount,
            individual_id=str(entry.get("individual_id", entry_ref))[:15],
            individual_name=name,
        ),
        [],
    )


@dataclass(frozen=True)
class BeftnFileConfig:
    """Originator-side constants (registry config in deployment)."""

    immediate_destination: str  # sponsor-bank 9-digit routing
    immediate_origin: str       # BB-assigned originator id (9)
    destination_name: str
    origin_name: str = "BD-PAY"
    company_name: str = "BD-PAY"
    company_id: str = "BDPAY00001"
    originating_dfi: str = "11223344"  # ODFI routing first 8
    service_class: str = "220"          # credits-only
    standard_entry_class: str = "CCD"
    entry_description: str = "MERCHPAYOUT"


def _text(value: str, width: int) -> str:
    return value[:width].ljust(width)


def _num(value: int, width: int) -> str:
    rendered = f"{value:0{width}d}"
    if len(rendered) > width:
        raise BeftnParseError(f"numeric field overflow: {value} does not fit {width}")
    return rendered


def _is_debit(transaction_code: str) -> bool:
    return transaction_code in ("27", "37")


def _entry_hash(rows: list[BeftnEntryRow]) -> int:
    """Sum of all 8-digit receiving-DFI prefixes, rightmost 10 digits."""
    return sum(int(row.receiving_routing[:8]) for row in rows) % 10**10


def _trace_number(originating_dfi: str, sequence: int) -> str:
    return f"{originating_dfi.zfill(8)[:8]}{sequence:07d}"


def beftn_file_name(originator_id: str, business_date: date, session_code: str,
                    modifier: str) -> str:
    """``BDPAY_<originator>_<YYYYMMDD>_<session>_<modifier>.beftn`` (B.1)."""
    return (
        f"BDPAY_{originator_id}_{business_date.strftime('%Y%m%d')}_"
        f"{session_code}_{modifier}.beftn"
    )


def render_file(
    rows: list[BeftnEntryRow],
    *,
    config: BeftnFileConfig,
    effective_date: date,
    created_at: datetime,
    file_id_modifier: str = "A",
    batch_number: int = 1,
) -> bytes:
    """Render the complete blocked file; deterministic and re-render-stable."""
    if not rows:
        raise BeftnParseError("refusing to render an empty BEFTN file")
    if not (len(file_id_modifier) == 1 and "A" <= file_id_modifier <= "Z"):
        raise BeftnParseError("file_id_modifier must be A..Z")
    local = created_at.astimezone(DHAKA_TZ)
    header = (
        "1"
        + "01"
        + (" " + config.immediate_destination.zfill(9))[:10]
        + (" " + config.immediate_origin.zfill(9))[:10]
        + local.strftime("%y%m%d")
        + local.strftime("%H%M")
        + file_id_modifier
        + f"{RECORD_LEN:03d}"
        + f"{BLOCKING_FACTOR:02d}"
        + "1"
        + _text(config.destination_name, 23)
        + _text(config.origin_name, 23)
        + "00000000"  # reference_code: zero-filled at hash time (B.1)
    )
    batch_header = (
        "5"
        + config.service_class
        + _text(config.company_name, 16)
        + _text("", 20)
        + _text(config.company_id, 10)
        + config.standard_entry_class
        + _text(config.entry_description, 10)
        + _text("", 6)   # company descriptive date (optional)
        + effective_date.strftime("%y%m%d")
        + _text("", 3)   # settlement date (bank-filled)
        + "1"             # originator status code
        + config.originating_dfi.zfill(8)[:8]
        + _num(batch_number, 7)
    )
    entry_records: list[str] = []
    for index, row in enumerate(rows, start=1):
        entry_records.append(
            "6"
            + row.transaction_code
            + row.receiving_routing[:8]
            + row.receiving_routing[8]
            + _text(row.account_number, 17)
            + _num(row.amount_minor, 10)
            + _text(row.individual_id, 15)
            + _text(row.individual_name, 22)
            + _text("", 2)
            + "0"  # addenda indicator
            + _trace_number(config.originating_dfi, index)
        )
    total_debit = sum(r.amount_minor for r in rows if _is_debit(r.transaction_code))
    total_credit = sum(r.amount_minor for r in rows if not _is_debit(r.transaction_code))
    entry_hash = _entry_hash(rows)
    batch_control = (
        "8"
        + config.service_class
        + _num(len(rows), 6)
        + _num(entry_hash, 10)
        + _num(total_debit, 12)
        + _num(total_credit, 12)
        + _text(config.company_id, 10)
        + _text("", 19)
        + _text("", 6)
        + config.originating_dfi.zfill(8)[:8]
        + _num(batch_number, 7)
    )
    body = [header, batch_header, *entry_records, batch_control]
    record_count_pre_control = len(body) + 1  # + file control
    block_count = -(-record_count_pre_control // BLOCKING_FACTOR)
    file_control = (
        "9"
        + _num(1, 6)
        + _num(block_count, 6)
        + _num(len(rows), 8)
        + _num(entry_hash, 10)
        + _num(total_debit, 12)
        + _num(total_credit, 12)
        + _text("", 39)
    )
    body.append(file_control)
    while len(body) % BLOCKING_FACTOR != 0:
        body.append(_PAD_RECORD)
    for record in body:
        if len(record) != RECORD_LEN:
            raise BeftnParseError(
                f"internal layout defect: record length {len(record)} != {RECORD_LEN}"
            )
        if not record.isascii():
            raise BeftnParseError("records must be ASCII")
    rendered = ("\n".join(body) + "\n").encode("ascii")
    content_hash = file_content_hash(rendered)
    final = bytearray(rendered)
    final[86:94] = content_hash[:8].encode("ascii")
    return bytes(final)


def file_content_hash(rendered: bytes) -> str:
    """sha256 over the rendered bytes with the reference_code field zeroed."""
    zeroed = bytearray(rendered)
    zeroed[86:94] = b"00000000"
    return hashlib.sha256(bytes(zeroed)).hexdigest()


@dataclass(frozen=True)
class ParsedBeftnFile:
    """Parse-back result; every control total independently recomputed."""

    entry_count: int
    entry_hash: int
    total_debit_minor: int
    total_credit_minor: int
    block_count: int
    effective_date: str  # YYMMDD as written
    reference_code: str
    trace_numbers: tuple[str, ...]
    amounts_minor: tuple[int, ...]
    content_hash: str


def parse_file(rendered: bytes) -> ParsedBeftnFile:
    """Independent parse-back pass (write-then-verify; CERT-R3)."""
    try:
        text = rendered.decode("ascii")
    except UnicodeDecodeError as exc:
        raise BeftnParseError("file is not ASCII") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if not lines or any(len(line) != RECORD_LEN for line in lines):
        raise BeftnParseError(f"every record must be exactly {RECORD_LEN} chars")
    if len(lines) % BLOCKING_FACTOR != 0:
        raise BeftnParseError("file is not blocked to a multiple of 10 records")
    if lines[0][0] != "1":
        raise BeftnParseError("missing file header record")
    reference_code = lines[0][86:94]
    entries: list[str] = []
    batch_controls: list[str] = []
    file_controls: list[str] = []
    batch_headers: list[str] = []
    for line in lines[1:]:
        kind = line[0]
        if kind == "6":
            entries.append(line)
        elif kind == "8":
            batch_controls.append(line)
        elif kind == "9" and line != _PAD_RECORD:
            file_controls.append(line)
        elif kind == "5":
            batch_headers.append(line)
        elif kind == "7" or line == _PAD_RECORD:
            continue
        else:
            raise BeftnParseError(f"unknown record type {kind!r}")
    if len(batch_headers) != 1 or len(batch_controls) != 1 or len(file_controls) != 1:
        raise BeftnParseError("file must contain exactly one batch and one file control")
    amounts: list[int] = []
    routing_prefix_sum = 0
    debit = credit = 0
    traces: list[str] = []
    for line in entries:
        normalized = normalize_bengali_digits(line)
        transaction_code = normalized[1:3]
        routing = normalized[3:12]
        if not routing.isdigit() or routing_check_digit(routing[:8]) != int(routing[8]):
            raise BeftnParseError("entry routing check digit invalid")
        amount_text = normalized[29:39]
        if not amount_text.isdigit():
            raise BeftnParseError("entry amount is not numeric paisa")
        amount = int(amount_text)
        amounts.append(amount)
        routing_prefix_sum += int(routing[:8])
        if _is_debit(transaction_code):
            debit += amount
        else:
            credit += amount
        traces.append(normalized[79:94])
    control = normalize_bengali_digits(batch_controls[0])
    if int(control[4:10]) != len(entries):
        raise BeftnParseError("batch control entry count mismatch")
    if int(control[10:20]) != routing_prefix_sum % 10**10:
        raise BeftnParseError("batch control entry hash mismatch")
    if int(control[20:32]) != debit or int(control[32:44]) != credit:
        raise BeftnParseError("batch control debit/credit totals mismatch")
    fcontrol = normalize_bengali_digits(file_controls[0])
    if int(fcontrol[13:21]) != len(entries):
        raise BeftnParseError("file control entry count mismatch")
    if int(fcontrol[21:31]) != routing_prefix_sum % 10**10:
        raise BeftnParseError("file control entry hash mismatch")
    if int(fcontrol[31:43]) != debit or int(fcontrol[43:55]) != credit:
        raise BeftnParseError("file control totals mismatch")
    block_count = int(fcontrol[7:13])
    if block_count != len(lines) // BLOCKING_FACTOR:
        raise BeftnParseError("file control block count mismatch")
    content_hash = file_content_hash(rendered)
    if reference_code != "00000000" and reference_code != content_hash[:8]:
        raise BeftnParseError("reference_code does not match file content hash")
    return ParsedBeftnFile(
        entry_count=len(entries),
        entry_hash=routing_prefix_sum % 10**10,
        total_debit_minor=debit,
        total_credit_minor=credit,
        block_count=block_count,
        effective_date=batch_headers[0][69:75],
        reference_code=reference_code,
        trace_numbers=tuple(traces),
        amounts_minor=tuple(amounts),
        content_hash=content_hash,
    )


def layout_hash() -> str:
    """Content hash of the shipped layout document (pinned per file row)."""
    return sha256_canonical(BEFTN_LAYOUT_OR2020)
