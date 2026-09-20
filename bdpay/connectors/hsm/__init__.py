"""bdpay.connectors.hsm — secure-element key-custody providers.

``yubi`` is the YubiHSM 2 key-custody provider (``hsm_yubi_v1``): Ed25519
signing, KEK wrap/unwrap, attestation and health over the certified secure
elements that custody the platform's non-card keys. Card/PIN custody stays
with the payment-HSM boundary (``hsm_thales_v1``, spec/12 §I) and activates
with the card programme.
"""

from bdpay.connectors.hsm.yubi import (
    CONNECTOR_ID,
    CustodyCommandRejectedError,
    CustodyKeyNotFoundError,
    CustodyMode,
    CustodyUnavailableError,
    HardwareCustodySession,
    KeyCustodyProvider,
    SoftCustodySession,
    YubiHsmSdkSession,
    YubiHsmSettings,
    YubiKeyCustodyProvider,
    custody_mode_from_config,
    record_custody_health,
)

__all__ = [
    "CONNECTOR_ID",
    "CustodyCommandRejectedError",
    "CustodyKeyNotFoundError",
    "CustodyMode",
    "CustodyUnavailableError",
    "HardwareCustodySession",
    "KeyCustodyProvider",
    "SoftCustodySession",
    "YubiHsmSdkSession",
    "YubiHsmSettings",
    "YubiKeyCustodyProvider",
    "custody_mode_from_config",
    "record_custody_health",
]
