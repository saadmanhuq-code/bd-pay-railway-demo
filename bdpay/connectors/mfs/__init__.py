"""spec/12 MFS connectors: bKash, Nagad, Rocket aggregator, SMS notification.

Each adapter implements the frozen ``bdpay.connectors.sdk`` contract with the
four spec/10 modes: SIMULATOR (deterministic ScenarioEngine), SANDBOX and
PRODUCTION (real wire code fully written — activation needs only credentials
config), DISABLED (refusal), plus the MANUAL_LOCAL contingency where the spec
designs one (Rocket).
"""

from bdpay.connectors.mfs.bkash import (
    BkashIpnWebhookHandler,
    BkashPgwConnector,
    BkashSyncStatusTruth,
)
from bdpay.connectors.mfs.handles import (
    InMemoryMfsHandleStore,
    MfsHandleStore,
    PostgresMfsHandleStore,
)
from bdpay.connectors.mfs.ipn import QueryConfirmedIpnHandler
from bdpay.connectors.mfs.nagad import NagadPgwConnector, NagadSyncStatusTruth
from bdpay.connectors.mfs.registration import (
    SPEC12_CONNECTOR_SPECS,
    register_spec12_connectors,
)
from bdpay.connectors.mfs.rocket import RocketAggregatorConnector, RocketSyncStatusTruth
from bdpay.connectors.mfs.sms import SmsOtpConnector
from bdpay.connectors.mfs.token_state import (
    InMemoryTokenStateStore,
    MfsAuthUnavailableError,
    PostgresTokenStateStore,
    TokenCache,
    TokenStateStore,
)

__all__ = [
    "BkashIpnWebhookHandler",
    "BkashPgwConnector",
    "BkashSyncStatusTruth",
    "InMemoryMfsHandleStore",
    "MfsAuthUnavailableError",
    "MfsHandleStore",
    "NagadPgwConnector",
    "NagadSyncStatusTruth",
    "PostgresMfsHandleStore",
    "QueryConfirmedIpnHandler",
    "RocketAggregatorConnector",
    "RocketSyncStatusTruth",
    "SPEC12_CONNECTOR_SPECS",
    "SmsOtpConnector",
    "InMemoryTokenStateStore",
    "PostgresTokenStateStore",
    "TokenStateStore",
    "TokenCache",
    "register_spec12_connectors",
]
