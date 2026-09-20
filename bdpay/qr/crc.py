"""CRC-16/CCITT-FALSE — spec/13 §B (normative, with golden vectors).

Polynomial 0x1021, init 0xFFFF, no reflection, no final XOR. Computed over all
payload bytes up to and including the literal ``"6304"`` (ID+length of the CRC
object), appended as 4 uppercase hex characters.

Binding rule: the CRC is computed over UTF-8 *bytes* — Bengali characters in
ID 64 are multi-byte, so this is byte-level, never codepoint-level.
"""

from __future__ import annotations

__all__ = ["CRC_PREFIX", "append_crc", "crc16_ccitt_false", "verify_crc"]

#: ID 63 + length 04 — the literal included in the CRC preimage.
CRC_PREFIX = "6304"

_POLY = 0x1021
_INIT = 0xFFFF


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE of ``data`` (spec/13 §B reference algorithm)."""
    crc = _INIT
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ _POLY if crc & 0x8000 else crc << 1) & 0xFFFF
    return crc


def append_crc(payload_without_crc: str) -> str:
    """Append ``6304`` + 4 uppercase hex CRC chars to a CRC-less payload."""
    base = payload_without_crc + CRC_PREFIX
    return base + f"{crc16_ccitt_false(base.encode('utf-8')):04X}"


def verify_crc(payload: str) -> bool:
    """True iff ``payload`` ends in ``6304`` + the correct uppercase CRC.

    Lowercase hex is rejected — the profile mandates uppercase (spec/13 §A
    ID 63 rule), and accepting both would create two wire forms of one payload
    (breaking content-addressing of ``payload_hash``).
    """
    if len(payload) < 8 or payload[-8:-4] != CRC_PREFIX:
        return False
    expected = f"{crc16_ccitt_false(payload[:-4].encode('utf-8')):04X}"
    return payload[-4:] == expected
