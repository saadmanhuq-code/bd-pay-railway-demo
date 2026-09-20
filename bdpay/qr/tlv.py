"""EMVCo QRCPS TLV primitives — spec/13 §A (format) and §C steps 1-2.

Each data object is ``ID(2 ASCII digits)`` + ``LEN(2 ASCII digits, 00-99)`` +
``VALUE``. LEN counts UTF-8 **bytes** of the value (spec/13 §C step 2:
"VALUE(LEN bytes — byte-counted)"); Bengali values in ID 64 are multi-byte,
so the walk operates on the UTF-8 encoding of the payload string.

This module is pure structure: no profile semantics (those live in codec.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from bdpay.qr.errors import QrEncodeError, QrValidationError

__all__ = ["TlvObject", "assemble", "encode_object", "walk", "walk_template"]

_MAX_VALUE_BYTES = 99


@dataclass(frozen=True)
class TlvObject:
    """One decoded data object: numeric ID, decoded string value, raw bytes."""

    object_id: int
    value: str
    raw: bytes  # ID+LEN+VALUE bytes as they appeared on the wire


def encode_object(object_id: int, value: str) -> str:
    """Encode one data object; LEN is the UTF-8 byte length of ``value``."""
    if not 0 <= object_id <= 99:
        raise QrEncodeError(f"data object ID must be 00-99, got {object_id}")
    if not isinstance(value, str):
        raise QrEncodeError(f"value must be str, got {type(value).__name__}")
    length = len(value.encode("utf-8"))
    if length == 0:
        raise QrEncodeError(f"data object {object_id:02d} has an empty value")
    if length > _MAX_VALUE_BYTES:
        raise QrEncodeError(
            f"data object {object_id:02d} value is {length} bytes; max is {_MAX_VALUE_BYTES}"
        )
    return f"{object_id:02d}{length:02d}{value}"


def assemble(objects: list[tuple[int, str]]) -> str:
    """Concatenate raw objects in the given order (no ordering or CRC logic).

    Used by the encoder (which supplies ascending-ID order) and by the golden
    corpus builder (which deliberately constructs invalid payloads).
    """
    return "".join(encode_object(object_id, value) for object_id, value in objects)


def _is_ascii_digits(chunk: bytes) -> bool:
    return len(chunk) == 2 and all(0x30 <= b <= 0x39 for b in chunk)


def walk(data: bytes, *, reject_duplicates: bool, context: str) -> list[TlvObject]:
    """Strict TLV walk over ``data`` (spec/13 §C steps 1-2).

    IDs and lengths must be ASCII digits (values may be UTF-8). A truncated
    value, a non-digit ID/length, or invalid UTF-8 in a value raises
    ``qr_malformed_tlv``. Duplicate IDs raise ``qr_duplicate_id`` when
    ``reject_duplicates`` (fail-closed at the root level).
    """
    objects: list[TlvObject] = []
    seen: set[int] = set()
    position = 0
    total = len(data)
    while position < total:
        id_chunk = data[position : position + 2]
        len_chunk = data[position + 2 : position + 4]
        if not _is_ascii_digits(id_chunk) or not _is_ascii_digits(len_chunk):
            raise QrValidationError(
                "qr_malformed_tlv",
                f"{context}: non-ASCII-digit ID/length at byte {position}",
            )
        object_id = int(id_chunk.decode("ascii"))
        length = int(len_chunk.decode("ascii"))
        value_bytes = data[position + 4 : position + 4 + length]
        if len(value_bytes) != length or length == 0:
            raise QrValidationError(
                "qr_malformed_tlv",
                f"{context}: truncated or empty value for object {object_id:02d}",
            )
        try:
            value = value_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise QrValidationError(
                "qr_malformed_tlv",
                f"{context}: object {object_id:02d} value is not valid UTF-8",
            ) from exc
        if reject_duplicates:
            if object_id in seen:
                raise QrValidationError(
                    "qr_duplicate_id", f"{context}: duplicate object {object_id:02d}"
                )
            seen.add(object_id)
        objects.append(
            TlvObject(
                object_id=object_id,
                value=value,
                raw=data[position : position + 4 + length],
            )
        )
        position += 4 + length
    return objects


def walk_template(value: str, *, context: str) -> dict[int, str]:
    """Parse a template value into its sub-TLV map (duplicates fail-closed)."""
    objects = walk(value.encode("utf-8"), reject_duplicates=True, context=context)
    return {obj.object_id: obj.value for obj in objects}
