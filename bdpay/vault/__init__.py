"""bdpay.vault — tokenization vault / PCI CDE sub-process (spec/14).

Isolated from the single deployable: own entrypoint (``python -m
bdpay.vault``), own stores, own hash-chained audit trail, shared-secret
channel auth on the internal planes. Depends only on ``bdpay.platform``
primitives. The card connector consumes :mod:`bdpay.vault.client`
(vault-proxy) — the vault itself never imports connector code.
"""

from bdpay.vault.client import (
    HttpVaultEgressClient,
    LocalVaultEgressClient,
    VaultEgressClient,
    VaultEgressDenied,
    VaultEgressUpstreamError,
)
from bdpay.vault.service import VaultService
from bdpay.vault.settings import VaultSettings
from bdpay.vault.wiring import VaultRuntime, build_vault

__all__ = [
    "HttpVaultEgressClient",
    "LocalVaultEgressClient",
    "VaultEgressClient",
    "VaultEgressDenied",
    "VaultEgressUpstreamError",
    "VaultRuntime",
    "VaultService",
    "VaultSettings",
    "build_vault",
]
