"""ISO 8583 v1987 codec, field-dictionary-driven (spec/11 §A.1, binding).

jPOS is REJECTED (AGPL v3, spec/11 reuse table); the codec is pure-function
Python driven entirely by a dictionary document — a BB dialect bump is a
dictionary release, not a code change. Message grammar: ``MTI(4 ASCII)`` +
``primary bitmap(16 hex chars)`` + ``[secondary bitmap(16 hex) if bit 1
set]`` + data elements in ascending DE order. Bitmap algorithms are the
normative ones from spec/11 verbatim. A DE present in the bitmap but absent
from the dictionary hard-fails decode (fail-closed, ``iso_unknown_de``).

Amount rule (DE4): wire format is n12 zero-padded INTEGER PAISA (conventions
§6 — no conversion, no floats). The MAC preimage is every populated DE except
the MAC fields (64/128), concatenated in DE order, prefixed with the MTI.
"""

from __future__ import annotations

from bdpay.platform.canonical import sha256_canonical

__all__ = [
    "Iso8583Error",
    "MAC_DES",
    "NPSB_V28_DICTIONARY",
    "decode_bitmaps",
    "dictionary_hash",
    "encode_bitmaps",
    "mac_preimage",
    "pack_de48",
    "pack_message",
    "unpack_de48",
    "unpack_message",
]

#: MAC-bearing data elements, excluded from the MAC preimage.
MAC_DES = frozenset({64, 128})

#: The shipped BB v2.8 dictionary (spec/11 A.1 document, verbatim content).
NPSB_V28_DICTIONARY: dict = {
    "dialect": "NPSB-BB-v2.8",
    "iso_version": "1987",
    "mti_allowed": ["0100", "0110", "0200", "0210", "0420", "0421", "0430", "0800", "0810"],
    "elements": {
        "2": {"name": "pan_or_token", "format": "n", "len_type": "LLVAR", "max": 19,
              "encoding": "ascii", "sensitive": True},
        "3": {"name": "processing_code", "format": "n", "len_type": "FIXED", "max": 6,
              "encoding": "ascii"},
        "4": {"name": "amount_txn", "format": "n", "len_type": "FIXED", "max": 12,
              "encoding": "ascii", "unit": "paisa_integer"},
        "7": {"name": "transmission_dt", "format": "n", "len_type": "FIXED", "max": 10,
              "encoding": "ascii"},
        "11": {"name": "stan", "format": "n", "len_type": "FIXED", "max": 6, "encoding": "ascii"},
        "12": {"name": "local_time", "format": "n", "len_type": "FIXED", "max": 6,
               "encoding": "ascii"},
        "13": {"name": "local_date", "format": "n", "len_type": "FIXED", "max": 4,
               "encoding": "ascii"},
        "32": {"name": "acquiring_inst_id", "format": "n", "len_type": "LLVAR", "max": 11,
               "encoding": "ascii"},
        "37": {"name": "rrn", "format": "an", "len_type": "FIXED", "max": 12, "encoding": "ascii"},
        "38": {"name": "auth_code", "format": "an", "len_type": "FIXED", "max": 6,
               "encoding": "ascii"},
        "39": {"name": "response_code", "format": "an", "len_type": "FIXED", "max": 2,
               "encoding": "ascii"},
        "41": {"name": "terminal_id", "format": "ans", "len_type": "FIXED", "max": 8,
               "encoding": "ascii"},
        "48": {"name": "bb_private_data", "format": "ans", "len_type": "LLLVAR", "max": 999,
               "encoding": "ascii", "bb_proprietary": True,
               "subelements": {"01": {"name": "channel"}, "02": {"name": "wallet_provider"},
                                "10": {"name": "qr_payload_hash"},
                                "11": {"name": "qr_merchant_template"}}},
        "49": {"name": "currency", "format": "n", "len_type": "FIXED", "max": 3,
               "encoding": "ascii", "value": "050"},
        "63": {"name": "bb_network_data", "format": "ans", "len_type": "LLLVAR", "max": 999,
               "encoding": "ascii", "bb_proprietary": True},
        "64": {"name": "mac_primary", "format": "b", "len_type": "FIXED", "max": 16,
               "encoding": "ascii_hex"},
        "70": {"name": "network_mgmt_code", "format": "n", "len_type": "FIXED", "max": 3,
               "encoding": "ascii"},
        "90": {"name": "original_data_elements", "format": "n", "len_type": "FIXED", "max": 42,
               "encoding": "ascii"},
        "102": {"name": "sender_account_ref", "format": "ans", "len_type": "LLVAR", "max": 28,
                "encoding": "ascii", "tokenized": True},
        "103": {"name": "beneficiary_account_ref", "format": "ans", "len_type": "LLVAR",
                "max": 28, "encoding": "ascii", "tokenized": True},
        "128": {"name": "mac_secondary", "format": "b", "len_type": "FIXED", "max": 16,
                "encoding": "ascii_hex"},
    },
}


class Iso8583Error(ValueError):
    """Codec refusal. ``code`` is the canonical connector error taxonomy code."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def dictionary_hash(dictionary: dict) -> str:
    """Content hash of a dictionary document (pinned on every message row)."""
    return sha256_canonical(dictionary)


def encode_bitmaps(present: set[int]) -> bytes:
    """Normative bitmap encode (spec/11 A.1). ``present`` is a subset of 2..128."""
    bits = bytearray(16)  # 128 bits
    if any(de > 64 for de in present):
        present = present | {1}
    for de in sorted(present):
        byte_i, bit_i = (de - 1) // 8, (de - 1) % 8
        bits[byte_i] |= 0x80 >> bit_i
    primary = bits[:8].hex().upper().encode("ascii")
    if 1 in present:
        return primary + bits[8:].hex().upper().encode("ascii")
    return primary


def decode_bitmaps(buf: bytes) -> tuple[set[int], int]:
    """Normative bitmap decode (spec/11 A.1). Returns (present DEs, consumed)."""
    primary = bytes.fromhex(buf[:16].decode("ascii"))
    present: set[int] = set()
    consumed = 16
    for i in range(64):
        if primary[i // 8] & (0x80 >> (i % 8)):
            present.add(i + 1)
    if 1 in present:
        secondary = bytes.fromhex(buf[16:32].decode("ascii"))
        consumed = 32
        for i in range(64):
            if secondary[i // 8] & (0x80 >> (i % 8)):
                present.add(i + 65)
        present.discard(1)
    return present, consumed


def _entry_for(de: int, dictionary: dict) -> dict:
    entry = dictionary["elements"].get(str(de))
    if entry is None:
        raise Iso8583Error(
            f"DE {de} present in bitmap but absent from dictionary "
            f"{dictionary.get('dialect')!r} (fail-closed)",
            code="iso_unknown_de",
        )
    return entry


def _validate_value(de: int, value: str, entry: dict) -> None:
    if not isinstance(value, str):
        raise Iso8583Error(f"DE {de} value must be str", code="format_error")
    if not value.isascii():
        raise Iso8583Error(f"DE {de} value must be ASCII", code="format_error")
    len_type = entry["len_type"]
    max_len = int(entry["max"])
    if len_type == "FIXED":
        if len(value) != max_len:
            raise Iso8583Error(
                f"DE {de} FIXED length {max_len} != {len(value)}", code="format_error"
            )
    elif len_type in ("LLVAR", "LLLVAR"):
        if len(value) > max_len:
            raise Iso8583Error(f"DE {de} exceeds max {max_len}", code="format_error")
    else:
        raise Iso8583Error(f"DE {de} unknown len_type {len_type!r}", code="format_error")
    fmt = entry["format"]
    if fmt == "n" and not (value.isdigit() or value == ""):
        raise Iso8583Error(f"DE {de} format n requires digits only", code="format_error")


def pack_message(mti: str, fields: dict[int, str], dictionary: dict) -> bytes:
    """Pack ``mti`` + ``fields`` (DE -> ASCII value) into wire bytes."""
    if mti not in dictionary["mti_allowed"]:
        raise Iso8583Error(f"MTI {mti!r} not allowed by dictionary", code="iso_mti_not_allowed")
    present = set(fields)
    if any(de < 2 or de > 128 for de in present):
        raise Iso8583Error("data elements must be within 2..128", code="format_error")
    parts: list[bytes] = [mti.encode("ascii"), encode_bitmaps(present)]
    for de in sorted(present):
        entry = _entry_for(de, dictionary)
        value = fields[de]
        _validate_value(de, value, entry)
        len_type = entry["len_type"]
        if len_type == "LLVAR":
            parts.append(f"{len(value):02d}".encode("ascii"))
        elif len_type == "LLLVAR":
            parts.append(f"{len(value):03d}".encode("ascii"))
        parts.append(value.encode("ascii"))
    return b"".join(parts)


def unpack_message(buf: bytes, dictionary: dict) -> tuple[str, dict[int, str]]:
    """Unpack wire bytes into ``(mti, fields)``; fail-closed on any anomaly."""
    try:
        mti = buf[:4].decode("ascii")
    except UnicodeDecodeError as exc:
        raise Iso8583Error("MTI is not ASCII", code="format_error") from exc
    if mti not in dictionary["mti_allowed"]:
        raise Iso8583Error(f"MTI {mti!r} not allowed by dictionary", code="iso_mti_not_allowed")
    body = buf[4:]
    try:
        present, consumed = decode_bitmaps(body)
    except ValueError as exc:
        raise Iso8583Error("bitmap is not valid hex", code="format_error") from exc
    cursor = consumed
    fields: dict[int, str] = {}
    for de in sorted(present):
        entry = _entry_for(de, dictionary)
        len_type = entry["len_type"]
        if len_type == "FIXED":
            length = int(entry["max"])
        elif len_type == "LLVAR":
            length = _read_length(body, cursor, 2, de)
            cursor += 2
        else:  # LLLVAR (validated by _entry_for/_validate_value vocabulary)
            length = _read_length(body, cursor, 3, de)
            cursor += 3
        raw = body[cursor : cursor + length]
        if len(raw) != length:
            raise Iso8583Error(f"DE {de} truncated on the wire", code="format_error")
        cursor += length
        try:
            value = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise Iso8583Error(f"DE {de} is not ASCII", code="format_error") from exc
        _validate_value(de, value, entry)
        fields[de] = value
    if cursor != len(body):
        raise Iso8583Error("trailing bytes after last data element", code="format_error")
    return mti, fields


def _read_length(body: bytes, cursor: int, digits: int, de: int) -> int:
    raw = body[cursor : cursor + digits]
    if len(raw) != digits or not raw.isdigit():
        raise Iso8583Error(f"DE {de} variable-length prefix invalid", code="format_error")
    return int(raw)


def pack_de48(subfields: dict[str, str], dictionary: dict) -> str:
    """Pack DE48 sub-fields (``sub-id -> value``) into the LLLVAR body.

    Sub-field grammar (BB private data, spec/11 A.1 / spec/13 §D): 2-digit
    sub-id + 3-digit length + ASCII value, concatenated in ascending sub-id
    order. A sub-id absent from the dictionary's DE48 ``subelements`` table
    hard-fails (fail-closed, ``iso_unknown_de``) — a dialect bump that adds a
    sub-field is a dictionary release, not a code change.
    """
    entry = _entry_for(48, dictionary)
    known = entry.get("subelements", {})
    parts: list[str] = []
    for sub_id in sorted(subfields):
        if not isinstance(sub_id, str) or len(sub_id) != 2 or not sub_id.isdigit():
            raise Iso8583Error(
                f"DE48 sub-id {sub_id!r} must be 2 ASCII digits", code="format_error"
            )
        if sub_id not in known:
            raise Iso8583Error(
                f"DE48 sub-id {sub_id} absent from dictionary "
                f"{dictionary.get('dialect')!r} (fail-closed)",
                code="iso_unknown_de",
            )
        value = subfields[sub_id]
        if not isinstance(value, str) or not value.isascii():
            raise Iso8583Error(f"DE48.{sub_id} value must be ASCII str", code="format_error")
        if len(value) > 999:
            raise Iso8583Error(
                f"DE48.{sub_id} exceeds the 3-digit sub-field length", code="format_error"
            )
        parts.append(f"{sub_id}{len(value):03d}{value}")
    packed = "".join(parts)
    if len(packed) > int(entry["max"]):
        raise Iso8583Error(
            f"DE48 packed sub-fields exceed max {entry['max']}", code="format_error"
        )
    return packed


def unpack_de48(value: str, dictionary: dict) -> dict[str, str]:
    """Decode a DE48 body back to ``sub-id -> value``; fail-closed on anomaly."""
    entry = _entry_for(48, dictionary)
    known = entry.get("subelements", {})
    cursor = 0
    out: dict[str, str] = {}
    while cursor < len(value):
        head = value[cursor : cursor + 5]
        if len(head) != 5 or not head.isascii() or not head.isdigit():
            raise Iso8583Error("DE48 sub-field header invalid", code="format_error")
        sub_id, length = head[:2], int(head[2:5])
        if sub_id not in known:
            raise Iso8583Error(
                f"DE48 sub-id {sub_id} absent from dictionary "
                f"{dictionary.get('dialect')!r} (fail-closed)",
                code="iso_unknown_de",
            )
        if sub_id in out:
            raise Iso8583Error(f"DE48.{sub_id} repeated on the wire", code="format_error")
        cursor += 5
        body = value[cursor : cursor + length]
        if len(body) != length:
            raise Iso8583Error(f"DE48.{sub_id} truncated on the wire", code="format_error")
        out[sub_id] = body
        cursor += length
    return out


def mac_preimage(mti: str, fields: dict[int, str]) -> bytes:
    """MAC preimage: MTI + all populated DEs except 64/128, in DE order."""
    parts = [mti]
    for de in sorted(fields):
        if de in MAC_DES:
            continue
        parts.append(fields[de])
    return "".join(parts).encode("ascii")
