"""Key-management program (spec/14 FSM 3 + FSM 4 + procedures P1-P4).

Owns: the LMK/KEK/DEK/PEPPER/EGRESS_HMAC/PII_KEY metadata records, ceremony
FSM with split-knowledge guards, rotation schedule records (crypto periods),
compromise handling, re-encryption runs, pepper migration (F9), and the
egress-grant HMAC verification used by detokenize-egress guard 2.

Refusal-first: any transition not in the spec tables raises a ConflictError
and the denial is audited (``transition_denied``).
"""

from __future__ import annotations

import base64
import hmac as hmac_mod
from datetime import datetime, timedelta

from bdpay.platform.canonical import canonical_json, sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    ConflictError,
    InternalError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.vault.audit import VaultAuditChain
from bdpay.vault.crypto import (
    GCM_NONCE_BYTES,
    KEY_BYTES,
    KeySourceUnavailableError,
    SoftHsm,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    hmac_sha256_hex,
    key_check_value,
    zeroize,
)
from bdpay.vault.ids import make_vault_id
from bdpay.vault.records import (
    CRYPTO_PERIOD_DAYS,
    CardTokenRecord,
    CeremonyShareRecord,
    CeremonyStatus,
    CustodianRecord,
    EncryptedPanRecord,
    KeyCeremonyRecord,
    KeyKind,
    KeyStatus,
    ReencryptionRunRecord,
    ReencryptionStatus,
    TokenAliasRecord,
    VaultKeyRecord,
)
from bdpay.vault.stores import (
    CeremonyStore,
    CustodianStore,
    KeyStore,
    OutboxStore,
    PanStore,
    ReencryptionRunStore,
    TokenAliasStore,
    TokenStore,
)

__all__ = ["CryptoUnavailableError", "KeyManager", "PAN_AAD_SCHEMA_VERSION"]

PAN_AAD_SCHEMA_VERSION = 1
CEREMONY_TTL = timedelta(days=7)
CEREMONY_EXECUTE_WINDOW = timedelta(hours=72)
DEK_CACHE_TTL_S = 900  # 15 min (PCI 12.3.2 TRA item)
DESTRUCTION_BACKUP_HOLD = timedelta(days=35)
REENCRYPTION_DEADLINE = timedelta(days=30)
GRANT_MAX_WINDOW_S = 120

_VALID_REASONS = frozenset({"initial", "scheduled_rotation", "compromise", "custodian_change"})


class CryptoUnavailableError(InternalError):
    """HSM breaker open / key source sealed / DEK unavailable — fail closed."""

    default_code = "vault_crypto_unavailable"


def pan_aad(token: str) -> bytes:
    """AAD binding a PAN ciphertext to its token (spec/14 intake step 5)."""
    return canonical_json({"token": token, "schema_version": PAN_AAD_SCHEMA_VERSION})


class KeyManager:
    def __init__(
        self,
        *,
        hsm: SoftHsm,
        keys: KeyStore,
        custodians: CustodianStore,
        ceremonies: CeremonyStore,
        pans: PanStore,
        tokens: TokenStore,
        aliases: TokenAliasStore,
        runs: ReencryptionRunStore,
        audit: VaultAuditChain,
        outbox: OutboxStore,
        clock: Clock,
        emit_event,
    ) -> None:
        self._hsm = hsm
        self._keys = keys
        self._custodians = custodians
        self._ceremonies = ceremonies
        self._pans = pans
        self._tokens = tokens
        self._aliases = aliases
        self._runs = runs
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._emit = emit_event
        self._material_cache: dict[str, tuple[bytearray, datetime]] = {}
        self._reencryption_only: set[str] = set()  # COMPROMISED keys flag-gated for forced runs

    # ------------------------------------------------------------------ helpers

    def _deny_transition(self, entity: str, detail: dict, *, actor: str) -> None:
        self._audit.append("transition_denied", {"entity": entity, **detail}, actor=actor)

    def _rotation_due(self, kind: KeyKind, activated_at: datetime) -> datetime | None:
        period = CRYPTO_PERIOD_DAYS[kind]
        if period is None:
            return None
        return activated_at + timedelta(days=period)

    def hsm_random_hex(self, n_bytes: int) -> str:
        try:
            return self._hsm.random(n_bytes).hex()
        except KeySourceUnavailableError as exc:
            raise CryptoUnavailableError("key source unavailable") from exc

    # ------------------------------------------------------------- custodians

    def appoint_custodian(
        self,
        *,
        person_name: str,
        operator_identity: str,
        reporting_line: str,
        approval_request_id: str,
        actor: str,
    ) -> CustodianRecord:
        if self._custodians.by_operator(operator_identity) is not None:
            raise ConflictError(
                "operator identity already maps to a custodian", code="custodian_exists"
            )
        record = CustodianRecord(
            vcus_id=make_vault_id(
                "vcus", {"operator_identity": operator_identity, "person_name": person_name}
            ),
            person_name=person_name,
            operator_identity=operator_identity,
            reporting_line=reporting_line,
            status="ACTIVE",
            appointed_at=self._clock.now(),
            appointment_approval_id=approval_request_id,
        )
        self._custodians.save(record)
        self._audit.append(
            "custodian_appointed",
            {"vcus_id": record.vcus_id, "approval_request_id": approval_request_id},
            actor=actor,
        )
        return record

    # ------------------------------------------------------------- ceremonies

    def open_ceremony(
        self,
        *,
        kind: KeyKind,
        target_key_name: str,
        reason: str,
        custodian_ids: list[str],
        quorum: int,
        approval_request_id: str,
        actor: str,
    ) -> KeyCeremonyRecord:
        if reason not in _VALID_REASONS:
            raise InvalidRequestError("unknown ceremony reason", code="ceremony_reason_invalid")
        if not (2 <= quorum <= len(custodian_ids) <= 5):
            raise InvalidRequestError(
                "quorum must satisfy 2 <= quorum <= custodians <= 5",
                code="ceremony_quorum_invalid",
            )
        if len(set(custodian_ids)) != len(custodian_ids):
            raise InvalidRequestError(
                "custodians must be distinct", code="ceremony_custodians_duplicate"
            )
        lines: set[str] = set()
        for vcus_id in custodian_ids:
            custodian = self._custodians.get(vcus_id)
            if custodian is None or custodian.status != "ACTIVE":
                raise InvalidRequestError(
                    "custodian missing or not ACTIVE", code="ceremony_custodian_invalid"
                )
            if custodian.reporting_line in lines:
                raise InvalidRequestError(
                    "no two custodians may share a reporting line (split knowledge)",
                    code="ceremony_reporting_line_shared",
                )
            lines.add(custodian.reporting_line)
        now = self._clock.now()
        record = KeyCeremonyRecord(
            vcer_id=make_vault_id(
                "vcer",
                {
                    "kind": kind.value,
                    "target_key_name": target_key_name,
                    "reason": reason,
                    "opened_at": now,
                    "approval_request_id": approval_request_id,
                },
            ),
            kind=kind,
            target_key_name=target_key_name,
            reason=reason,
            quorum=quorum,
            custodian_ids=tuple(custodian_ids),
            status=CeremonyStatus.DRAFT,
            approval_request_id=approval_request_id,
            opened_at=now,
            ttl_expires_at=now + CEREMONY_TTL,
        )
        self._ceremonies.save(record)
        self._audit.append(
            "ceremony_created",
            {"vcer_id": record.vcer_id, "kind": kind.value, "reason": reason},
            actor=actor,
        )
        return record

    def attest_share(
        self,
        *,
        ceremony_id: str,
        custodian_id: str,
        share_kcv: str,
        attestation: str,
        operator_identity: str,
    ) -> KeyCeremonyRecord:
        ceremony = self._ceremonies.get(ceremony_id)
        if ceremony is None:
            raise NotFoundError("ceremony not found", code="ceremony_not_found")
        self._expire_ceremony_if_due(ceremony)
        if ceremony.status not in (CeremonyStatus.DRAFT, CeremonyStatus.SHARES_PENDING):
            self._deny_transition(
                "KeyCeremony",
                {"vcer_id": ceremony_id, "from": ceremony.status.value, "trigger": "share"},
                actor=operator_identity,
            )
            raise ConflictError(
                "ceremony does not accept shares in its current state",
                code="ceremony_not_accepting_shares",
            )
        if custodian_id not in ceremony.custodian_ids:
            raise ConflictError(
                "custodian is not named on this ceremony", code="ceremony_custodian_not_named"
            )
        custodian = self._custodians.get(custodian_id)
        if custodian is None or custodian.operator_identity != operator_identity:
            raise ConflictError(
                "caller operator identity does not map to the custodian",
                code="ceremony_share_identity_mismatch",
            )
        if any(share.custodian_id == custodian_id for share in ceremony.shares):
            raise ConflictError(
                "one share per custodian (dual control)", code="ceremony_share_duplicate"
            )
        share = CeremonyShareRecord(
            ceremony_id=ceremony_id,
            custodian_id=custodian_id,
            share_kcv=share_kcv,
            attestation_hash=sha256_canonical({"attestation": attestation}),
            attested_at=self._clock.now(),
        )
        ceremony.shares.append(share)
        if ceremony.status is CeremonyStatus.DRAFT:
            ceremony.status = CeremonyStatus.SHARES_PENDING
        self._audit.append(
            "ceremony_share_attested",
            {"vcer_id": ceremony_id, "custodian_id": custodian_id},
            actor=operator_identity,
        )
        if len(ceremony.shares) >= ceremony.quorum:
            ceremony.status = CeremonyStatus.QUORUM_REACHED
            ceremony.quorum_at = self._clock.now()
            self._audit.append(
                "ceremony_quorum", {"vcer_id": ceremony_id}, actor=operator_identity
            )
        self._ceremonies.save(ceremony)
        return ceremony

    def abort_ceremony(self, ceremony_id: str, *, actor: str) -> KeyCeremonyRecord:
        ceremony = self._ceremonies.get(ceremony_id)
        if ceremony is None:
            raise NotFoundError("ceremony not found", code="ceremony_not_found")
        if ceremony.status in (CeremonyStatus.EXECUTED, CeremonyStatus.ABORTED):
            self._deny_transition(
                "KeyCeremony",
                {"vcer_id": ceremony_id, "from": ceremony.status.value, "trigger": "abort"},
                actor=actor,
            )
            raise ConflictError("ceremony already terminal", code="ceremony_terminal")
        ceremony.status = CeremonyStatus.ABORTED
        ceremony.aborted_at = self._clock.now()
        self._ceremonies.save(ceremony)
        self._audit.append("ceremony_aborted", {"vcer_id": ceremony_id}, actor=actor)
        return ceremony

    def _expire_ceremony_if_due(self, ceremony: KeyCeremonyRecord) -> None:
        if ceremony.status in (CeremonyStatus.EXECUTED, CeremonyStatus.ABORTED):
            return
        if self._clock.now() >= ceremony.ttl_expires_at:
            ceremony.status = CeremonyStatus.ABORTED
            ceremony.aborted_at = self._clock.now()
            self._ceremonies.save(ceremony)
            self._audit.append(
                "ceremony_aborted",
                {"vcer_id": ceremony.vcer_id, "reason": "ttl"},
                actor="vault-sweep",
            )

    def _require_executable_ceremony(
        self, ceremony_id: str, kind: KeyKind, *, actor: str
    ) -> KeyCeremonyRecord:
        ceremony = self._ceremonies.get(ceremony_id)
        if ceremony is None:
            raise NotFoundError("ceremony not found", code="ceremony_not_found")
        self._expire_ceremony_if_due(ceremony)
        if ceremony.status is not CeremonyStatus.QUORUM_REACHED:
            self._deny_transition(
                "KeyCeremony",
                {"vcer_id": ceremony_id, "from": ceremony.status.value, "trigger": "execute"},
                actor=actor,
            )
            raise ConflictError(
                "ceremony has not reached quorum", code="ceremony_quorum_not_reached"
            )
        if ceremony.kind is not kind:
            raise ConflictError("ceremony kind mismatch", code="ceremony_kind_mismatch")
        if ceremony.quorum_at is None or self._clock.now() > (
            ceremony.quorum_at + CEREMONY_EXECUTE_WINDOW
        ):
            raise ConflictError(
                "rotation must execute within 72h of quorum", code="ceremony_execute_window_past"
            )
        return ceremony

    def _execute_ceremony(self, ceremony: KeyCeremonyRecord, *, actor: str) -> None:
        ceremony.status = CeremonyStatus.EXECUTED
        ceremony.executed_at = self._clock.now()
        self._ceremonies.save(ceremony)
        self._audit.append("ceremony_executed", {"vcer_id": ceremony.vcer_id}, actor=actor)

    # ------------------------------------------------------------- activation

    def _new_key_record(
        self,
        *,
        kind: KeyKind,
        key_name: str | None,
        generation: int,
        wrapped: str | None,
        kcv: str,
        ceremony_id: str | None,
        version: int | None,
    ) -> VaultKeyRecord:
        now = self._clock.now()
        return VaultKeyRecord(
            vkey_id=make_vault_id(
                "vkey",
                {
                    "kind": kind.value,
                    "generation": generation,
                    "created_ceremony": ceremony_id,
                    "kcv": kcv,
                },
            ),
            kind=kind,
            generation=generation,
            status=KeyStatus.PENDING_CEREMONY,
            kcv=kcv,
            wrapped_key_material=wrapped,
            openbao_key_name=key_name,
            openbao_key_version=version,
            ceremony_id=ceremony_id,
            created_at=now,
        )

    def _activate(self, record: VaultKeyRecord, *, actor: str, old: VaultKeyRecord | None) -> None:
        """PENDING_CEREMONY -> ACTIVE after a wrap-chain round-trip check;
        the displaced same-kind/name ACTIVE key -> DEPRECATED (FSM 3)."""
        self._verify_wrap_chain(record)
        now = self._clock.now()
        record.status = KeyStatus.ACTIVE
        record.activated_at = now
        record.rotation_due_at = self._rotation_due(record.kind, now)
        self._keys.save(record)
        self._audit.append(
            "key_activated",
            {"vkey_id": record.vkey_id, "kind": record.kind.value, "kcv": record.kcv},
            actor=actor,
        )
        if old is not None and old.status is KeyStatus.ACTIVE:
            # COMPROMISED predecessors stay COMPROMISED (flag-gated) until destroy.
            old.status = KeyStatus.DEPRECATED
            old.deprecated_at = now
            self._keys.save(old)
            self._audit.append("key_deprecated", {"vkey_id": old.vkey_id}, actor=actor)
        self._emit(
            "vault_key.rotated",
            "VaultKey",
            record.vkey_id,
            {
                "new_key_id": record.vkey_id,
                "old_key_id": old.vkey_id if old is not None else None,
                "kind": record.kind.value,
                "target_key_name": record.openbao_key_name,
                "generation": record.generation,
                "ceremony_id": record.ceremony_id,
                "reencryption_run_id": None,
            },
        )

    def _unwrap_material(self, record: VaultKeyRecord) -> bytes:
        """Direct unwrap of a record's wrapped material (no store lookup, no
        cache) — used by the activation round-trip check."""
        wrapped = record.wrapped_key_material
        if wrapped is None:
            raise CryptoUnavailableError("key has no wrapped material")
        try:
            if wrapped.startswith("vault:v"):
                kek_record = self._keys.active(KeyKind.KEK, "vault/pan-kek")
                if kek_record is None:
                    raise CryptoUnavailableError("no ACTIVE KEK")
                kek = bytearray(self._hsm.unwrap_under_lmk(kek_record.wrapped_key_material or ""))
                try:
                    return self._hsm.kek_unwrap(bytes(kek), wrapped)
                finally:
                    zeroize(kek)
            return self._hsm.unwrap_under_lmk(wrapped)
        except KeySourceUnavailableError as exc:
            raise CryptoUnavailableError("key source unavailable") from exc

    def _verify_wrap_chain(self, record: VaultKeyRecord) -> None:
        """Test encrypt/decrypt round-trip before activation (FSM 3 guard)."""
        if record.kind in (KeyKind.DEK, KeyKind.PEPPER):
            material = bytearray(self._unwrap_material(record))
            try:
                nonce = self._hsm.random(GCM_NONCE_BYTES)
                probe = aes_gcm_encrypt(bytes(material), b"kcv-roundtrip", b"probe", nonce)
                if aes_gcm_decrypt(bytes(material), probe, b"probe", nonce) != b"kcv-roundtrip":
                    raise CryptoUnavailableError("wrap-chain round-trip failed")
                if key_check_value(bytes(material)) != record.kcv:
                    raise CryptoUnavailableError("KCV mismatch on activation")
            finally:
                zeroize(material)

    # ------------------------------------------------------------- provisioning

    def provision_initial_keys(self, *, actor: str, custodian_ids: list[str]) -> dict[str, str]:
        """Soft-HSM initial provisioning (P1 analog) — drives the real ceremony
        FSM for every layer, then activates LMK/KEK/DEK/PEPPER/EGRESS_HMAC."""
        out: dict[str, str] = {}
        kek_material: bytearray | None = None
        try:
            # LMK — metadata only; no material outside the (soft) HSM.
            lmk_ceremony = self._ceremonial_quorum(
                KeyKind.LMK, "softhsm/lmk", "initial", custodian_ids, actor
            )
            lmk = self._new_key_record(
                kind=KeyKind.LMK,
                key_name=None,
                generation=1,
                wrapped=None,
                kcv=key_check_value(self._hsm.random(KEY_BYTES)),
                ceremony_id=lmk_ceremony.vcer_id,
                version=None,
            )
            self._activate(lmk, actor=actor, old=None)
            self._execute_ceremony(lmk_ceremony, actor=actor)
            out["LMK"] = lmk.vkey_id

            # KEK — material wrapped under the (soft) LMK domain.
            kek_ceremony = self._ceremonial_quorum(
                KeyKind.KEK, "vault/pan-kek", "initial", custodian_ids, actor
            )
            generated = self._hsm.generate_key_under_lmk()
            kek = self._new_key_record(
                kind=KeyKind.KEK,
                key_name="vault/pan-kek",
                generation=1,
                wrapped=generated["wrapped"],
                kcv=generated["kcv"],
                ceremony_id=kek_ceremony.vcer_id,
                version=1,
            )
            self._activate(kek, actor=actor, old=None)
            self._execute_ceremony(kek_ceremony, actor=actor)
            out["KEK"] = kek.vkey_id
            kek_material = bytearray(self._hsm.unwrap_under_lmk(generated["wrapped"]))

            # DEK + PEPPER — wrapped under KEK (transit-shaped blobs).
            wrapped_layers = ((KeyKind.DEK, "vault/pan-dek"), (KeyKind.PEPPER, "vault/pan-pepper"))
            for kind, name in wrapped_layers:
                ceremony = self._ceremonial_quorum(kind, name, "initial", custodian_ids, actor)
                material = bytearray(self._hsm.random(KEY_BYTES))
                try:
                    record = self._new_key_record(
                        kind=kind,
                        key_name=None,
                        generation=1,
                        wrapped=self._hsm.kek_wrap(bytes(kek_material), bytes(material), 1),
                        kcv=key_check_value(bytes(material)),
                        ceremony_id=ceremony.vcer_id,
                        version=None,
                    )
                finally:
                    zeroize(material)
                self._activate(record, actor=actor, old=None)
                self._execute_ceremony(ceremony, actor=actor)
                out[kind.value] = record.vkey_id

            # EGRESS_HMAC — kernel/egress-grant-hmac verify key (dual-generation).
            ceremony = self._ceremonial_quorum(
                KeyKind.EGRESS_HMAC, "kernel/egress-grant-hmac", "initial", custodian_ids, actor
            )
            material = bytearray(self._hsm.random(KEY_BYTES))
            try:
                record = self._new_key_record(
                    kind=KeyKind.EGRESS_HMAC,
                    key_name="kernel/egress-grant-hmac",
                    generation=1,
                    wrapped=self._hsm.wrap_under_lmk(bytes(material)),
                    kcv=key_check_value(bytes(material)),
                    ceremony_id=ceremony.vcer_id,
                    version=1,
                )
            finally:
                zeroize(material)
            self._activate(record, actor=actor, old=None)
            self._execute_ceremony(ceremony, actor=actor)
            out["EGRESS_HMAC"] = record.vkey_id
        finally:
            if kek_material is not None:
                zeroize(kek_material)
        return out

    def _ceremonial_quorum(
        self,
        kind: KeyKind,
        target_key_name: str,
        reason: str,
        custodian_ids: list[str],
        actor: str,
    ) -> KeyCeremonyRecord:
        ceremony = self.open_ceremony(
            kind=kind,
            target_key_name=target_key_name,
            reason=reason,
            custodian_ids=custodian_ids,
            quorum=2,
            approval_request_id=make_vault_id(
                "appr", {"purpose": f"{reason}:{kind.value}:{target_key_name}"}
            ),
            actor=actor,
        )
        for vcus_id in custodian_ids[:2]:
            custodian = self._custodians.get(vcus_id)
            assert custodian is not None  # validated by open_ceremony
            self.attest_share(
                ceremony_id=ceremony.vcer_id,
                custodian_id=vcus_id,
                share_kcv=self._hsm.random(3).hex(),
                attestation=f"component entered at console by {custodian.person_name}",
                operator_identity=custodian.operator_identity,
            )
        refreshed = self._ceremonies.get(ceremony.vcer_id)
        assert refreshed is not None
        return refreshed

    # ------------------------------------------------------------------ rotate

    def rotate_key(
        self, key_id: str, *, ceremony_id: str, approval_request_id: str, actor: str
    ) -> dict:
        """POST /vault/v1/keys/{key_id}/rotate — KEK/DEK/PEPPER/PII/EGRESS only
        (LMK rotation is a physical ceremony; the API records, never executes)."""
        old = self._keys.get(key_id)
        if old is None:
            raise NotFoundError("key not found", code="key_not_found")
        if old.kind is KeyKind.LMK:
            raise ConflictError(
                "LMK rotation is a physical HSM ceremony", code="lmk_rotation_is_physical"
            )
        if old.status not in (KeyStatus.ACTIVE, KeyStatus.COMPROMISED):
            self._deny_transition(
                "VaultKey",
                {"vkey_id": key_id, "from": old.status.value, "trigger": "rotate"},
                actor=actor,
            )
            raise ConflictError("key is not rotatable from its state", code="key_not_rotatable")
        ceremony = self._require_executable_ceremony(ceremony_id, old.kind, actor=actor)

        generation = old.generation + 1
        run_id: str | None = None
        if old.kind is KeyKind.KEK:
            new = self._rotate_kek(old, ceremony, actor)
        elif old.kind in (KeyKind.DEK, KeyKind.PEPPER):
            new = self._rotate_wrapped_kind(old, ceremony, actor)
            if old.kind is KeyKind.DEK:
                run_id = self.create_reencryption_run(
                    scope="encrypted_pans",
                    old_key_id=old.vkey_id,
                    new_key_id=new.vkey_id,
                    ceremony_id=ceremony.vcer_id,
                    compromise=(ceremony.reason == "compromise"),
                    actor=actor,
                ).vrun_id
        elif old.kind is KeyKind.PII_KEY:
            new = self._new_key_record(
                kind=KeyKind.PII_KEY,
                key_name=old.openbao_key_name,
                generation=generation,
                wrapped=None,
                kcv=key_check_value(self._hsm.random(KEY_BYTES)),
                ceremony_id=ceremony.vcer_id,
                version=(old.openbao_key_version or 1) + 1,
            )
            self._activate(new, actor=actor, old=old)
            run_id = self.create_reencryption_run(
                scope=ceremony.target_key_name,
                old_key_id=old.vkey_id,
                new_key_id=new.vkey_id,
                ceremony_id=ceremony.vcer_id,
                compromise=(ceremony.reason == "compromise"),
                actor=actor,
            ).vrun_id
        else:  # EGRESS_HMAC
            material = bytearray(self._hsm.random(KEY_BYTES))
            try:
                new = self._new_key_record(
                    kind=KeyKind.EGRESS_HMAC,
                    key_name=old.openbao_key_name,
                    generation=generation,
                    wrapped=self._hsm.wrap_under_lmk(bytes(material)),
                    kcv=key_check_value(bytes(material)),
                    ceremony_id=ceremony.vcer_id,
                    version=(old.openbao_key_version or 1) + 1,
                )
            finally:
                zeroize(material)
            self._activate(new, actor=actor, old=old)
        self._execute_ceremony(ceremony, actor=actor)
        self._material_cache.clear()
        return {"new_key_id": new.vkey_id, "reencryption_run_id": run_id}

    def _rotate_kek(
        self, old: VaultKeyRecord, ceremony: KeyCeremonyRecord, actor: str
    ) -> VaultKeyRecord:
        """P2: new KEK generation, then rewrap DEK/PEPPER wrapped blobs
        (rewrap touches only wrapped blobs, never PAN ciphertexts)."""
        generated = self._hsm.generate_key_under_lmk()
        new = self._new_key_record(
            kind=KeyKind.KEK,
            key_name=old.openbao_key_name,
            generation=old.generation + 1,
            wrapped=generated["wrapped"],
            kcv=generated["kcv"],
            ceremony_id=ceremony.vcer_id,
            version=(old.openbao_key_version or 1) + 1,
        )
        old_kek = bytearray(self._hsm.unwrap_under_lmk(old.wrapped_key_material or ""))
        new_kek = bytearray(self._hsm.unwrap_under_lmk(generated["wrapped"]))
        try:
            self._activate(new, actor=actor, old=old)
            for record in self._keys.all():
                if record.kind in (KeyKind.DEK, KeyKind.PEPPER) and record.status in (
                    KeyStatus.ACTIVE,
                    KeyStatus.DEPRECATED,
                ):
                    material = bytearray(
                        self._hsm.kek_unwrap(bytes(old_kek), record.wrapped_key_material or "")
                    )
                    try:
                        record.wrapped_key_material = self._hsm.kek_wrap(
                            bytes(new_kek), bytes(material), new.generation
                        )
                    finally:
                        zeroize(material)
                    self._keys.save(record)
            self._audit.append(
                "kek_rewrap_completed", {"new_kek": new.vkey_id}, actor=actor
            )
        finally:
            zeroize(old_kek)
            zeroize(new_kek)
        return new

    def _rotate_wrapped_kind(
        self, old: VaultKeyRecord, ceremony: KeyCeremonyRecord, actor: str
    ) -> VaultKeyRecord:
        kek_record = self._keys.active(KeyKind.KEK, "vault/pan-kek")
        if kek_record is None:
            raise CryptoUnavailableError("no ACTIVE KEK")
        kek = bytearray(self._hsm.unwrap_under_lmk(kek_record.wrapped_key_material or ""))
        material = bytearray(self._hsm.random(KEY_BYTES))
        try:
            new = self._new_key_record(
                kind=old.kind,
                key_name=old.openbao_key_name,
                generation=old.generation + 1,
                wrapped=self._hsm.kek_wrap(bytes(kek), bytes(material), kek_record.generation),
                kcv=key_check_value(bytes(material)),
                ceremony_id=ceremony.vcer_id,
                version=None,
            )
        finally:
            zeroize(material)
            zeroize(kek)
        self._activate(new, actor=actor, old=old)
        return new

    # -------------------------------------------------------------- compromise

    def declare_compromise(
        self, key_id: str, *, approval_request_id: str, evidence_ref: str, declared_by: str
    ) -> dict:
        record = self._keys.get(key_id)
        if record is None:
            raise NotFoundError("key not found", code="key_not_found")
        if record.status not in (KeyStatus.ACTIVE, KeyStatus.DEPRECATED):
            self._deny_transition(
                "VaultKey",
                {"vkey_id": key_id, "from": record.status.value, "trigger": "compromise"},
                actor=declared_by,
            )
            raise ConflictError(
                "compromise is declarable only from ACTIVE or DEPRECATED",
                code="key_not_compromisable",
            )
        record.status = KeyStatus.COMPROMISED
        self._keys.save(record)
        self._reencryption_only.add(record.vkey_id)
        self._material_cache.clear()
        self._audit.append(
            "key_compromised",
            {"vkey_id": key_id, "evidence_ref": evidence_ref, "approval": approval_request_id},
            actor=declared_by,
        )
        # Auto-open the forced-rotation ceremony with a disjoint ACTIVE custodian pair.
        custodian_ids = self._pick_disjoint_custodians()
        forced = self.open_ceremony(
            kind=record.kind,
            target_key_name=record.openbao_key_name or f"vault/{record.kind.value.lower()}",
            reason="compromise",
            custodian_ids=custodian_ids,
            quorum=2,
            approval_request_id=approval_request_id,
            actor=declared_by,
        )
        self._emit(
            "vault_key.compromised",
            "VaultKey",
            key_id,
            {
                "key_id": key_id,
                "kind": record.kind.value,
                "target_key_name": record.openbao_key_name,
                "approval_request_id": approval_request_id,
                "forced_ceremony_id": forced.vcer_id,
            },
        )
        return {
            "key_id": key_id,
            "status": record.status.value,
            "forced_ceremony_id": forced.vcer_id,
        }

    def _pick_disjoint_custodians(self) -> list[str]:
        chosen: list[str] = []
        lines: set[str] = set()
        for custodian in self._custodians.active():
            if custodian.reporting_line not in lines:
                chosen.append(custodian.vcus_id)
                lines.add(custodian.reporting_line)
            if len(chosen) == 2:
                return chosen
        raise ConflictError(
            "need two ACTIVE custodians with disjoint reporting lines",
            code="ceremony_custodian_invalid",
        )

    # ----------------------------------------------------------------- destroy

    def destroy_key(self, key_id: str, *, actor: str) -> VaultKeyRecord:
        record = self._keys.get(key_id)
        if record is None:
            raise NotFoundError("key not found", code="key_not_found")
        if record.status not in (KeyStatus.DEPRECATED, KeyStatus.COMPROMISED):
            self._deny_transition(
                "VaultKey",
                {"vkey_id": key_id, "from": record.status.value, "trigger": "destroy"},
                actor=actor,
            )
            raise ConflictError(
                "destroy is reachable only from DEPRECATED or COMPROMISED",
                code="key_not_destroyable",
            )
        if record.kind is KeyKind.DEK and self._pans.by_dek(key_id):
            raise ConflictError(
                "ciphertexts still reference this key", code="key_still_referenced"
            )
        if record.status is KeyStatus.DEPRECATED:
            if record.deprecated_at is None or self._clock.now() < (
                record.deprecated_at + DESTRUCTION_BACKUP_HOLD
            ):
                raise ConflictError(
                    "backup-cycle hold (35d) has not elapsed", code="key_backup_hold_active"
                )
        receipt = self._hsm.delete_key(record.wrapped_key_material or record.vkey_id)
        record.status = KeyStatus.DESTROYED
        record.destroyed_at = self._clock.now()
        record.destroy_receipt_hash = sha256_canonical(receipt)
        record.wrapped_key_material = None
        self._keys.save(record)
        self._reencryption_only.discard(key_id)
        self._material_cache.pop(key_id, None)
        self._audit.append(
            "key_destroyed",
            {"vkey_id": key_id, "destroy_receipt_hash": record.destroy_receipt_hash},
            actor=actor,
        )
        self._emit(
            "vault_key.destroyed",
            "VaultKey",
            key_id,
            {
                "key_id": key_id,
                "kind": record.kind.value,
                "destroy_receipt_hash": record.destroy_receipt_hash,
            },
        )
        return record

    # ------------------------------------------------------------ re-encryption

    def create_reencryption_run(
        self,
        *,
        scope: str,
        old_key_id: str,
        new_key_id: str,
        ceremony_id: str | None,
        compromise: bool,
        actor: str,
    ) -> ReencryptionRunRecord:
        now = self._clock.now()
        run = ReencryptionRunRecord(
            vrun_id=make_vault_id(
                "vrun", {"scope": scope, "old": old_key_id, "new": new_key_id, "opened_at": now}
            ),
            scope=scope,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            ceremony_id=ceremony_id,
            status=ReencryptionStatus.PENDING,
            deadline_at=now + REENCRYPTION_DEADLINE,
            rate_cap_rps=500 if compromise else 50,
        )
        self._runs.save(run)
        self._audit.append(
            "reencryption_run_created", {"vrun_id": run.vrun_id, "scope": scope}, actor=actor
        )
        return run

    def advance_reencryption_run(
        self, vrun_id: str, *, batch_size: int = 500
    ) -> ReencryptionRunRecord:
        """P3 batch step: decrypt-under-old, encrypt-under-new with fresh nonce,
        cursor-checkpointed and idempotent on re-run."""
        run = self._runs.get(vrun_id)
        if run is None:
            raise NotFoundError("reencryption run not found", code="reencryption_run_not_found")
        if run.status in (
            ReencryptionStatus.COMPLETED,
            ReencryptionStatus.FAILED,
            ReencryptionStatus.CANCELLED,
        ):
            return run
        if run.scope != "encrypted_pans":
            # PII scopes are executed by their owning service (P4 step 3);
            # the vault tracks progress via record_external_progress only.
            return run
        now = self._clock.now()
        if run.status is ReencryptionStatus.PENDING:
            run.status = ReencryptionStatus.RUNNING
            run.started_at = now
            run.total_rows = len(self._pans.by_dek(run.old_key_id))
        old_material = bytearray(self.key_material(run.old_key_id, allow_reencryption=True))
        new_material = bytearray(self.key_material(run.new_key_id))
        processed = 0
        try:
            for row in self._pans.by_dek(run.old_key_id):
                if processed >= batch_size:
                    break
                if run.cursor_ref is not None and row.epan_id <= run.cursor_ref:
                    continue
                aad = pan_aad(row.token)
                pan = bytearray(
                    aes_gcm_decrypt(bytes(old_material), row.pan_ciphertext, aad, row.nonce)
                )
                try:
                    fresh_nonce = self._hsm.random(GCM_NONCE_BYTES)
                    row.pan_ciphertext = aes_gcm_encrypt(
                        bytes(new_material), bytes(pan), aad, fresh_nonce
                    )
                finally:
                    zeroize(pan)
                row.nonce = fresh_nonce
                row.dek_key_id = run.new_key_id
                row.reencrypted_at = now
                self._pans.save(row)
                run.cursor_ref = row.epan_id
                run.done_rows += 1
                processed += 1
        finally:
            zeroize(old_material)
            zeroize(new_material)
        if not self._pans.by_dek(run.old_key_id):
            run.status = ReencryptionStatus.COMPLETED
            run.completed_at = now
            run.stats = {"rows": run.done_rows}
            self._emit(
                "reencryption_run.completed",
                "ReencryptionRun",
                run.vrun_id,
                {
                    "run_id": run.vrun_id,
                    "scope": run.scope,
                    "old_key_id": run.old_key_id,
                    "new_key_id": run.new_key_id,
                    "total_rows": run.total_rows,
                    "duration_seconds": int(
                        (now - (run.started_at or now)).total_seconds()
                    ),
                },
            )
        self._runs.save(run)
        return run

    def record_external_progress(
        self, vrun_id: str, *, done_rows: int, cursor_ref: str | None, completed: bool
    ) -> ReencryptionRunRecord:
        """P4: the owning service (kyc/onboarding) reports rewrap progress."""
        run = self._runs.get(vrun_id)
        if run is None:
            raise NotFoundError("reencryption run not found", code="reencryption_run_not_found")
        now = self._clock.now()
        if run.status is ReencryptionStatus.PENDING:
            run.status = ReencryptionStatus.RUNNING
            run.started_at = now
        run.done_rows = done_rows
        run.cursor_ref = cursor_ref
        if completed:
            run.status = ReencryptionStatus.COMPLETED
            run.completed_at = now
            self._emit(
                "reencryption_run.completed",
                "ReencryptionRun",
                run.vrun_id,
                {
                    "run_id": run.vrun_id,
                    "scope": run.scope,
                    "old_key_id": run.old_key_id,
                    "new_key_id": run.new_key_id,
                    "total_rows": run.total_rows,
                    "duration_seconds": int((now - (run.started_at or now)).total_seconds()),
                },
            )
        self._runs.save(run)
        return run

    # ---------------------------------------------------------- pepper migration

    def migrate_pepper(
        self, *, old_pepper_id: str, new_pepper_id: str, batch_size: int = 500, actor: str
    ) -> int:
        """F9 background pass: derive a new-pepper token per encrypted_pans
        row, alias old->new for kernel resolution; DEK unchanged."""
        migrated = 0
        new_pepper = bytearray(self.key_material(new_pepper_id))
        dek_cache: dict[str, bytearray] = {}
        try:
            for token_row in sorted(self._tokens.all(), key=lambda row: row.token):
                if migrated >= batch_size:
                    break
                if token_row.pepper_kid != old_pepper_id:
                    continue
                if self._aliases.resolve(token_row.token) is not None:
                    continue  # idempotent re-run
                epan = self._pans.get_by_token(token_row.token)
                if epan is None:
                    continue
                if epan.dek_key_id not in dek_cache:
                    dek_cache[epan.dek_key_id] = bytearray(
                        self.key_material(epan.dek_key_id, allow_reencryption=True)
                    )
                dek = dek_cache[epan.dek_key_id]
                pan = bytearray(
                    aes_gcm_decrypt(
                        bytes(dek), epan.pan_ciphertext, pan_aad(token_row.token), epan.nonce
                    )
                )
                try:
                    new_hmac = hmac_sha256_hex(bytes(new_pepper), bytes(pan))
                    new_token = make_vault_id(
                        "tok", {"pan_hmac": new_hmac, "pepper_kid": new_pepper_id}
                    )
                    if self._tokens.get(new_token) is None:
                        clone = CardTokenRecord(
                            token=new_token,
                            pan_hmac=new_hmac,
                            pepper_kid=new_pepper_id,
                            bin=token_row.bin,
                            last4=token_row.last4,
                            scheme=token_row.scheme,
                            expiry_month=token_row.expiry_month,
                            expiry_year=token_row.expiry_year,
                            status=token_row.status,
                            origin=token_row.origin,
                            merchant_id=token_row.merchant_id,
                            customer_id=token_row.customer_id,
                            created_at=self._clock.now(),
                        )
                        self._tokens.save(clone)
                        fresh_nonce = self._hsm.random(GCM_NONCE_BYTES)
                        self._pans.save(
                            EncryptedPanRecord(
                                epan_id=make_vault_id(
                                    "epan", {"token": new_token, "dek_kid": epan.dek_key_id}
                                ),
                                token=new_token,
                                dek_key_id=epan.dek_key_id,
                                nonce=fresh_nonce,
                                pan_ciphertext=aes_gcm_encrypt(
                                    bytes(dek), bytes(pan), pan_aad(new_token), fresh_nonce
                                ),
                                aad=pan_aad(new_token).decode("utf-8"),
                                created_at=self._clock.now(),
                            )
                        )
                        self._emit(
                            "card_token.created",
                            "CardToken",
                            new_token,
                            {
                                "token": new_token,
                                "bin": token_row.bin,
                                "last4": token_row.last4,
                                "scheme": token_row.scheme,
                                "expiry_month": token_row.expiry_month,
                                "expiry_year": token_row.expiry_year,
                                "merchant_id": token_row.merchant_id,
                                "customer_id": token_row.customer_id,
                                "origin": token_row.origin,
                            },
                        )
                finally:
                    zeroize(pan)
                self._aliases.save(
                    TokenAliasRecord(
                        old_token=token_row.token,
                        new_token=new_token,
                        old_pepper_kid=old_pepper_id,
                        new_pepper_kid=new_pepper_id,
                        migrated_at=self._clock.now(),
                    )
                )
                migrated += 1
        finally:
            zeroize(new_pepper)
            for buffer in dek_cache.values():
                zeroize(buffer)
        if migrated:
            self._audit.append(
                "pepper_migration_batch",
                {"old_pepper": old_pepper_id, "new_pepper": new_pepper_id, "count": migrated},
                actor=actor,
            )
        return migrated

    # ---------------------------------------------------------------- material

    def active_key(self, kind: KeyKind, key_name: str | None = None) -> VaultKeyRecord:
        record = self._keys.active(kind, key_name)
        if record is None:
            raise CryptoUnavailableError(f"no ACTIVE {kind.value} key")
        return record

    def key_material(self, vkey_id: str, *, allow_reencryption: bool = False) -> bytes:
        """Unwrap (or serve from the 15-min cache) the plaintext material of a
        DEK/PEPPER/EGRESS_HMAC key. COMPROMISED keys decrypt only inside the
        forced re-encryption job (flag-gated, FSM 3)."""
        record = self._keys.get(vkey_id)
        if record is None or record.kind is KeyKind.LMK:
            raise CryptoUnavailableError("key material unavailable")
        if record.status is KeyStatus.DESTROYED:
            raise CryptoUnavailableError("key destroyed")
        if record.status is KeyStatus.COMPROMISED and not (
            allow_reencryption and vkey_id in self._reencryption_only
        ):
            raise CryptoUnavailableError("compromised key is flag-gated to forced re-encryption")
        now = self._clock.now()
        cached = self._material_cache.get(vkey_id)
        if cached is not None and (now - cached[1]).total_seconds() < DEK_CACHE_TTL_S:
            return bytes(cached[0])
        material = self._unwrap_material(record)
        self._material_cache[vkey_id] = (bytearray(material), now)
        return material

    def dek_cache_age_seconds(self) -> int | None:
        """Age of the freshest cached material entry (health surface)."""
        if not self._material_cache:
            return None
        newest = max(cached_at for _, cached_at in self._material_cache.values())
        return int((self._clock.now() - newest).total_seconds())

    def active_dek(self) -> tuple[bytes, str]:
        record = self.active_key(KeyKind.DEK)
        return self.key_material(record.vkey_id), record.vkey_id

    def active_pepper(self) -> tuple[bytes, str]:
        record = self.active_key(KeyKind.PEPPER)
        return self.key_material(record.vkey_id), record.vkey_id

    # ------------------------------------------------------------ egress grants

    def sign_egress_grant(self, payload: dict) -> str:
        """HMAC-SHA256 over canonical_json(payload) under the ACTIVE
        kernel/egress-grant-hmac key, base64 (kernel-parity helper: the
        kernel signs in production; simulators and tests sign here)."""
        record = self.active_key(KeyKind.EGRESS_HMAC, "kernel/egress-grant-hmac")
        material = bytearray(self.key_material(record.vkey_id))
        try:
            digest = hmac_mod.new(
                bytes(material), canonical_json(payload), "sha256"
            ).digest()
        finally:
            zeroize(material)
        return base64.b64encode(digest).decode("ascii")

    def verify_egress_grant(self, payload: dict, sig_b64: str) -> bool:
        """Accept the current or previous EGRESS_HMAC generation (90-day
        crypto period with a dual-generation verify window)."""
        try:
            presented = base64.b64decode(sig_b64, validate=True)
        except Exception:
            return False
        generations = [
            record
            for record in self._keys.generations(KeyKind.EGRESS_HMAC, "kernel/egress-grant-hmac")
            if record.status in (KeyStatus.ACTIVE, KeyStatus.DEPRECATED)
        ]
        for record in generations[-2:]:
            material = bytearray(self.key_material(record.vkey_id))
            try:
                expected = hmac_mod.new(
                    bytes(material), canonical_json(payload), "sha256"
                ).digest()
            finally:
                zeroize(material)
            if hmac_mod.compare_digest(expected, presented):
                return True
        return False

    # --------------------------------------------------------------- metadata

    def list_keys(self) -> list[dict]:
        """GET /vault/v1/keys — metadata only; never material, wrapped or not."""
        out = []
        for record in sorted(self._keys.all(), key=lambda row: (row.kind.value, row.generation)):
            out.append(
                {
                    "vkey_id": record.vkey_id,
                    "kind": record.kind.value,
                    "generation": record.generation,
                    "status": record.status.value,
                    "kcv": record.kcv,
                    "openbao_key_name": record.openbao_key_name,
                    "openbao_key_version": record.openbao_key_version,
                    "ceremony_id": record.ceremony_id,
                    "created_at": record.created_at,
                    "activated_at": record.activated_at,
                    "deprecated_at": record.deprecated_at,
                    "destroyed_at": record.destroyed_at,
                    "rotation_due_at": record.rotation_due_at,
                }
            )
        return out

    def keys_rotation_due(self) -> list[str]:
        now = self._clock.now()
        return [
            record.vkey_id
            for record in self._keys.all()
            if record.status is KeyStatus.ACTIVE
            and record.rotation_due_at is not None
            and now >= record.rotation_due_at
        ]
