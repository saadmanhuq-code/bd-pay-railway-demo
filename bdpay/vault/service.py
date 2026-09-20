"""VaultService — the tokenize / detokenize / lifecycle core (spec/14).

One facade over the stores + key manager, transport-free: the FastAPI layer
(`bdpay.vault.api`) and the in-process vault-proxy client both call these
methods. Every FSM here is refusal-first; every denial is audited; no method
ever returns a PAN beyond bin+last4; no vault transition moves money.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from bdpay.platform.canonical import sha256_canonical
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    ConnectorError,
    IdempotencyConflictError,
    InternalError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
    RateLimitError,
)
from bdpay.vault.audit import VaultAuditChain
from bdpay.vault.cards import (
    detect_scheme,
    expiry_in_past,
    luhn_valid,
    normalize_digits,
    normalize_holder_name,
    truncate_pan,
)
from bdpay.vault.channel import authorization_denied, operation_permitted
from bdpay.vault.crypto import (
    GCM_NONCE_BYTES,
    SoftHsm,
    aes_gcm_decrypt,
    aes_gcm_encrypt,
    hmac_sha256_hex,
    zeroize,
)
from bdpay.vault.egress import (
    AcquirerEgressExecutor,
    EgressTimeoutError,
    EgressUnreachableError,
    host_of,
    raw_response_hash,
    strip_response,
    substitute_template,
    template_slots,
)
from bdpay.vault.ids import make_vault_id
from bdpay.vault.keys import KeyManager, pan_aad
from bdpay.vault.records import (
    TOKEN_TERMINAL,
    AllowlistRecord,
    CardTokenRecord,
    DetokDecision,
    DetokenizationRecord,
    EncryptedPanRecord,
    IntakeSessionRecord,
    IntakeSessionStatus,
    ReencryptionStatus,
    TokenStatus,
    VaultOutboxRecord,
)
from bdpay.vault.sad import SadStore
from bdpay.vault.stores import (
    AllowlistStore,
    DetokAuditStore,
    IdempotencyStore,
    OutboxStore,
    PanStore,
    SessionStore,
    TokenStore,
)

__all__ = ["EGRESS_CALLER_SAN", "PRODUCER", "VaultService", "rfc3339"]

PRODUCER = "tokenization-vault@1.0.0"
EGRESS_CALLER_SAN = "spiffe://bdpay/connector-runner/card-vault-proxy"
INTAKE_TTL = timedelta(minutes=15)
REPLAY_WINDOW = timedelta(hours=24)
GRANT_MAX_WINDOW = timedelta(seconds=120)
NEVER_USED_RETIRE = timedelta(days=90)
CIPHERTEXT_DESTROY_HOLD = timedelta(days=90)
MAX_EGRESS_TIMEOUT_MS = 28_000
MAX_VALIDATION_FAILURES = 5  # the 6th failure voids the session
RETIRE_REASONS = frozenset(
    {"customer_request", "merchant_offboarded", "compromise", "expiry_sweep"}
)
_EXPIRY_GRACE_MONTHS = 1


def rfc3339(value: datetime) -> str:
    """Second-precision RFC 3339 Z form for API/event wire fields."""
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class _RateWindow:
    """Fixed one-minute window counter driven by the injected clock."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._counts: dict[tuple[str, str, str], int] = {}

    def hit(self, kind: str, key: str, limit: int) -> bool:
        minute = self._clock.now().strftime("%Y%m%d%H%M")
        bucket = (kind, key, minute)
        count = self._counts.get(bucket, 0) + 1
        self._counts[bucket] = count
        return count <= limit


class VaultService:
    def __init__(
        self,
        *,
        clock: Clock,
        hsm: SoftHsm,
        key_manager: KeyManager,
        tokens: TokenStore,
        pans: PanStore,
        sessions: SessionStore,
        detok_audit: DetokAuditStore,
        allowlist: AllowlistStore,
        idempotency: IdempotencyStore,
        outbox: OutboxStore,
        audit: VaultAuditChain,
        sad: SadStore,
        egress_executor: AcquirerEgressExecutor,
        acquirer_hosts: tuple[str, ...],
        import_window_open: bool = False,
        intake_ip_limit_per_min: int = 10,
        intake_merchant_limit_per_min: int = 600,
    ) -> None:
        self._clock = clock
        self._hsm = hsm
        self.keys = key_manager
        self._tokens = tokens
        self._pans = pans
        self._sessions = sessions
        self._detok = detok_audit
        self._allowlist = allowlist
        self.idempotency = idempotency
        self._outbox = outbox
        self.audit = audit
        self.sad = sad
        self._executor = egress_executor
        self._acquirer_hosts = frozenset(acquirer_hosts)
        self._import_window_open = import_window_open
        self._rates = _RateWindow(clock)
        self._ip_limit = intake_ip_limit_per_min
        self._merchant_limit = intake_merchant_limit_per_min

    # --------------------------------------------------------------- plumbing

    def emit_event(
        self, event_type: str, subject_type: str, subject_id: str, payload: dict
    ) -> None:
        """Events are produced ONLY via the vault outbox (conventions §5)."""
        occurred_at = self._clock.now()
        record = VaultOutboxRecord(
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
        self._outbox.append(record)

    def authorize(self, caller_san: str, operation: str) -> None:
        """Default-deny allowlist check; denials are audited (spec/14 Plane B)."""
        row = self._allowlist.get(caller_san)
        if row is None or row.revoked_at is not None or not operation_permitted(
            row.operations, operation
        ):
            self.audit.append(
                "caller_denied",
                {"caller_san": caller_san, "operation": operation},
                actor=caller_san,
            )
            raise authorization_denied(operation)

    def seed_allowlist(self, operations_by_san: dict[str, tuple[str, ...]], *, actor: str) -> None:
        for caller_san, operations in operations_by_san.items():
            self._allowlist.save(
                AllowlistRecord(
                    caller_san=caller_san,
                    operations=tuple(operations),
                    approval_request_id=make_vault_id(
                        "appr", {"purpose": f"allowlist:{caller_san}"}
                    ),
                    created_at=self._clock.now(),
                )
            )
        self.audit.append(
            "allowlist_seeded", {"callers": sorted(operations_by_san)}, actor=actor
        )

    def known_caller(self, caller_san: str) -> bool:
        row = self._allowlist.get(caller_san)
        return row is not None and row.revoked_at is None

    # ---------------------------------------------------------- intake sessions

    def create_intake_session(
        self,
        *,
        caller_san: str,
        merchant_id: str,
        purpose: str,
        payment_intent_id: str | None = None,
        customer_id: str | None = None,
        allowed_origins: list[str] | None = None,
        cvv_required: bool = True,
    ) -> dict:
        self.authorize(caller_san, "intake_session.create")
        if purpose not in ("payment", "save_card"):
            raise InvalidRequestError("unknown intake purpose", code="purpose_binding_missing")
        if purpose == "payment" and not payment_intent_id:
            raise InvalidRequestError(
                "payment purpose requires payment_intent_id", code="purpose_binding_missing"
            )
        if purpose == "save_card" and not customer_id:
            raise InvalidRequestError(
                "save_card purpose requires customer_id", code="purpose_binding_missing"
            )
        if not merchant_id.startswith("mrch_"):
            raise InvalidRequestError("merchant_id malformed", code="merchant_id_invalid")
        origins = tuple(allowed_origins or ())
        if not origins or any(not origin.startswith("https://") for origin in origins):
            raise InvalidRequestError(
                "allowed_origins must be https origins", code="allowed_origins_invalid"
            )
        now = self._clock.now()
        record = IntakeSessionRecord(
            vint_id=make_vault_id(
                "vint",
                {
                    "payment_intent_id_or_customer_id": payment_intent_id or customer_id,
                    "merchant_id": merchant_id,
                    "nonce_hex": self.keys.hsm_random_hex(16),
                },
            ),
            merchant_id=merchant_id,
            purpose=purpose,
            payment_intent_id=payment_intent_id,
            customer_id=customer_id,
            allowed_origins=origins,
            cvv_required=cvv_required,
            status=IntakeSessionStatus.CREATED,
            failed_attempts=0,
            created_at=now,
            expires_at=now + INTAKE_TTL,
        )
        self._sessions.save(record)
        self.audit.append(
            "intake_session_created",
            {"vint_id": record.vint_id, "merchant_id": merchant_id, "purpose": purpose},
            actor=caller_san,
        )
        return {
            "intake_session_id": record.vint_id,
            "intake_url": f"https://fields.bdpay.example/v1/card-intake/{record.vint_id}",
            "expires_at": rfc3339(record.expires_at),
            "single_use": True,
        }

    def void_intake_session(self, vint_id: str, *, reason: str, actor: str) -> None:
        record = self._sessions.get(vint_id)
        if record is None:
            raise NotFoundError("intake session not found", code="intake_session_not_found")
        if record.status is not IntakeSessionStatus.CREATED:
            raise ConflictError("intake session already terminal", code="intake_session_terminal")
        record.status = IntakeSessionStatus.VOIDED
        record.terminal_at = self._clock.now()
        self._sessions.save(record)
        self.sad.purge(vint_id)
        self.audit.append(
            "intake_session_voided", {"vint_id": vint_id, "reason": reason}, actor=actor
        )

    def _load_intake_session(self, vint_id: str) -> IntakeSessionRecord:
        record = self._sessions.get(vint_id)
        if record is None:
            # Expired and never-existed are deliberately indistinguishable.
            raise NotFoundError("intake session not found", code="intake_session_not_found")
        if record.status is IntakeSessionStatus.CREATED and self._clock.now() >= record.expires_at:
            self._expire_session(record)
        if record.status is IntakeSessionStatus.EXPIRED:
            raise NotFoundError("intake session not found", code="intake_session_not_found")
        if record.status is IntakeSessionStatus.CONSUMED:
            raise ConflictError("intake session consumed", code="intake_session_consumed")
        if record.status is IntakeSessionStatus.VOIDED:
            raise ConflictError("intake session voided", code="intake_session_voided")
        return record

    def _expire_session(self, record: IntakeSessionRecord) -> None:
        record.status = IntakeSessionStatus.EXPIRED
        record.terminal_at = self._clock.now()
        self._sessions.save(record)
        self.sad.purge(record.vint_id)
        self.audit.append(
            "intake_session_expired", {"vint_id": record.vint_id}, actor="vault-sweep"
        )
        self.emit_event(
            "card_intake_session.expired",
            "CardIntakeSession",
            record.vint_id,
            {
                "intake_session_id": record.vint_id,
                "merchant_id": record.merchant_id,
                "payment_intent_id": record.payment_intent_id,
            },
        )

    def _intake_validation_failure(
        self, session: IntakeSessionRecord, code: str, message: str
    ) -> InvalidRequestError:
        session.failed_attempts += 1
        if session.failed_attempts > MAX_VALIDATION_FAILURES:
            session.status = IntakeSessionStatus.VOIDED
            session.terminal_at = self._clock.now()
            self.sad.purge(session.vint_id)
            self.audit.append(
                "intake_session_voided",
                {"vint_id": session.vint_id, "reason": "attempts"},
                actor="vault-intake",
            )
        self._sessions.save(session)
        return InvalidRequestError(message, code=code)

    # ----------------------------------------------------------------- intake

    def card_intake(self, vint_id: str, fields: dict, *, client_ip: str) -> dict:
        if not self._rates.hit("ip", client_ip, self._ip_limit):
            raise RateLimitError("intake rate limited", code="intake_rate_limited")
        session = self._load_intake_session(vint_id)
        if not self._rates.hit("merchant", session.merchant_id, self._merchant_limit):
            raise RateLimitError("intake rate limited", code="intake_rate_limited")

        pan = normalize_digits(str(fields.get("pan", "")))
        expiry_month = normalize_digits(str(fields.get("expiry_month", "")))
        expiry_year = normalize_digits(str(fields.get("expiry_year", "")))
        cvv_raw = fields.get("cvv")
        cvv = normalize_digits(str(cvv_raw)) if cvv_raw is not None else None
        holder_name_raw = fields.get("cardholder_name")
        holder_name = (
            normalize_holder_name(str(holder_name_raw))[:96]
            if holder_name_raw is not None
            else None
        )

        if not pan.isdigit() or not (13 <= len(pan) <= 19):
            raise self._intake_validation_failure(
                session, "pan_length_invalid", "card number length invalid"
            )
        if not luhn_valid(pan):
            raise self._intake_validation_failure(
                session, "pan_luhn_failed", "card number check digit invalid"
            )
        if (
            len(expiry_month) != 2
            or not expiry_month.isdigit()
            or not (1 <= int(expiry_month) <= 12)
            or len(expiry_year) != 4
            or not expiry_year.isdigit()
            or expiry_in_past(expiry_month, expiry_year, self._clock.now())
        ):
            raise self._intake_validation_failure(session, "card_expired", "card expiry invalid")
        if cvv is None and session.cvv_required:
            raise self._intake_validation_failure(
                session, "cvv_format_invalid", "cvv required for this session"
            )
        if cvv is not None and (not cvv.isdigit() or not (3 <= len(cvv) <= 4)):
            raise self._intake_validation_failure(
                session, "cvv_format_invalid", "cvv format invalid"
            )

        record = self._tokenize(
            pan,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            holder_name=holder_name,
            merchant_id=session.merchant_id,
            customer_id=session.customer_id,
            origin="intake",
            actor=f"intake:{session.vint_id}",
        )
        if cvv is not None:
            self.sad.put(session.vint_id, cvv, session_expires_at=session.expires_at)
        session.status = IntakeSessionStatus.CONSUMED
        session.terminal_at = self._clock.now()
        session.token = record.token
        self._sessions.save(session)
        self.audit.append(
            "intake_session_consumed",
            {"vint_id": session.vint_id, "token": record.token},
            actor=f"intake:{session.vint_id}",
        )
        return {
            "token": record.token,
            "bin": record.bin,
            "last4": record.last4,
            "scheme": record.scheme,
            "expiry_month": record.expiry_month,
            "expiry_year": record.expiry_year,
            "cardholder_name_present": holder_name is not None and holder_name != "",
            "token_status": record.status.value,
        }

    def import_token(self, *, caller_san: str, body: dict) -> dict:
        self.authorize(caller_san, "token.import")
        if not self._import_window_open:
            raise AuthorizationError(
                "bulk import window is closed", code="operation_disabled"
            )
        if body.get("cvv") is not None:
            # Importing SAD is a PCI violation, full stop.
            raise InvalidRequestError(
                "SAD must never be imported", code="sad_import_forbidden"
            )
        merchant_id = str(body.get("merchant_id", ""))
        if not merchant_id.startswith("mrch_"):
            raise InvalidRequestError(
                "import requires the owning merchant_id", code="merchant_id_invalid"
            )
        pan = normalize_digits(str(body.get("pan", "")))
        expiry_month = normalize_digits(str(body.get("expiry_month", "")))
        expiry_year = normalize_digits(str(body.get("expiry_year", "")))
        if not pan.isdigit() or not (13 <= len(pan) <= 19) or not luhn_valid(pan):
            raise InvalidRequestError("card number invalid", code="pan_luhn_failed")
        record = self._tokenize(
            pan,
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            holder_name=None,
            merchant_id=merchant_id,
            customer_id=None,
            origin="import",
            actor=caller_san,
        )
        self.audit.append(
            "token_imported",
            {"token": record.token, "import_batch_ref": str(body.get("import_batch_ref", ""))},
            actor=caller_san,
        )
        return {
            "token": record.token,
            "bin": record.bin,
            "last4": record.last4,
            "scheme": record.scheme,
            "expiry_month": record.expiry_month,
            "expiry_year": record.expiry_year,
            "cardholder_name_present": False,
            "token_status": record.status.value,
        }

    def _tokenize(
        self,
        pan: str,
        *,
        expiry_month: str,
        expiry_year: str,
        holder_name: str | None,
        merchant_id: str,
        customer_id: str | None,
        origin: str,
        actor: str,
    ) -> CardTokenRecord:
        pepper, pepper_kid = self.keys.active_pepper()
        pan_hmac = hmac_sha256_hex(pepper, pan.encode("ascii"))
        token = make_vault_id("tok", {"pan_hmac": pan_hmac, "pepper_kid": pepper_kid})
        bin_value, last4 = truncate_pan(pan)
        existing = self._tokens.by_pan_hmac(pan_hmac, pepper_kid)
        now = self._clock.now()
        if existing is not None:
            if existing.bin != bin_value or existing.last4 != last4:
                self.audit.append(
                    "token_collision_critical",
                    {"token": existing.token},
                    actor=actor,
                )
                raise InternalError(
                    "token derivation mismatch against stored truncation",
                    code="vault_unavailable",
                )
            existing.last_seen_at = now
            self._tokens.save(existing)
            return existing
        dek, dek_kid = self.keys.active_dek()
        aad = pan_aad(token)
        nonce = self._hsm.random(GCM_NONCE_BYTES)
        pan_buffer = bytearray(pan.encode("ascii"))
        try:
            ciphertext = aes_gcm_encrypt(dek, bytes(pan_buffer), aad, nonce)
        finally:
            zeroize(pan_buffer)
        holder_ct = holder_nonce = None
        holder_dek = None
        if holder_name:
            holder_nonce = self._hsm.random(GCM_NONCE_BYTES)
            holder_ct = aes_gcm_encrypt(
                dek, holder_name.encode("utf-8"), b"holder-name-v1", holder_nonce
            )
            holder_dek = dek_kid
        record = CardTokenRecord(
            token=token,
            pan_hmac=pan_hmac,
            pepper_kid=pepper_kid,
            bin=bin_value,
            last4=last4,
            scheme=detect_scheme(pan),
            expiry_month=expiry_month,
            expiry_year=expiry_year,
            status=TokenStatus.PENDING_FIRST_AUTH,
            origin=origin,
            merchant_id=merchant_id,
            customer_id=customer_id,
            holder_name_ct=holder_ct,
            holder_name_nonce=holder_nonce,
            holder_name_dek=holder_dek,
            created_at=now,
            last_seen_at=now,
        )
        # One transaction in the DDL: encrypted_pans + card_tokens + audit + outbox.
        self._tokens.save(record)
        self._pans.save(
            EncryptedPanRecord(
                epan_id=make_vault_id("epan", {"token": token, "dek_kid": dek_kid}),
                token=token,
                dek_key_id=dek_kid,
                nonce=nonce,
                pan_ciphertext=ciphertext,
                aad=aad.decode("utf-8"),
                created_at=now,
            )
        )
        self.audit.append("token_created", {"token": token, "origin": origin}, actor=actor)
        self.emit_event(
            "card_token.created",
            "CardToken",
            token,
            {
                "token": token,
                "bin": bin_value,
                "last4": last4,
                "scheme": record.scheme,
                "expiry_month": expiry_month,
                "expiry_year": expiry_year,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "origin": origin,
            },
        )
        return record

    # ----------------------------------------------------------------- tokens

    def _resolve_token(self, token: str) -> CardTokenRecord | None:
        record = self._tokens.get(token)
        if record is None:
            alias = self.keys._aliases.resolve(token)  # noqa: SLF001 - same package
            if alias is not None:
                record = self._tokens.get(alias.new_token)
        return record

    def token_metadata(self, *, caller_san: str, token: str) -> dict:
        self.authorize(caller_san, "token.read_metadata")
        record = self._resolve_token(token)
        if record is None:
            raise NotFoundError("token not found", code="token_not_found")
        return {
            "token": record.token,
            "status": record.status.value,
            "bin": record.bin,
            "last4": record.last4,
            "scheme": record.scheme,
            "expiry_month": record.expiry_month,
            "expiry_year": record.expiry_year,
            "created_at": rfc3339(record.created_at),
            "last_used_at": rfc3339(record.last_used_at) if record.last_used_at else None,
        }

    def retire_token(
        self,
        *,
        caller_san: str,
        token: str,
        reason: str,
        approval_request_id: str | None = None,
    ) -> dict:
        self.authorize(caller_san, "token.retire")
        if reason not in RETIRE_REASONS:
            raise InvalidRequestError("unknown retire reason", code="retire_reason_invalid")
        record = self._resolve_token(token)
        if record is None:
            raise NotFoundError("token not found", code="token_not_found")
        if record.status in TOKEN_TERMINAL:
            raise ConflictError("token already terminal", code="token_already_terminal")
        now = self._clock.now()
        if reason == "compromise":
            # FSM 1: compromise via the retire API SUSPENDS (four-eyes guarded).
            if not approval_request_id:
                raise AuthorizationError(
                    "suspension for compromise requires a four-eyes approval reference",
                    code="approval_required",
                )
            if record.status is TokenStatus.SUSPENDED:
                raise ConflictError("token already suspended", code="token_already_suspended")
            record.status = TokenStatus.SUSPENDED
            self._tokens.save(record)
            self.audit.append(
                "token_suspended",
                {"token": record.token, "approval_request_id": approval_request_id},
                actor=caller_san,
            )
            self.emit_event(
                "card_token.suspended",
                "CardToken",
                record.token,
                {
                    "token": record.token,
                    "reason": reason,
                    "approval_request_id": approval_request_id,
                },
            )
            return {"token": record.token, "status": record.status.value}
        record.status = TokenStatus.RETIRED
        record.terminal_at = now
        record.retire_reason = reason
        self._tokens.save(record)
        destroy_after = now + CIPHERTEXT_DESTROY_HOLD
        epan = self._pans.get_by_token(record.token)
        if epan is not None:
            epan.destroy_after = destroy_after
            self._pans.save(epan)
        self.audit.append(
            "token_retired", {"token": record.token, "reason": reason}, actor=caller_san
        )
        self.emit_event(
            "card_token.retired",
            "CardToken",
            record.token,
            {
                "token": record.token,
                "reason": reason,
                "ciphertext_destroy_after": rfc3339(destroy_after),
            },
        )
        return {
            "token": record.token,
            "status": record.status.value,
            "ciphertext_destroy_after": rfc3339(destroy_after),
        }

    def reinstate_token(self, *, token: str, approval_request_id: str, actor: str) -> dict:
        record = self._resolve_token(token)
        if record is None:
            raise NotFoundError("token not found", code="token_not_found")
        if record.status is not TokenStatus.SUSPENDED:
            self.audit.append(
                "transition_denied",
                {"entity": "CardToken", "token": token, "trigger": "reinstate"},
                actor=actor,
            )
            raise ConflictError(
                "reinstate is only legal from SUSPENDED", code="token_not_suspended"
            )
        if expiry_in_past(record.expiry_month, record.expiry_year, self._clock.now()):
            raise ConflictError("token past card expiry", code="token_past_expiry")
        record.status = TokenStatus.ACTIVE
        self._tokens.save(record)
        self.audit.append(
            "token_reinstated",
            {"token": record.token, "approval_request_id": approval_request_id},
            actor=actor,
        )
        self.emit_event(
            "card_token.reinstated",
            "CardToken",
            record.token,
            {
                "token": record.token,
                "reason": "reinstate",
                "approval_request_id": approval_request_id,
            },
        )
        return {"token": record.token, "status": record.status.value}

    # ------------------------------------------------------- detokenize-egress

    def _detok_deny(
        self,
        *,
        code: str,
        error: Exception,
        instruction_id: str | None,
        connector_ref: str | None,
        token: str | None,
        caller_san: str,
        grant_hash: str,
        acquirer_host: str | None = None,
        egress_at: datetime | None = None,
    ) -> Exception:
        """Every denial writes detokenization_audit + emits detokenization.denied."""
        self._detok.append(
            DetokenizationRecord(
                instruction_id=instruction_id or "",
                connector_ref=connector_ref or "",
                token=token or "",
                caller_san=caller_san,
                decision=DetokDecision.DENIED,
                denial_code=code,
                grant_hash=grant_hash,
                acquirer_host=acquirer_host,
                egress_at=egress_at,
                created_at=self._clock.now(),
            )
        )
        self.emit_event(
            "detokenization.denied",
            "DetokenizationRecord",
            connector_ref or "",
            {
                "instruction_id": instruction_id,
                "connector_ref": connector_ref,
                "token": token,
                "caller_san": caller_san,
                "denial_code": code,
            },
        )
        return error

    async def detokenize_egress(self, *, caller_san: str, request: dict) -> dict:
        instruction_id = str(request.get("instruction_id", ""))
        connector_ref = str(request.get("connector_ref", ""))
        token = str(request.get("token", ""))
        intake_session_id = request.get("intake_session_id")
        grant = request.get("egress_grant") or {}
        grant_payload = grant.get("payload") or {}
        egress = request.get("egress") or {}
        grant_hash = sha256_canonical(grant_payload)
        url = str(egress.get("url", ""))
        url_host = host_of(url) if url else ""

        def deny(code: str, error: Exception, **extra) -> Exception:
            return self._detok_deny(
                code=code,
                error=error,
                instruction_id=instruction_id,
                connector_ref=connector_ref,
                token=token,
                caller_san=caller_san,
                grant_hash=grant_hash,
                **extra,
            )

        # Guard 1 — caller identity is exactly the card vault-proxy.
        if caller_san != EGRESS_CALLER_SAN:
            raise deny(
                "vault_caller_not_allowlisted", authorization_denied("detokenize.egress")
            )
        self.authorize(caller_san, "detokenize.egress")

        # Guard 2 — grant signature, freshness window, field match.
        now = self._clock.now()
        try:
            issued_at = datetime.fromisoformat(
                str(grant_payload.get("issued_at", "")).replace("Z", "+00:00")
            )
            expires_at = datetime.fromisoformat(
                str(grant_payload.get("expires_at", "")).replace("Z", "+00:00")
            )
        except ValueError:
            issued_at = expires_at = None  # type: ignore[assignment]
        grant_ok = (
            isinstance(grant.get("sig"), str)
            and issued_at is not None
            and expires_at is not None
            and issued_at.tzinfo is not None
            and expires_at.tzinfo is not None
            and issued_at <= now  # a future-dated grant is not yet valid
            and now <= expires_at
            and (expires_at - issued_at) <= GRANT_MAX_WINDOW
            and grant_payload.get("instruction_id") == instruction_id
            and grant_payload.get("connector_ref") == connector_ref
            and grant_payload.get("token") == token
            and self.keys.verify_egress_grant(grant_payload, grant["sig"])
        )
        if not grant_ok:
            raise deny(
                "egress_grant_invalid",
                AuthorizationError("egress grant rejected", code="egress_grant_invalid"),
            )

        # Guard 3 — connector_ref single-use with 24 h idempotent replay.
        prior = self._detok.allowed_by_connector_ref(connector_ref)
        if prior is not None:
            if prior.grant_hash != grant_hash:
                raise deny(
                    "idempotency_conflict",
                    IdempotencyConflictError(
                        "connector_ref replayed with different parameters",
                        code="idempotency_conflict",
                    ),
                )
            if prior.stored_response is not None and (
                prior.replay_expires_at is None or now < prior.replay_expires_at
            ):
                self.audit.append(
                    "detokenize_replay_served", {"connector_ref": connector_ref}, actor=caller_san
                )
                return dict(prior.stored_response)
            raise deny(
                "egress_replay_window_expired",
                ConflictError(
                    "connector_ref already used and replay window has closed",
                    code="egress_replay_window_expired",
                ),
            )

        # Guard 4 — egress host equals the grant host and is allowlisted.
        if (
            not url_host
            or url_host != grant_payload.get("acquirer_host")
            or url_host not in self._acquirer_hosts
        ):
            raise deny(
                "egress_host_not_allowlisted",
                AuthorizationError(
                    "egress host is not allowlisted", code="egress_host_not_allowlisted"
                ),
                acquirer_host=url_host or None,
            )

        # Guard 5 — token usable.
        record = self._resolve_token(token)
        if record is None or record.status not in (
            TokenStatus.PENDING_FIRST_AUTH,
            TokenStatus.ACTIVE,
        ):
            raise deny(
                "token_not_usable",
                ConflictError("token is not usable for egress", code="token_not_usable"),
                acquirer_host=url_host,
            )

        # Guard 6 — slot grammar; CVV slot requires a live SAD entry.
        template = str(egress.get("body_template", ""))
        try:
            slots = template_slots(template)  # unknown slots are a denial, not a plain 400
        except InvalidRequestError as exc:
            raise deny("placeholder_context_invalid", exc, acquirer_host=url_host) from exc
        needs_cvv = "{{VAULT:CVV}}" in slots
        if needs_cvv and (
            not intake_session_id or not self.sad.alive(str(intake_session_id))
        ):
            raise deny(
                "cvv_not_available",
                LimitExceededError(
                    "cvv not available for this egress", code="cvv_not_available"
                ),
                acquirer_host=url_host,
            )

        # Guard 7 — timeout must undercut the card connector's 30 s budget.
        timeout_ms = int(egress.get("timeout_ms", MAX_EGRESS_TIMEOUT_MS))
        if timeout_ms <= 0 or timeout_ms > MAX_EGRESS_TIMEOUT_MS:
            raise deny(
                "egress_timeout_budget_exceeded",
                InvalidRequestError(
                    "timeout_ms must undercut the connector budget",
                    code="egress_timeout_budget_exceeded",
                ),
                acquirer_host=url_host,
            )

        # Execution — decrypt, substitute in memory, call, zeroize.
        epan = self._pans.get_by_token(record.token)
        if epan is None:
            raise deny(
                "token_not_usable",
                ConflictError("ciphertext destroyed", code="token_not_usable"),
                acquirer_host=url_host,
            )
        dek = self.keys.key_material(epan.dek_key_id)
        pan_buffer = bytearray(
            aes_gcm_decrypt(dek, epan.pan_ciphertext, pan_aad(record.token), epan.nonce)
        )
        sad_consumed = False
        pan = ""
        try:
            pan = pan_buffer.decode("ascii")
            values: dict[str, str] = {
                "{{VAULT:PAN}}": pan,
                "{{VAULT:EXPIRY_MMYY}}": record.expiry_month + record.expiry_year[2:],
                "{{VAULT:EXPIRY_MMYYYY}}": record.expiry_month + record.expiry_year,
            }
            if "{{VAULT:HOLDER_NAME}}" in slots:
                values["{{VAULT:HOLDER_NAME}}"] = self._decrypt_holder_name(record)
            if needs_cvv:
                cvv_value = self.sad.consume(str(intake_session_id))
                if cvv_value is None:
                    raise deny(
                        "cvv_not_available",
                        LimitExceededError(
                            "cvv not available for this egress", code="cvv_not_available"
                        ),
                        acquirer_host=url_host,
                    )
                values["{{VAULT:CVV}}"] = cvv_value
                sad_consumed = True
            try:
                body = substitute_template(template, values)
            except InvalidRequestError as exc:
                raise deny("placeholder_context_invalid", exc, acquirer_host=url_host) from exc
            egress_at = self._clock.now()
            try:
                response = await self._executor.execute(
                    method=str(egress.get("method", "POST")),
                    url=url,
                    headers=dict(egress.get("headers") or {}),
                    body=body,
                    timeout_ms=timeout_ms,
                )
            except EgressTimeoutError as exc:
                raise deny(
                    "acquirer_timeout",
                    ConnectorError("acquirer call timed out", code="acquirer_timeout"),
                    acquirer_host=url_host,
                    egress_at=egress_at,
                ) from exc
            except EgressUnreachableError as exc:
                raise deny(
                    "acquirer_unreachable",
                    ConnectorError("acquirer unreachable", code="acquirer_unreachable"),
                    acquirer_host=url_host,
                ) from exc
            finally:
                body = ""
            responded_at = self._clock.now()
            raw_hash = raw_response_hash(response.status_code, response.headers, response.body)
            stripped_headers, stripped_body = strip_response(
                response.status_code, response.headers, response.body, pan=pan, token=record.token
            )
        finally:
            zeroize(pan_buffer)
            pan = ""

        result = {
            "instruction_id": instruction_id,
            "connector_ref": connector_ref,
            "acquirer_status_code": response.status_code,
            "acquirer_headers": stripped_headers,
            "acquirer_body_stripped": stripped_body,
            "raw_response_hash": f"sha256:{raw_hash}",
            "egress_at": rfc3339(egress_at),
            "responded_at": rfc3339(responded_at),
            "sad_consumed": sad_consumed,
        }
        self._detok.append(
            DetokenizationRecord(
                instruction_id=instruction_id,
                connector_ref=connector_ref,
                token=record.token,
                caller_san=caller_san,
                decision=DetokDecision.ALLOWED,
                grant_hash=grant_hash,
                acquirer_host=url_host,
                sad_consumed=sad_consumed,
                egress_at=egress_at,
                responded_at=responded_at,
                acquirer_status=response.status_code,
                raw_response_hash=f"sha256:{raw_hash}",
                stored_response=dict(result),
                replay_expires_at=egress_at + REPLAY_WINDOW,
                created_at=self._clock.now(),
            )
        )
        if 200 <= response.status_code < 300:
            record.last_used_at = responded_at
            if record.status is TokenStatus.PENDING_FIRST_AUTH:
                record.status = TokenStatus.ACTIVE
                record.first_used_at = responded_at
                self.audit.append(
                    "token_first_use", {"token": record.token}, actor=caller_san
                )
            self._tokens.save(record)
        self.audit.append(
            "detokenization_executed",
            {"connector_ref": connector_ref, "token": record.token, "host": url_host},
            actor=caller_san,
        )
        self.emit_event(
            "detokenization.executed",
            "DetokenizationRecord",
            connector_ref,
            {
                "instruction_id": instruction_id,
                "connector_ref": connector_ref,
                "token": record.token,
                "acquirer_host": url_host,
                "acquirer_status": response.status_code,
                "raw_response_hash": f"sha256:{raw_hash}",
                "sad_consumed": sad_consumed,
            },
        )
        return result

    def _decrypt_holder_name(self, record: CardTokenRecord) -> str:
        if record.holder_name_ct is None or record.holder_name_nonce is None:
            return ""
        dek = self.keys.key_material(record.holder_name_dek or "")
        return aes_gcm_decrypt(
            dek, record.holder_name_ct, b"holder-name-v1", record.holder_name_nonce
        ).decode("utf-8")

    # ------------------------------------------------------------------ sweep

    def maintenance_sweep(self, *, actor: str = "vault-sweep") -> dict:
        """Plane C idempotent batch (spec/14): expire sessions, expire/retire
        tokens, destroy due ciphertexts, advance runs, clear replay bodies."""
        now = self._clock.now()
        counts = {
            "sessions_expired": 0,
            "tokens_retired_never_used": 0,
            "tokens_expired": 0,
            "ciphertexts_destroyed": 0,
            "replay_responses_cleared": 0,
            "idempotency_purged": 0,
            "sad_purged": 0,
            "reencryption_rows": 0,
            "keys_rotation_due": len(self.keys.keys_rotation_due()),
        }
        for session in self._sessions.all():
            if session.status is IntakeSessionStatus.CREATED and now >= session.expires_at:
                self._expire_session(session)
                counts["sessions_expired"] += 1
        for token in self._tokens.all():
            if token.status in TOKEN_TERMINAL:
                continue
            grace_year, grace_month = int(token.expiry_year), int(token.expiry_month)
            grace_month += _EXPIRY_GRACE_MONTHS
            if grace_month > 12:
                grace_month -= 12
                grace_year += 1
            if (now.year, now.month) > (grace_year, grace_month):
                token.status = TokenStatus.EXPIRED
                token.terminal_at = now
                self._tokens.save(token)
                self._set_destroy_after(token.token, now)
                self.audit.append("token_expired", {"token": token.token}, actor=actor)
                self.emit_event(
                    "card_token.expired",
                    "CardToken",
                    token.token,
                    {
                        "token": token.token,
                        "expiry_month": token.expiry_month,
                        "expiry_year": token.expiry_year,
                    },
                )
                counts["tokens_expired"] += 1
                continue
            if (
                token.status is TokenStatus.PENDING_FIRST_AUTH
                and token.first_used_at is None
                and now >= token.created_at + NEVER_USED_RETIRE
            ):
                token.status = TokenStatus.RETIRED
                token.terminal_at = now
                token.retire_reason = "never_used"
                self._tokens.save(token)
                self._set_destroy_after(token.token, now)
                self.audit.append(
                    "token_retired", {"token": token.token, "reason": "never_used"}, actor=actor
                )
                self.emit_event(
                    "card_token.retired",
                    "CardToken",
                    token.token,
                    {
                        "token": token.token,
                        "reason": "never_used",
                        "ciphertext_destroy_after": rfc3339(now + CIPHERTEXT_DESTROY_HOLD),
                    },
                )
                counts["tokens_retired_never_used"] += 1
        for epan in self._pans.all():
            if epan.destroy_after is not None and now >= epan.destroy_after:
                self._pans.delete_by_token(epan.token)
                self.audit.append(
                    "ciphertext_destroyed", {"token": epan.token}, actor=actor
                )
                counts["ciphertexts_destroyed"] += 1
        counts["replay_responses_cleared"] = self._detok.clear_expired_responses(now)
        counts["idempotency_purged"] = self.idempotency.purge_older_than(now - timedelta(hours=24))
        counts["sad_purged"] = self.sad.purge_expired()
        for run in self.keys._runs.all():  # noqa: SLF001 - same package
            if run.status in (ReencryptionStatus.PENDING, ReencryptionStatus.RUNNING):
                before = run.done_rows
                self.keys.advance_reencryption_run(run.vrun_id, batch_size=run.rate_cap_rps)
                counts["reencryption_rows"] += run.done_rows - before
        self.audit.append("maintenance_sweep", counts, actor=actor)
        return counts

    def _set_destroy_after(self, token: str, now: datetime) -> None:
        epan = self._pans.get_by_token(token)
        if epan is not None:
            epan.destroy_after = now + CIPHERTEXT_DESTROY_HOLD
            self._pans.save(epan)

    # ------------------------------------------------------------------ health

    def health(self) -> dict:
        hsm_ok = self._hsm.health_check()
        dek_age = self.keys.dek_cache_age_seconds()
        dek_warm = dek_age is not None and dek_age < 900
        if hsm_ok:
            status = "ok"
        elif dek_warm:
            status = "degraded"  # detokenize-only mode (F1/F2)
        else:
            status = "down"
        chain_ok = not self.audit.write_refused
        if not chain_ok and status == "ok":
            status = "degraded"
        return {
            "status": status,
            "checks": {
                "vault_db": "ok",
                "hsm": "ok" if hsm_ok else "breaker_open",
                "openbao": "ok" if hsm_ok else "unreachable",
                "dek_cache": {
                    "status": "warm" if dek_warm else "cold",
                    "age_seconds": dek_age,
                    "ttl_seconds": 900,
                },
                "audit_chain": (
                    f"verified_at {rfc3339(self.audit.last_verified_at)}"
                    if chain_ok and self.audit.last_verified_at is not None
                    else ("unverified" if chain_ok else "verify_failed")
                ),
                "outbox_lag_events": len(self._outbox.pending()),
            },
        }
