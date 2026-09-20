"""spec/12 identity-and-compliance-side connectors.

``porichoy_ekyc_v1`` (IdentityConnector), ``goaml_reporter_v1``
(AmlFilingConnector — durable never-drop queue), ``sanctions_feed_v1``
(SanctionsConnector — UN/BFIU feeds + ported Jaro-Winkler screening) and
``hsm_thales_v1`` (PKCS#11-shaped boundary with the soft-HSM mode).
"""

from bdpay.connectors.identity.goaml import GoamlReporterConnector
from bdpay.connectors.identity.hsm import HsmThalesConnector
from bdpay.connectors.identity.matcher import (
    WatchlistEntry,
    jaro_winkler,
    match_score,
    normalize_name,
    screen_names,
)
from bdpay.connectors.identity.porichoy import (
    NidFormatError,
    PorichoyEkycConnector,
    PorichoyUnavailableError,
    normalize_nid,
)
from bdpay.connectors.identity.sanctions import (
    FeedUnavailableError,
    SanctionsFeedConnector,
)

__all__ = [
    "FeedUnavailableError",
    "GoamlReporterConnector",
    "HsmThalesConnector",
    "NidFormatError",
    "PorichoyEkycConnector",
    "PorichoyUnavailableError",
    "SanctionsFeedConnector",
    "WatchlistEntry",
    "jaro_winkler",
    "match_score",
    "normalize_name",
    "normalize_nid",
    "screen_names",
]
