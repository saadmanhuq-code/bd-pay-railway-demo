"""Composition root for the vault sub-process (spec/14 topology).

``build_vault`` assembles the full object graph (soft-HSM key source,
stores, audit chain, SAD store, key manager, service, channel auth) with an
injected Clock — the same wiring serves the deterministic test suite and the
``__main__`` entrypoint. The vault never imports kernel/ledger/compliance/
connectors/qr (isolated CDE build).
"""

from __future__ import annotations

from datetime import datetime

from bdpay.platform.clock import Clock
from bdpay.vault.audit import VaultAuditChain
from bdpay.vault.channel import DEFAULT_ALLOWLIST_OPERATIONS, SharedSecretChannelAuth
from bdpay.vault.crypto import SoftHsm
from bdpay.vault.egress import AcquirerEgressExecutor, HttpxAcquirerExecutor, SimulatedAcquirer
from bdpay.vault.ids import make_vault_id
from bdpay.vault.keys import KeyManager
from bdpay.vault.records import VaultOutboxRecord
from bdpay.vault.sad import SadStore
from bdpay.vault.service import VaultService
from bdpay.vault.settings import VaultSettings
from bdpay.vault.stores import (
    AllowlistStore,
    CeremonyStore,
    CustodianStore,
    DetokAuditStore,
    IdempotencyStore,
    KeyStore,
    OutboxStore,
    PanStore,
    ReencryptionRunStore,
    SessionStore,
    TokenAliasStore,
    TokenStore,
)

__all__ = ["VaultRuntime", "build_vault"]

_BOOTSTRAP_CUSTODIANS = (
    ("Security Officer A", "ops:security-officer-a", "line-security"),
    ("Platform Lead B", "ops:platform-lead-b", "line-platform"),
)


class VaultRuntime:
    """The assembled vault graph (one per process / one per test)."""

    def __init__(
        self,
        *,
        clock: Clock,
        hsm: SoftHsm,
        service: VaultService,
        channel: SharedSecretChannelAuth,
        outbox: OutboxStore,
    ) -> None:
        self.clock = clock
        self.hsm = hsm
        self.service = service
        self.channel = channel
        self.outbox = outbox


def build_vault(
    settings: VaultSettings,
    clock: Clock,
    *,
    egress_executor: AcquirerEgressExecutor | None = None,
    provision_keys: bool = True,
) -> VaultRuntime:
    hsm = SoftHsm(
        bytes.fromhex(settings.master_key_hex) if settings.master_key_hex else None,
        production=settings.is_production,
    )
    audit = VaultAuditChain(clock)
    outbox = OutboxStore()

    def emit(event_type: str, subject_type: str, subject_id: str, payload: dict) -> None:
        occurred_at: datetime = clock.now()
        outbox.append(
            VaultOutboxRecord(
                vobx_id=make_vault_id(
                    "vobx",
                    {"type": event_type, "subject_id": subject_id, "occurred_at": occurred_at},
                ),
                event_type=event_type,
                subject_type=subject_type,
                subject_id=subject_id,
                payload=payload,
                occurred_at=occurred_at,
            )
        )

    tokens = TokenStore()
    pans = PanStore()
    custodians = CustodianStore()
    key_manager = KeyManager(
        hsm=hsm,
        keys=KeyStore(),
        custodians=custodians,
        ceremonies=CeremonyStore(),
        pans=pans,
        tokens=tokens,
        aliases=TokenAliasStore(),
        runs=ReencryptionRunStore(),
        audit=audit,
        outbox=outbox,
        clock=clock,
        emit_event=emit,
    )
    sad = SadStore(clock, dek_provider=key_manager.active_dek, rng=hsm.random)
    executor: AcquirerEgressExecutor
    if egress_executor is not None:
        executor = egress_executor
    elif settings.egress_mode == "simulator":
        executor = SimulatedAcquirer()
    else:
        # Live/sandbox wire path: SPKI pinning is mandatory (require_pins) — a
        # live acquirer channel that carries cleartext PAN must not run on CA
        # validation alone (PCI Req 4); zero pins fails closed at construction.
        executor = HttpxAcquirerExecutor(
            verify=settings.tls_verify,
            spki_pins=settings.acquirer_spki_pins,
            require_pins=True,
        )
    service = VaultService(
        clock=clock,
        hsm=hsm,
        key_manager=key_manager,
        tokens=tokens,
        pans=pans,
        sessions=SessionStore(),
        detok_audit=DetokAuditStore(),
        allowlist=AllowlistStore(),
        idempotency=IdempotencyStore(),
        outbox=outbox,
        audit=audit,
        sad=sad,
        egress_executor=executor,
        acquirer_hosts=settings.acquirer_hosts,
        import_window_open=settings.import_window_open,
    )
    service.seed_allowlist(dict(DEFAULT_ALLOWLIST_OPERATIONS), actor="vault-bootstrap")
    channel = SharedSecretChannelAuth(dict(settings.caller_secrets), clock)
    if provision_keys:
        custodian_ids = [
            key_manager.appoint_custodian(
                person_name=name,
                operator_identity=operator,
                reporting_line=line,
                approval_request_id=make_vault_id("appr", {"purpose": f"custodian:{operator}"}),
                actor="vault-bootstrap",
            ).vcus_id
            for name, operator, line in _BOOTSTRAP_CUSTODIANS
        ]
        key_manager.provision_initial_keys(actor="vault-bootstrap", custodian_ids=custodian_ids)
    return VaultRuntime(clock=clock, hsm=hsm, service=service, channel=channel, outbox=outbox)
