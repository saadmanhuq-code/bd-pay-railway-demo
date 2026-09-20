"""Deterministic object store for canonical netting result files (spec/19).

The netting result file is content-addressed (pointer embeds the result
hash) and write-once: re-putting the SAME bytes at a pointer is the
idempotent no-op the content-addressed scheme guarantees; putting DIFFERENT
bytes at an existing pointer is refused (append-only evidence discipline).
``get`` returns the exact stored bytes — byte-identical on re-download
(spec/15 deterministic file+hash contract).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from bdpay.ledger.netting.errors import NettingError

__all__ = ["FilesystemObjectStore", "InMemoryObjectStore", "ObjectStore"]


class ObjectStore(Protocol):
    """Write-once binary object store keyed by deterministic pointers."""

    def put(self, pointer: str, content: bytes) -> None: ...

    def get(self, pointer: str) -> bytes: ...

    def exists(self, pointer: str) -> bool: ...


class InMemoryObjectStore:
    """Deterministic in-memory object store (unit suite)."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, pointer: str, content: bytes) -> None:
        existing = self._objects.get(pointer)
        if existing is not None:
            if existing == content:
                return  # idempotent re-put of identical bytes
            raise NettingError(f"object {pointer} already exists with different bytes")
        self._objects[pointer] = bytes(content)

    def get(self, pointer: str) -> bytes:
        try:
            return self._objects[pointer]
        except KeyError:
            raise NettingError(f"object {pointer} not found") from None

    def exists(self, pointer: str) -> bool:
        return pointer in self._objects


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]")


class FilesystemObjectStore:
    """Filesystem-backed object store (single-node deployments, dev rigs).

    Pointers map to files under ``root`` with every path component sanitized;
    the write-once rule matches the in-memory semantics.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path_for(self, pointer: str) -> Path:
        scheme, _, rest = pointer.partition("://")
        components = [scheme, *[c for c in rest.split("/") if c]]
        safe = [_SAFE_COMPONENT.sub("_", c) for c in components if c not in ("..", "")]
        if not safe:
            raise NettingError(f"unusable object pointer {pointer!r}")
        return self._root.joinpath(*safe)

    def put(self, pointer: str, content: bytes) -> None:
        path = self._path_for(pointer)
        if path.exists():
            if path.read_bytes() == content:
                return
            raise NettingError(f"object {pointer} already exists with different bytes")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, pointer: str) -> bytes:
        path = self._path_for(pointer)
        if not path.exists():
            raise NettingError(f"object {pointer} not found")
        return path.read_bytes()

    def exists(self, pointer: str) -> bool:
        return self._path_for(pointer).exists()
