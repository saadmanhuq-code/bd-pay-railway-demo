"""Durable checkpoint-trust custody (F01).

``LedgerService`` fails closed on checkpoint trust: it only accepts a
checkpoint whose public key is in an externally supplied trusted-key set
(SPEC_ERRATA E6). Until this module existed the composition root
(:func:`bdpay.app.build_services`) minted a fresh ``LocalEd25519Signer()`` per
process and supplied NO trusted-key set, so ``LedgerService`` defaulted to
trusting that one ephemeral key. A second service instance over the same store
— a restart, a replica, an export/replay worker — therefore read a historical
checkpoint signed by a key it had never heard of, ``verify_chain`` returned
``FAILED_INVALID_CHECKPOINT_SIG`` and the fail-closed write gate engaged
against valid money.

The trust that survives a process must live outside the process, and it must
not be read out of the checkpoint row it authorises (whoever re-signs a chain
also stores their own public key next to it). This module is that external
custody: a small append-oriented JSON registry holding

* the ACTIVE checkpoint signing key (32-byte Ed25519 seed), so every instance
  over the same store signs with the same key rather than minting its own, and
* every public key ever enrolled, ACTIVE or RETIRED, so historical checkpoints
  stay verifiable across an authorised rotation.

A key that is not in the registry is not trusted — an unapproved signer still
fails verification, which is the property the write gate exists to protect.

Deployments that hold the private key in an external custody provider
construct their own ``CheckpointSigner`` and call
:meth:`FileCheckpointTrustRegistry.adopt` with it instead: the seed never
enters the registry, only the public key the ledger must trust.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from bdpay.ledger.checkpoint_signer import (
    CheckpointSigner,
    LocalEd25519Signer,
    generate_signing_seed,
)

__all__ = [
    "DEFAULT_CHECKPOINT_TRUST_PATH",
    "CheckpointTrustRegistry",
    "CheckpointTrustError",
    "FileCheckpointTrustRegistry",
    "build_checkpoint_trust",
]

#: Default custody location, relative to the deployment working directory.
#: Overridden by ``LEDGER_CHECKPOINT_TRUST_PATH`` (``Settings``).
DEFAULT_CHECKPOINT_TRUST_PATH = "var/ledger/checkpoint_trust.json"

_SCHEMA_VERSION = 1
_STATUS_ACTIVE = "ACTIVE"
_STATUS_RETIRED = "RETIRED"


class CheckpointTrustError(RuntimeError):
    """The durable trust record is missing, unreadable or malformed."""


@runtime_checkable
class CheckpointTrustRegistry(Protocol):
    """Durable custody for the checkpoint signing key and the trusted set."""

    def active_signer(self) -> CheckpointSigner: ...

    def trusted_public_keys(self) -> frozenset[str]: ...


def _key_id(public_key_b64: str) -> str:
    # Stable, non-secret handle for operators; the public key itself is the id.
    return f"ckpt_{public_key_b64[:16]}"


class FileCheckpointTrustRegistry:
    """File-backed checkpoint trust custody (default composition).

    The record is rewritten atomically (temp file + ``os.replace``) so a
    concurrent instance never observes a half-written registry, and the first
    writer wins on creation: a second instance racing to bootstrap re-reads the
    winner's key instead of minting a competing one.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path or DEFAULT_CHECKPOINT_TRUST_PATH)

    # -- reads ----------------------------------------------------------
    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict[str, Any] | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:  # unreadable custody is NOT an empty custody
            raise CheckpointTrustError(
                f"checkpoint trust registry at {self._path} is unreadable: {exc}"
            ) from exc
        try:
            record = json.loads(raw)
        except ValueError as exc:
            raise CheckpointTrustError(
                f"checkpoint trust registry at {self._path} is not valid JSON: {exc}"
            ) from exc
        if not isinstance(record, dict) or not isinstance(record.get("keys"), list):
            raise CheckpointTrustError(
                f"checkpoint trust registry at {self._path} is malformed"
            )
        return record

    def enrolled_keys(self) -> tuple[dict[str, Any], ...]:
        """Every enrolled key record (ACTIVE first), newest enrolment last."""
        record = self._read()
        if record is None:
            return ()
        return tuple(record["keys"])

    def trusted_public_keys(self) -> frozenset[str]:
        """Every public key ever enrolled — retired keys stay trusted.

        Historical checkpoints were signed under an authorisation that was
        valid when they were written; retiring a key stops NEW signing, it
        does not retroactively make old money unverifiable.
        """
        return frozenset(
            str(entry["public_key_b64"])
            for entry in self.enrolled_keys()
            if entry.get("public_key_b64")
        )

    # -- writes ---------------------------------------------------------
    def _serialize(self, keys: Iterable[dict[str, Any]]) -> str:
        payload = {"schema_version": _SCHEMA_VERSION, "keys": list(keys)}
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def _write(self, keys: Iterable[dict[str, Any]]) -> None:
        body = self._serialize(keys)
        self._ensure_writable_parent()
        handle, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".checkpoint_trust-", suffix=".tmp"
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(body)
            try:
                os.chmod(tmp_name, 0o600)  # secret seed: owner-only
            except OSError:  # pragma: no cover - platform without POSIX modes
                pass
            os.replace(tmp_name, self._path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    def _create_exclusive(self, keys: Iterable[dict[str, Any]]) -> bool:
        """Bootstrap write that loses cleanly to a racing instance."""
        body = self._serialize(keys)
        self._ensure_writable_parent()
        try:
            fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            raise self._custody_error(exc) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        return True

    def _custody_error(self, exc: OSError) -> CheckpointTrustError:
        # Fail closed and LOUD: a bare PermissionError/OSError here (the
        # production shape review evidence — read_only rootfs, no
        # ledger-custody volume mounted at this path) crash-loops the app
        # tier with a stack trace that names neither the custody path nor the
        # override knob. Name both explicitly so an operator sees exactly
        # what to mount / point LEDGER_CHECKPOINT_TRUST_PATH at instead.
        return CheckpointTrustError(
            f"checkpoint trust custody at {self._path} is not writable "
            f"({exc}). Mount a writable, durable directory at this path's "
            "parent (a named volume in production) or set "
            "LEDGER_CHECKPOINT_TRUST_PATH to one that is."
        )

    def _ensure_writable_parent(self) -> None:
        parent = self._path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise self._custody_error(exc) from exc
        if not os.access(parent, os.W_OK):
            raise self._custody_error(
                PermissionError(f"{parent} is not writable by this process")
            )

    def active_signer(self) -> CheckpointSigner:
        """Return the durable ACTIVE signer, bootstrapping custody on first use."""
        for _ in range(2):
            record = self._read()
            if record is not None:
                entry = _active_entry(record["keys"])
                if entry is not None:
                    seed_b64 = entry.get("private_key_seed_b64")
                    if not seed_b64:
                        raise CheckpointTrustError(
                            "the ACTIVE checkpoint key is held in an external custody "
                            "provider; construct its signer and call adopt()"
                        )
                    return LocalEd25519Signer(base64.b64decode(seed_b64, validate=True))
            # Bootstrap: create-exclusive so a racing instance cannot end up
            # with a different ACTIVE key over the same store.
            seed = generate_signing_seed()
            signer = LocalEd25519Signer(seed)
            self._create_exclusive([_entry(signer, seed=seed, authorized_by="bootstrap")])
        raise CheckpointTrustError(
            f"checkpoint trust registry at {self._path} has no ACTIVE key"
        )

    def adopt(self, signer: CheckpointSigner, *, authorized_by: str) -> CheckpointSigner:
        """Enrol an externally held signer (custody provider) as ACTIVE."""
        if signer.public_key_b64() not in self.trusted_public_keys():
            self._enroll(signer, seed=None, authorized_by=authorized_by)
        return signer

    def rotate(self, *, authorized_by: str) -> CheckpointSigner:
        """Authorised rotation: mint a new ACTIVE key, retain the old as trusted."""
        if not authorized_by:
            raise CheckpointTrustError("key rotation requires an authorising actor")
        seed = generate_signing_seed()
        signer = LocalEd25519Signer(seed)
        self._enroll(signer, seed=seed, authorized_by=authorized_by)
        return signer

    def _enroll(
        self, signer: CheckpointSigner, *, seed: bytes | None, authorized_by: str
    ) -> None:
        record = self._read()
        keys: list[dict[str, Any]] = list(record["keys"]) if record else []
        for entry in keys:
            if entry.get("status") == _STATUS_ACTIVE:
                entry["status"] = _STATUS_RETIRED
        keys.append(_entry(signer, seed=seed, authorized_by=authorized_by))
        self._write(keys)


def _entry(
    signer: CheckpointSigner, *, seed: bytes | None, authorized_by: str
) -> dict[str, Any]:
    public_key_b64 = signer.public_key_b64()
    return {
        "key_id": _key_id(public_key_b64),
        "public_key_b64": public_key_b64,
        "private_key_seed_b64": (
            base64.b64encode(seed).decode("ascii") if seed is not None else None
        ),
        "status": _STATUS_ACTIVE,
        "authorized_by": authorized_by,
    }


def _active_entry(keys: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    for entry in keys:
        if entry.get("status") == _STATUS_ACTIVE:
            return entry
    return None


def build_checkpoint_trust(
    path: str | os.PathLike[str],
) -> tuple[CheckpointSigner, frozenset[str], FileCheckpointTrustRegistry]:
    """Composition-root helper: (active signer, trusted set, registry).

    Every service instance over the same store MUST be built through this so
    the trusted set comes from durable custody rather than from whichever key
    this process happened to mint.
    """
    registry = FileCheckpointTrustRegistry(path)
    signer = registry.active_signer()
    return signer, registry.trusted_public_keys(), registry
