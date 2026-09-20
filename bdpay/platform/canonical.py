"""Canonical JSON — THE single wire-format implementation (errata E12, binding).

Form pinned by SPEC_ERRATA.md E12 / arch §556 lineage:

- dict keys sorted at serialize time (code-point order of the canonical key string)
- ``ensure_ascii=False`` — Bengali text stays raw UTF-8 in the output bytes
- compact separators ``,`` and ``:`` (no whitespace)
- datetimes MUST be timezone-aware; serialized as UTC ISO-8601 with
  MILLISECOND precision and ``Z`` suffix, e.g. ``2026-06-11T14:05:00.000Z``
- str-subclass Enums serialize as their lowercased string value
- ``decimal.Decimal`` serializes as a string (money on the wire is integer
  paisa per spec 00 §6; Decimal-as-string exists for non-amount precise values)
- floats REJECTED (``CanonicalError``) — by construction, per spec 00 §6
- sets REJECTED (unordered, non-deterministic)
- naive datetimes REJECTED (spec 00 §7)
- returns ``bytes`` (UTF-8)

No content-addressed data may be written by any module that does not import
this one implementation (errata E12). ``sha256_canonical`` is the hash every
``make_id`` / ledger preimage uses.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

__all__ = ["CanonicalError", "canonical_json", "sha256_canonical"]


class CanonicalError(ValueError):
    """Raised when an object cannot be serialized to canonical JSON."""


def canonical_json(obj: object) -> bytes:
    """Serialize ``obj`` to canonical JSON bytes (UTF-8) per errata E12."""
    parts: list[str] = []
    _write(obj, parts, set())
    return "".join(parts).encode("utf-8")


def sha256_canonical(obj: object) -> str:
    """Hex SHA-256 of ``canonical_json(obj)`` — the content-address hash."""
    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _format_datetime(dt: datetime) -> str:
    """UTC ISO-8601, millisecond precision, ``Z`` suffix. Rejects naive."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise CanonicalError(
            "naive datetimes are rejected; canonical timestamps must be timezone-aware"
        )
    dt = dt.astimezone(UTC)
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
        f"T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        f".{dt.microsecond // 1000:03d}Z"
    )


def _string(value: str) -> str:
    """JSON-escape a string atom with ensure_ascii=False (raw UTF-8)."""
    return json.dumps(value, ensure_ascii=False)


def _key_string(key: object) -> str:
    """Canonical string form of a dict key (str-subclass Enum lowercases)."""
    if isinstance(key, Enum):
        if isinstance(key, str):
            return str(key.value).lower()
        raise CanonicalError(
            f"dict key Enum {type(key).__name__} is not a str subclass; "
            "only str-subclass Enums are canonically serializable"
        )
    if isinstance(key, str):
        return str(key)
    raise CanonicalError(f"dict keys must be str, got {type(key).__name__}")


def _write(obj: object, parts: list[str], seen: set[int]) -> None:
    if obj is None:
        parts.append("null")
        return
    if isinstance(obj, bool):  # before int: bool is an int subclass
        parts.append("true" if obj else "false")
        return
    if isinstance(obj, float):
        raise CanonicalError(
            "floats are rejected in canonical JSON; money is integer paisa (spec 00 §6)"
        )
    if isinstance(obj, Enum):  # before str: str-subclass Enums lowercase
        if isinstance(obj, str):
            parts.append(_string(str(obj.value).lower()))
            return
        raise CanonicalError(
            f"Enum {type(obj).__name__} is not a str subclass; "
            "only str-subclass Enums are canonically serializable"
        )
    if isinstance(obj, str):
        parts.append(_string(str(obj)))
        return
    if isinstance(obj, int):
        parts.append(str(int(obj)))
        return
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise CanonicalError("non-finite Decimal is rejected in canonical JSON")
        parts.append(_string(str(obj)))
        return
    if isinstance(obj, datetime):
        parts.append('"' + _format_datetime(obj) + '"')
        return
    if isinstance(obj, set | frozenset):
        raise CanonicalError("sets are rejected in canonical JSON (unordered)")
    if isinstance(obj, dict):
        marker = id(obj)
        if marker in seen:
            raise CanonicalError("circular reference detected")
        seen.add(marker)
        items = sorted(
            ((_key_string(key), value) for key, value in obj.items()),
            key=lambda kv: kv[0],
        )
        for (key_a, _), (key_b, _) in zip(items, items[1:], strict=False):
            if key_a == key_b:
                raise CanonicalError(f"duplicate canonical key {key_a!r} after key normalization")
        parts.append("{")
        for index, (key, value) in enumerate(items):
            if index:
                parts.append(",")
            parts.append(_string(key))
            parts.append(":")
            _write(value, parts, seen)
        parts.append("}")
        seen.discard(marker)
        return
    if isinstance(obj, list | tuple):
        marker = id(obj)
        if marker in seen:
            raise CanonicalError("circular reference detected")
        seen.add(marker)
        parts.append("[")
        for index, item in enumerate(obj):
            if index:
                parts.append(",")
            _write(item, parts, seen)
        parts.append("]")
        seen.discard(marker)
        return
    raise CanonicalError(f"type {type(obj).__name__} is not canonically serializable")
