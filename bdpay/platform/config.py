"""Environment-driven platform settings (plain ``os.environ`` parsing).

One codebase, two license modes via ``PLATFORM_MODE`` (``PSP`` | ``PSO``,
arch Part II §(a)). Money thresholds are integer paisa throughout:

- refund two-eyes threshold: BDT 50,000        =  5_000_000 paisa
- eKYC simplified tier per-txn: BDT 2.5 lakh   = 25_000_000 paisa
- eKYC simplified tier monthly: BDT 5 lakh     = 50_000_000 paisa
- eKYC simplified wallet cap:  BDT 4 lakh      = 40_000_000 paisa
- CTR threshold: BDT 10 lakh cash/day          = 100_000_000 paisa

``Settings.from_env(env)`` is pure (testable with any mapping);
``get_settings()`` caches one instance from ``os.environ`` and
``reset_settings()`` clears the cache for tests.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "EkycTierLimits",
    "Settings",
    "get_settings",
    "reset_settings",
]

_VALID_PLATFORM_MODES = ("PSP", "PSO")
_VALID_CONNECTOR_MODES = ("simulator", "sandbox", "live", "disabled")


@dataclass(frozen=True)
class EkycTierLimits:
    """Per-tier eKYC limits, integer paisa (spec 09 simplified-account tier)."""

    per_txn_minor: int
    monthly_minor: int
    wallet_balance_cap_minor: int

    def __post_init__(self) -> None:
        for name in ("per_txn_minor", "monthly_minor", "wallet_balance_cap_minor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int (paisa), got {value!r}")


def _default_simplified_tier() -> EkycTierLimits:
    return EkycTierLimits(
        per_txn_minor=25_000_000,  # BDT 2.5 lakh
        monthly_minor=50_000_000,  # BDT 5 lakh
        wallet_balance_cap_minor=40_000_000,  # BDT 4 lakh
    )


@dataclass(frozen=True)
class Settings:
    """Immutable platform settings; constructed once per process."""

    platform_mode: str = "PSP"
    # COMP-08 (Foster Payments analog): the PSP-side merchant-of-record /
    # payment-aggregation authorization.  Mirrors the PSO ``pso_license_active``
    # gate: holding or settling THIRD-PARTY merchant funds (the
    # ``MERCHANT_SETTLEMENT`` credit at capture) requires a live PSP licence.
    # Fail-closed: default false, so the unsafe posture is never the default.
    psp_license_active: bool = False  # PSP_LICENSE_ACTIVE
    refund_two_eyes_threshold_minor: int = 5_000_000  # BDT 50k
    ekyc_simplified: EkycTierLimits = field(default_factory=_default_simplified_tier)
    ctr_threshold_minor: int = 100_000_000  # BDT 10 lakh cash/day
    # CASH_AGENT/CASH_ATM movements are not in the current digital-payment launch
    # surface. Keep the daily CTR aggregate registered, but report the cash
    # pipeline as disabled unless a deployment explicitly enables the cash scope
    # and asserts that a trusted cash-event producer/consumer path is wired.
    cash_events_enabled: bool = False  # CASH_EVENTS_ENABLED
    cash_events_wired: bool = False  # CASH_EVENTS_WIRED
    # COMP-01: sanctions watchlist feed source loaded at boot.  "" (default) =
    # no ingest, screener stays fail-closed until the real UN/BFIU feed is
    # wired; "bootstrap" = the deterministic seed feed (see
    # bdpay/compliance/sanctions/feed.py).  Must be a known value or boot fails.
    sanctions_feed_source: str = ""  # SANCTIONS_FEED_SOURCE
    # BDPAY-08: opt-in fail-closed sanctions-list freshness gate on the
    # disbursement path. Default OFF preserves the legacy alarm-only posture
    # (a stale list raises the spec/12 §G side-channel STALE alarm but
    # screening continues against the previously ingested list). When ON, a
    # stale watchlist refuses disbursement batch submission — money cannot
    # move against an obsolete list. Fail-closed default: the unsafe posture
    # (blocking live disbursement) is never the default.
    sanctions_stale_fail_closed: bool = False  # SANCTIONS_STALE_FAIL_CLOSED
    # Optional boot-time AML RulePack activation. The artifact must already be
    # signed out-of-band by CAMLCO custody; the app only registers the public key
    # and verifies/loads the artifact. Empty means "do not activate at boot".
    aml_rule_pack_artifact_path: str = ""  # AML_RULE_PACK_PATH
    aml_rule_pack_signer_id: str = ""  # AML_RULE_PACK_SIGNER_ID
    aml_rule_pack_public_key_b64: str = ""  # AML_RULE_PACK_PUBLIC_KEY_B64
    aml_rule_pack_registry_version: int = 1  # AML_RULE_PACK_REGISTRY_VERSION
    aml_rule_pack_activated_by: str = "oper_camlco_bootstrap"
    database_url: str = "postgresql://localhost:5432/bdpay"
    # Non-superuser NOLOGIN application role the ledger connects AS (SET ROLE),
    # so append-only RLS on the ledger binds in production. The login role must
    # be granted membership in this role. It must stay non-empty; disabling the
    # role would bypass the money-ledger append-only RLS control.
    # (audit P0 bdpay-ledger-rls-bypassed-prod)
    ledger_app_role: str = "ledger_app_role"
    # F01: durable custody for the ledger checkpoint signing key and the
    # trusted-key set. Checkpoint trust MUST outlive the process — a fresh
    # per-process key made every restart/replica read historical checkpoints
    # as forged and engaged the fail-closed write gate. See
    # bdpay.ledger.trust_registry.
    ledger_checkpoint_trust_path: str = (
        "var/ledger/checkpoint_trust.json"  # LEDGER_CHECKPOINT_TRUST_PATH
    )
    redis_url: str = "redis://localhost:6379/0"
    connector_mode: str = "simulator"
    # Non-secret JSON deployment config for connector activation. The file
    # carries base URLs and credential refs only; values resolve through a
    # CredentialResolver at composition time.
    connector_config_path: str = ""  # CONNECTOR_CONFIG_PATH
    # -- spec/16 LR-5 config keys ("Month-one resilience patches") -----------
    webhook_secret_grace_hours: int = 24  # WEBHOOK_SECRET_GRACE_HOURS (min 1)
    manual_review_max_queue_depth: int = 500  # MANUAL_REVIEW_MAX_QUEUE_DEPTH
    manual_review_avg_review_seconds: int = 300  # drain-projection input (§API I)
    manual_review_active_reviewers: int = 1  # drain-projection input (§API I)
    # -- spec/16 LR-4 config keys ("Pre-license merchant wedge") --------------
    payment_link_default_ttl_hours: int = 72  # PAYMENT_LINK_DEFAULT_TTL_HOURS
    sandbox_public: bool = False  # SANDBOX_PUBLIC (boot invariants, §API F)
    sandbox_signup_ttl_hours: int = 24  # SANDBOX_SIGNUP_TTL_HOURS
    sandbox_retention_days: int = 90  # SANDBOX_RETENTION_DAYS
    # -- spec/13 Bangla QR config keys ----------------------------------------
    # Soft-launch posture (spec/13 Failure Modes, ARCHITECTURE R2): issuer
    # scan-to-pay works without the BB template assignment.  QR_SCAN_ONLY=true
    # refuses the acquirer issuance surface (issue static/dynamic) while the
    # issuer resolve/pay surface stays live.
    qr_scan_only: bool = False  # QR_SCAN_ONLY
    # BP-G4d: opt-in CERT WebhookPipeline on gateway POST /v1/webhooks/{id}.
    # Default OFF preserves verify+webhook_sink money path. When ON, inbound
    # uses WebhookPipeline + runner.webhook_inbound_store (DUPLICATE-terminal,
    # fail-closed) and dual-writes accepted results to webhook_sink.
    gateway_webhook_pipeline: bool = False  # GATEWAY_WEBHOOK_PIPELINE
    # BB merchant-account template identity (spec/13 Open question 1) —
    # interim soft-launch values; the real GUID/sub-ID land with acquirer
    # onboarding as a config change, never code.
    qr_acquirer_id: str = "BDPAY-INTERIM-ACQ"  # QR_ACQUIRER_ID
    qr_psp_sub_id: str = "BDPAY01"  # QR_PSP_SUB_ID
    qr_template_root_id: int = 26  # QR_TEMPLATE_ROOT_ID (EMVCo range 26-51)
    qr_scheme_guid: str = "BD.BANGLAQR.NPSB"  # QR_SCHEME_GUID

    def __post_init__(self) -> None:
        if self.platform_mode not in _VALID_PLATFORM_MODES:
            raise ValueError(
                f"PLATFORM_MODE must be one of {_VALID_PLATFORM_MODES}, "
                f"got {self.platform_mode!r}"
            )
        if self.connector_mode not in _VALID_CONNECTOR_MODES:
            raise ValueError(
                f"connector mode must be one of {_VALID_CONNECTOR_MODES}, "
                f"got {self.connector_mode!r}"
            )
        for name in ("refund_two_eyes_threshold_minor", "ctr_threshold_minor"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive int (paisa), got {value!r}")
        if not isinstance(self.ekyc_simplified, EkycTierLimits):
            raise ValueError("ekyc_simplified must be an EkycTierLimits instance")
        for name in (
            "database_url",
            "ledger_app_role",
            "redis_url",
            "qr_acquirer_id",
            "qr_psp_sub_id",
            "qr_scheme_guid",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.connector_config_path, str):
            raise ValueError(
                f"connector_config_path must be a string, got "
                f"{self.connector_config_path!r}"
            )
        if (
            isinstance(self.qr_template_root_id, bool)
            or not isinstance(self.qr_template_root_id, int)
            or not 26 <= self.qr_template_root_id <= 51
        ):
            raise ValueError(
                f"qr_template_root_id must be an int in 26-51 (EMVCo merchant-account "
                f"range), got {self.qr_template_root_id!r}"
            )
        if not isinstance(self.qr_scan_only, bool):
            raise ValueError(f"qr_scan_only must be a boolean, got {self.qr_scan_only!r}")
        if not isinstance(self.gateway_webhook_pipeline, bool):
            raise ValueError(
                f"gateway_webhook_pipeline must be a boolean, got "
                f"{self.gateway_webhook_pipeline!r}"
            )
        if not isinstance(self.psp_license_active, bool):
            raise ValueError(
                f"psp_license_active must be a boolean, got {self.psp_license_active!r}"
            )
        for name in ("cash_events_enabled", "cash_events_wired"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be a boolean, got {value!r}")
        if not isinstance(self.sanctions_stale_fail_closed, bool):
            raise ValueError(
                f"sanctions_stale_fail_closed must be a boolean, "
                f"got {self.sanctions_stale_fail_closed!r}"
            )
        if self.cash_events_wired and not self.cash_events_enabled:
            raise ValueError(
                "cash_events_wired requires cash_events_enabled; a cash-event "
                "producer/consumer path cannot be active while cash rails are "
                "outside launch scope"
            )
        for name in (
            "webhook_secret_grace_hours",  # spec/16: default 24, min 1
            "manual_review_max_queue_depth",
            "manual_review_avg_review_seconds",
            "manual_review_active_reviewers",
            "payment_link_default_ttl_hours",  # spec/16 LR-4
            "sandbox_signup_ttl_hours",
            "sandbox_retention_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an int >= 1, got {value!r}")
        if not isinstance(self.sandbox_public, bool):
            raise ValueError(
                f"sandbox_public must be a boolean, got {self.sandbox_public!r}"
            )
        for name in (
            "sanctions_feed_source",
            "aml_rule_pack_artifact_path",
            "aml_rule_pack_signer_id",
            "aml_rule_pack_public_key_b64",
            "aml_rule_pack_activated_by",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string, got {value!r}")
        if (
            isinstance(self.aml_rule_pack_registry_version, bool)
            or not isinstance(self.aml_rule_pack_registry_version, int)
            or self.aml_rule_pack_registry_version < 1
        ):
            raise ValueError(
                "aml_rule_pack_registry_version must be an int >= 1, "
                f"got {self.aml_rule_pack_registry_version!r}"
            )
        has_pack_path = bool(self.aml_rule_pack_artifact_path)
        has_key_material = bool(
            self.aml_rule_pack_signer_id or self.aml_rule_pack_public_key_b64
        )
        if has_pack_path and not (
            self.aml_rule_pack_signer_id
            and self.aml_rule_pack_public_key_b64
            and self.aml_rule_pack_activated_by
        ):
            raise ValueError(
                "AML rule pack boot activation requires AML_RULE_PACK_PATH, "
                "AML_RULE_PACK_SIGNER_ID, AML_RULE_PACK_PUBLIC_KEY_B64, and "
                "AML_RULE_PACK_ACTIVATED_BY"
            )
        if has_key_material and not has_pack_path:
            raise ValueError(
                "AML_RULE_PACK_PATH is required when AML rule pack signing key "
                "configuration is provided"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        """Build Settings from an environment mapping (pure, test-friendly)."""
        defaults = cls()
        tier_defaults = _default_simplified_tier()
        return cls(
            platform_mode=_str_var(env, "PLATFORM_MODE", defaults.platform_mode),
            psp_license_active=_bool_var(
                env, "PSP_LICENSE_ACTIVE", defaults.psp_license_active
            ),
            refund_two_eyes_threshold_minor=_int_var(
                env,
                "REFUND_TWO_EYES_THRESHOLD_MINOR",
                defaults.refund_two_eyes_threshold_minor,
            ),
            ekyc_simplified=EkycTierLimits(
                per_txn_minor=_int_var(
                    env, "EKYC_SIMPLIFIED_PER_TXN_LIMIT_MINOR", tier_defaults.per_txn_minor
                ),
                monthly_minor=_int_var(
                    env, "EKYC_SIMPLIFIED_MONTHLY_LIMIT_MINOR", tier_defaults.monthly_minor
                ),
                wallet_balance_cap_minor=_int_var(
                    env,
                    "EKYC_SIMPLIFIED_WALLET_BALANCE_CAP_MINOR",
                    tier_defaults.wallet_balance_cap_minor,
                ),
            ),
            ctr_threshold_minor=_int_var(env, "CTR_THRESHOLD_MINOR", defaults.ctr_threshold_minor),
            cash_events_enabled=_bool_var(
                env, "CASH_EVENTS_ENABLED", defaults.cash_events_enabled
            ),
            cash_events_wired=_bool_var(
                env, "CASH_EVENTS_WIRED", defaults.cash_events_wired
            ),
            sanctions_feed_source=_str_var(
                env, "SANCTIONS_FEED_SOURCE", defaults.sanctions_feed_source
            ),
            sanctions_stale_fail_closed=_bool_var(
                env, "SANCTIONS_STALE_FAIL_CLOSED", defaults.sanctions_stale_fail_closed
            ),
            aml_rule_pack_artifact_path=_str_var(
                env, "AML_RULE_PACK_PATH", defaults.aml_rule_pack_artifact_path
            ),
            aml_rule_pack_signer_id=_str_var(
                env, "AML_RULE_PACK_SIGNER_ID", defaults.aml_rule_pack_signer_id
            ),
            aml_rule_pack_public_key_b64=_str_var(
                env, "AML_RULE_PACK_PUBLIC_KEY_B64", defaults.aml_rule_pack_public_key_b64
            ),
            aml_rule_pack_registry_version=_int_var(
                env,
                "AML_RULE_PACK_REGISTRY_VERSION",
                defaults.aml_rule_pack_registry_version,
            ),
            aml_rule_pack_activated_by=_str_var(
                env, "AML_RULE_PACK_ACTIVATED_BY", defaults.aml_rule_pack_activated_by
            ),
            database_url=_str_var(env, "DATABASE_URL", defaults.database_url),
            ledger_app_role=_str_var(env, "LEDGER_APP_ROLE", defaults.ledger_app_role),
            ledger_checkpoint_trust_path=_str_var(
                env,
                "LEDGER_CHECKPOINT_TRUST_PATH",
                defaults.ledger_checkpoint_trust_path,
            ),
            redis_url=_str_var(env, "REDIS_URL", defaults.redis_url),
            connector_mode=_str_var(env, "CONNECTOR_MODE", defaults.connector_mode),
            connector_config_path=_str_var(
                env, "CONNECTOR_CONFIG_PATH", defaults.connector_config_path
            ),
            webhook_secret_grace_hours=_int_var(
                env, "WEBHOOK_SECRET_GRACE_HOURS", defaults.webhook_secret_grace_hours
            ),
            manual_review_max_queue_depth=_int_var(
                env, "MANUAL_REVIEW_MAX_QUEUE_DEPTH", defaults.manual_review_max_queue_depth
            ),
            manual_review_avg_review_seconds=_int_var(
                env,
                "MANUAL_REVIEW_AVG_REVIEW_SECONDS",
                defaults.manual_review_avg_review_seconds,
            ),
            manual_review_active_reviewers=_int_var(
                env,
                "MANUAL_REVIEW_ACTIVE_REVIEWERS",
                defaults.manual_review_active_reviewers,
            ),
            payment_link_default_ttl_hours=_int_var(
                env,
                "PAYMENT_LINK_DEFAULT_TTL_HOURS",
                defaults.payment_link_default_ttl_hours,
            ),
            sandbox_public=_bool_var(env, "SANDBOX_PUBLIC", defaults.sandbox_public),
            sandbox_signup_ttl_hours=_int_var(
                env, "SANDBOX_SIGNUP_TTL_HOURS", defaults.sandbox_signup_ttl_hours
            ),
            sandbox_retention_days=_int_var(
                env, "SANDBOX_RETENTION_DAYS", defaults.sandbox_retention_days
            ),
            qr_scan_only=_bool_var(env, "QR_SCAN_ONLY", defaults.qr_scan_only),
            gateway_webhook_pipeline=_bool_var(
                env, "GATEWAY_WEBHOOK_PIPELINE", defaults.gateway_webhook_pipeline
            ),
            qr_acquirer_id=_str_var(env, "QR_ACQUIRER_ID", defaults.qr_acquirer_id),
            qr_psp_sub_id=_str_var(env, "QR_PSP_SUB_ID", defaults.qr_psp_sub_id),
            qr_template_root_id=_int_var(
                env, "QR_TEMPLATE_ROOT_ID", defaults.qr_template_root_id
            ),
            qr_scheme_guid=_str_var(env, "QR_SCHEME_GUID", defaults.qr_scheme_guid),
        )


def _str_var(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip()
    if not value:
        raise ValueError(f"{name} is set but empty")
    return value


def _int_var(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None:
        return default
    try:
        return int(raw.strip(), 10)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer (paisa), got {raw!r}") from exc


def _bool_var(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise ValueError(f"{name} must be a boolean (true/false), got {raw!r}")


_settings_cache: Settings | None = None


def get_settings() -> Settings:
    """Process-wide cached Settings built from ``os.environ``."""
    global _settings_cache
    if _settings_cache is None:
        _settings_cache = Settings.from_env(os.environ)
    return _settings_cache


def reset_settings() -> None:
    """Clear the cache (tests only — call after mutating the environment)."""
    global _settings_cache
    _settings_cache = None
