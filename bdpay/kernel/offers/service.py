"""OfferService — merchant offer lifecycle, discovery, sweeps (spec/18).

The ``offer-engine`` logical sub-service (G4 grant). Every FSM transition
goes through :data:`~bdpay.kernel.offers.states.OFFER_TABLE` (refusal-first),
writes one audit row, and produces its spec/18 event on the ``offer.events``
topic via the outbox.

Edit-by-versioning (spec/18 §State machines, Edits): economic fields
(``percent_bps``, caps, windows, validity) change ONLY through a new
immutable version row (``version = n+1``); in-flight reservations pin the
version they reserved against. Title/description fields mutate in place.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.offers.copy_bn import REASON_COPY
from bdpay.kernel.offers.eligibility import (
    CounterReading,
    dhaka_date_str,
    evaluate_eligibility,
)
from bdpay.kernel.offers.ids_ext import make_offer_id
from bdpay.kernel.offers.models import (
    PERCENT_BPS_CEILING,
    PERCENT_BPS_FLOOR,
    OfferRecord,
    OfferVersionRecord,
    OfferWindow,
    validate_windows,
)
from bdpay.kernel.offers.repository import OfferStore
from bdpay.kernel.offers.states import OFFER_TABLE
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthorizationError,
    ConflictError,
    IdempotencyConflictError,
    InvalidRequestError,
    NotFoundError,
)
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = ["OFFER_EVENTS_TOPIC", "PRODUCER", "OfferMerchantPort", "OfferService"]

PRODUCER = "offer-engine@1"

#: G4-granted topic; registered in the platform TOPICS registry (errata S18-E9).
OFFER_EVENTS_TOPIC = "offer.events"

#: Economic fields that are edit-by-versioning only.
_ECONOMIC_FIELDS = (
    "percent_bps",
    "windows",
    "valid_from",
    "valid_until",
    "min_spend_minor",
    "max_discount_minor",
    "cap_per_day",
    "cap_total",
    "cap_per_customer",
    "allowed_methods",
    "commission_bps",
    "subsidy_bps",
)

#: Title/description fields that mutate in place.
_MUTABLE_FIELDS = ("title", "title_bn")


@runtime_checkable
class OfferMerchantPort(Protocol):
    """Merchant facts the offer engine needs (spec/08 owns the truth)."""

    def is_active(self, merchant_id: str) -> bool: ...

    def supported_methods(self, merchant_id: str) -> frozenset[str]: ...


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OfferService:
    """spec/18 merchant-facing offer management + consumer discovery."""

    def __init__(
        self,
        *,
        store: OfferStore,
        merchants: OfferMerchantPort,
        audit: AuditPort,
        outbox: OutboxPort,
        clock: Clock,
        max_percent_bps: int = PERCENT_BPS_CEILING,
    ) -> None:
        if not PERCENT_BPS_FLOOR <= max_percent_bps <= PERCENT_BPS_CEILING:
            raise InvalidRequestError(
                "offer.max_percent_bps config must stay within the DDL bounds "
                f"[{PERCENT_BPS_FLOOR}, {PERCENT_BPS_CEILING}]"
            )
        self._store = store
        self._merchants = merchants
        self._audit = audit
        self._outbox = outbox
        self._clock = clock
        self._max_percent_bps = max_percent_bps

    # ------------------------------------------------------------------
    # create
    # ------------------------------------------------------------------

    def create_offer(
        self,
        merchant_id: str,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        commission_bps: int | None = None,
        subsidy_bps: int | None = None,
        conn: Any | None = None,
    ) -> dict[str, object]:
        """POST /v1/offers (merchant). Validation per spec/18 §API surface.

        ``commission_bps`` / ``subsidy_bps`` are NOT merchant-settable: they
        arrive only through the operator configuration path (the spec/18
        request schema carries neither; subsidy is ApprovalRequest-gated).
        """
        if not merchant_id:
            raise InvalidRequestError("merchant_id is required")
        if not idempotency_key:
            raise InvalidRequestError(
                "Idempotency-Key is required for offer creation",
                code="idempotency_key_required",
            )
        if not self._merchants.is_active(merchant_id):
            raise AuthorizationError(
                "merchant must be ACTIVE to manage offers", code="merchant_not_active"
            )
        existing = self._store.find_offer_by_create_key(
            merchant_id, idempotency_key, conn=conn
        )
        if existing is not None:
            if existing.kind != payload.get("kind") or existing.title != payload.get("title"):
                raise IdempotencyConflictError(
                    "Idempotency-Key replayed with mismatched parameters"
                )
            return self.offer_view(existing)

        kind = payload.get("kind")
        if kind not in ("PERCENT_OFF", "BOGO"):
            raise InvalidRequestError(
                "kind must be PERCENT_OFF or BOGO", code="invalid_request"
            )
        if kind == "BOGO":
            # Decision D2 (binding): schema accepts BOGO, build refuses it.
            raise InvalidRequestError(
                "BOGO offers are not yet enabled (pilot is PERCENT_OFF-only)",
                code="bogo_not_yet_enabled",
            )
        percent_bps = payload.get("percent_bps")
        if isinstance(percent_bps, bool) or not isinstance(percent_bps, int):
            raise InvalidRequestError(
                "percent_bps is required for PERCENT_OFF", code="invalid_request"
            )
        if not PERCENT_BPS_FLOOR <= percent_bps <= self._max_percent_bps:
            raise InvalidRequestError(
                f"percent_bps must be within [{PERCENT_BPS_FLOOR}, {self._max_percent_bps}]",
                code="percent_bps_out_of_range",
            )
        windows_raw = payload.get("windows")
        if not isinstance(windows_raw, list | tuple):
            raise InvalidRequestError("windows must be a list", code="windows_required")
        windows = validate_windows(
            tuple(OfferWindow.from_payload(w) for w in windows_raw)
        )
        valid_from = _parse_dt(payload.get("valid_from"), "valid_from")
        valid_until = _parse_dt(payload.get("valid_until"), "valid_until")
        allowed_raw = payload.get("allowed_methods")
        if not isinstance(allowed_raw, list | tuple) or not allowed_raw:
            raise InvalidRequestError(
                "allowed_methods must be a non-empty list", code="invalid_request"
            )
        allowed_methods = tuple(str(m) for m in allowed_raw)
        supported = self._merchants.supported_methods(merchant_id)
        for method in allowed_methods:
            if method not in supported:
                raise InvalidRequestError(
                    "allowed_methods must be a subset of the merchant's active "
                    "connector methods",
                    code="method_not_supported_by_merchant",
                )
        now = self._clock.now()
        offer_id = make_offer_id(
            "off",
            {
                "merchant_id": merchant_id,
                "kind": kind,
                "created_idem_key": idempotency_key,
            },
        )
        record = OfferRecord(
            offer_id=offer_id,
            version=1,
            merchant_id=merchant_id,
            kind=kind,
            percent_bps=percent_bps,
            bogo_config=None,
            title=str(payload.get("title") or ""),
            title_bn=str(payload.get("title_bn") or ""),
            windows=windows,
            valid_from=valid_from,
            valid_until=valid_until,
            min_spend_minor=_opt_minor(payload.get("min_spend_minor"), "min_spend_minor") or 0,
            max_discount_minor=_opt_minor(
                payload.get("max_discount_minor"), "max_discount_minor"
            ),
            cap_per_day=_opt_cap(payload.get("cap_per_day"), "cap_per_day"),
            cap_total=_opt_cap(payload.get("cap_total"), "cap_total"),
            cap_per_customer=_opt_cap(payload.get("cap_per_customer"), "cap_per_customer"),
            allowed_methods=allowed_methods,
            cap_recredit_on_refund=bool(payload.get("cap_recredit_on_refund", True)),
            commission_bps=commission_bps,
            subsidy_bps=subsidy_bps,
            state="DRAFT",
            created_idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        self._store.insert_offer(record, conn=conn)
        self._store.insert_version(
            OfferVersionRecord.from_offer(record, created_at=now), conn=conn
        )
        self._audit_offer(record, from_state=None, to_state="DRAFT", conn=conn)
        return self.offer_view(record)

    # ------------------------------------------------------------------
    # lifecycle (sub-resource POSTs; idempotent per spec)
    # ------------------------------------------------------------------

    def activate_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> dict[str, object]:
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        if offer.state == "ACTIVE":
            return self.offer_view(offer)  # idempotent replay
        now = self._clock.now()
        # Guards (spec/18 row): merchant ACTIVE; now < valid_until; caps valid.
        if not self._merchants.is_active(merchant_id):
            raise AuthorizationError(
                "merchant must be ACTIVE to activate offers", code="merchant_not_active"
            )
        if now >= offer.valid_until:
            raise ConflictError(
                "offer validity has already ended", code="invalid_state_transition"
            )
        offer = self._advance(offer, "activate", event="offer.activated", conn=conn)
        return self.offer_view(offer)

    def pause_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> dict[str, object]:
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        if offer.state == "PAUSED":
            return self.offer_view(offer)
        offer = self._advance(offer, "pause", event="offer.paused", conn=conn)
        return self.offer_view(offer)

    def resume_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> dict[str, object]:
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        if offer.state == "ACTIVE":
            return self.offer_view(offer)
        now = self._clock.now()
        if now >= offer.valid_until:
            raise ConflictError(
                "offer validity has already ended", code="invalid_state_transition"
            )
        offer = self._advance(offer, "resume", event="offer.resumed", conn=conn)
        return self.offer_view(offer)

    def archive_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> dict[str, object]:
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        if offer.state == "ARCHIVED":
            return self.offer_view(offer)
        if offer.state != "DRAFT":
            outstanding = self._store.list_redemptions(
                offer_id, states=("RESERVED",), conn=conn
            )
            if outstanding:
                raise ConflictError(
                    "offer has RESERVED redemptions outstanding; archive refused",
                    code="reserved_redemptions_outstanding",
                )
        offer = self._advance(offer, "archive", event="offer.archived", conn=conn)
        return self.offer_view(offer)

    def handle_merchant_suspended(
        self, merchant_id: str, *, conn: Any | None = None
    ) -> int:
        """spec/18 FSM row: any non-terminal --merchant_suspended--> PAUSED."""
        moved = 0
        for offer in self._store.list_merchant_offers_in_states(
            merchant_id, ("DRAFT", "ACTIVE", "PAUSED", "EXHAUSTED"), conn=conn
        ):
            self._advance(
                offer,
                "merchant_suspended",
                event="offer.paused",
                event_payload={"reason": "merchant_state"},
                conn=conn,
            )
            moved += 1
        return moved

    # ------------------------------------------------------------------
    # edits (versioning) + cap raise
    # ------------------------------------------------------------------

    def edit_offer(
        self,
        offer_id: str,
        merchant_id: str,
        changes: Mapping[str, object],
        *,
        conn: Any | None = None,
    ) -> dict[str, object]:
        """Apply edits. Economic fields version; title fields mutate in place.

        ``EXHAUSTED --cap_raised--> ACTIVE`` happens exactly when the edit
        raises ``cap_total`` on an EXHAUSTED offer (spec/18 FSM row); the
        total counter's ``cap_limit`` is lifted in the same call.
        """
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        if OFFER_TABLE.is_terminal(offer.state):
            raise ConflictError(
                "terminal offers are immutable", code="invalid_state_transition"
            )
        unknown = set(changes) - set(_ECONOMIC_FIELDS) - set(_MUTABLE_FIELDS)
        if unknown:
            raise InvalidRequestError(
                f"unknown or immutable fields: {sorted(unknown)}", code="invalid_request"
            )
        now = self._clock.now()
        economic = {k: v for k, v in changes.items() if k in _ECONOMIC_FIELDS}
        mutable = {k: str(v) for k, v in changes.items() if k in _MUTABLE_FIELDS}

        updated = offer
        if mutable:
            updated = replace(updated, **mutable, updated_at=now)
        cap_raised = False
        if economic:
            if "windows" in economic:
                economic = dict(economic)
                economic["windows"] = validate_windows(
                    tuple(OfferWindow.from_payload(w) for w in economic["windows"])  # type: ignore[union-attr]
                )
            if "valid_from" in economic:
                economic["valid_from"] = _parse_dt(economic["valid_from"], "valid_from")
            if "valid_until" in economic:
                economic["valid_until"] = _parse_dt(economic["valid_until"], "valid_until")
            if "allowed_methods" in economic:
                economic["allowed_methods"] = tuple(
                    str(m) for m in economic["allowed_methods"]  # type: ignore[union-attr]
                )
            new_cap_total = economic.get("cap_total", offer.cap_total)
            cap_raised = (
                offer.state == "EXHAUSTED"
                and isinstance(new_cap_total, int)
                and offer.cap_total is not None
                and new_cap_total > offer.cap_total
            )
            if offer.state == "EXHAUSTED" and not cap_raised:
                raise ConflictError(
                    "an EXHAUSTED offer reactivates only by raising cap_total",
                    code="invalid_state_transition",
                )
            updated = replace(
                updated, **economic, version=offer.version + 1, updated_at=now
            )
            # Record-level validation re-runs in replace()'s __post_init__.
            self._store.insert_version(
                OfferVersionRecord.from_offer(updated, created_at=now), conn=conn
            )
            if isinstance(new_cap_total, int) and new_cap_total != offer.cap_total:
                counter_id = make_offer_id(
                    "octr", {"offer_id": offer.offer_id, "scope": "total", "scope_key": "ALL"}
                )
                if self._store.get_counter(counter_id, conn=conn) is not None:
                    self._store.set_counter_cap(
                        counter_id, new_cap_total, now=now, conn=conn
                    )
        self._store.update_offer(updated, conn=conn)
        self._audit_offer(updated, from_state=offer.state, to_state=updated.state, conn=conn)
        if cap_raised:
            updated = self._advance(updated, "cap_raised", event="offer.resumed", conn=conn)
        return self.offer_view(updated)

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def get_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None = None
    ) -> dict[str, object]:
        """GET /v1/offers/{offer_id} — includes the live counter readout."""
        offer = self._own_offer(offer_id, merchant_id, conn=conn)
        view = self.offer_view(offer)
        view["counters"] = self.counter_readout(offer, conn=conn)
        return view

    def list_offers(
        self,
        merchant_id: str,
        *,
        state: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
        conn: Any | None = None,
    ) -> tuple[list[dict[str, object]], str | None]:
        if state is not None and state not in OFFER_TABLE.states:
            raise InvalidRequestError(f"unknown offer state {state!r}", code="invalid_request")
        records, next_cursor = self._store.list_offers(
            merchant_id, state=state, limit=limit, cursor=cursor, conn=conn
        )
        return [self.offer_view(r) for r in records], next_cursor

    def counter_readout(
        self, offer: OfferRecord, *, conn: Any | None = None
    ) -> dict[str, int]:
        """``{redeemed_today, redeemed_total, reserved_now}`` (spec/18 read)."""
        now = self._clock.now()
        today = dhaka_date_str(now)
        redemptions = self._store.list_redemptions(offer.offer_id, conn=conn)
        redeemed = [r for r in redemptions if r.state in ("APPLIED", "SETTLED")]
        return {
            "redeemed_today": sum(
                1 for r in redeemed if dhaka_date_str(r.reserved_at) == today
            ),
            "redeemed_total": len(redeemed),
            "reserved_now": sum(1 for r in redemptions if r.state == "RESERVED"),
        }

    # ------------------------------------------------------------------
    # consumer discovery (diner-app)
    # ------------------------------------------------------------------

    def eligible_offers(
        self,
        merchant_id: str,
        *,
        at: datetime | None = None,
        method: str | None = None,
        example_amount_minor: int | None = None,
        conn: Any | None = None,
    ) -> list[dict[str, object]]:
        """GET /v1/offers/eligible — PII-free, customer-checks excluded.

        Per-customer caps are enforced only at reservation (spec/18) so this
        stays cacheable; day/total counter reads here are advisory fast-fail.
        """
        when = at if at is not None else self._clock.now()
        merchant_active = self._merchants.is_active(merchant_id)
        out: list[dict[str, object]] = []
        for offer in self._store.list_merchant_offers_in_states(
            merchant_id, ("ACTIVE",), conn=conn
        ):
            day_counter = self._advisory_counter(offer, "day", dhaka_date_str(when), conn=conn)
            total_counter = self._advisory_counter(offer, "total", "ALL", conn=conn)
            try:
                result = evaluate_eligibility(
                    offer,
                    merchant_active=merchant_active,
                    now_utc=when,
                    gross_amount_minor=None,
                    method=method,
                    day_counter=day_counter,
                    total_counter=total_counter,
                )
            except InvalidRequestError:
                continue  # fail closed: an unevaluable offer is not discoverable
            if not result.eligible:
                continue
            entry: dict[str, object] = {
                "offer_id": offer.offer_id,
                "kind": offer.kind,
                "title": offer.title,
                "title_bn": offer.title_bn,
                "percent_bps": offer.percent_bps,
                "windows": [w.to_payload() for w in offer.windows],
                "min_spend_minor": offer.min_spend_minor,
                "max_discount_minor": offer.max_discount_minor,
                "allowed_methods": list(offer.allowed_methods),
                "valid_until": _rfc3339(offer.valid_until),
            }
            if example_amount_minor is not None:
                try:
                    example = evaluate_eligibility(
                        offer,
                        merchant_active=merchant_active,
                        now_utc=when,
                        gross_amount_minor=example_amount_minor,
                        method=method,
                    )
                except InvalidRequestError:
                    example = None
                if example is not None and example.eligible:
                    entry["example_amount_minor"] = example_amount_minor
                    entry["example_discount_minor"] = example.discount_minor
            out.append(entry)
        return out

    def reason_copy(self) -> dict[str, dict[str, str]]:
        """Bilingual, PII-free ReasonCode copy table (consumer-displayable)."""
        return {
            code: {"message": en, "message_bn": bn}
            for code, (en, bn) in REASON_COPY.items()
        }

    # ------------------------------------------------------------------
    # scheduler tasks (spec/18 §Scheduler tasks)
    # ------------------------------------------------------------------

    def sweep_expired_offers(self, *, conn: Any | None = None) -> int:
        """``offer-expiry-sweep`` (60 s): ACTIVE/PAUSED past validity -> EXPIRED."""
        now = self._clock.now()
        moved = 0
        for offer in self._store.list_offers_past_validity(now=now, conn=conn):
            self._advance(offer, "ttl_expired", event="offer.expired", conn=conn)
            moved += 1
        return moved

    def prune_day_counters(
        self, *, retention_days: int = 400, conn: Any | None = None
    ) -> int:
        """``offer-day-counter-roll``: prune day counters older than 400 days."""
        cutoff = dhaka_date_str(self._clock.now() - timedelta(days=retention_days))
        return self._store.prune_day_counters(older_than_scope_key=cutoff, conn=conn)

    # ------------------------------------------------------------------
    # views + internals
    # ------------------------------------------------------------------

    def offer_view(self, offer: OfferRecord) -> dict[str, object]:
        return {
            "offer_id": offer.offer_id,
            "version": offer.version,
            "merchant_id": offer.merchant_id,
            "kind": offer.kind,
            "percent_bps": offer.percent_bps,
            "bogo_config": dict(offer.bogo_config) if offer.bogo_config else None,
            "title": offer.title,
            "title_bn": offer.title_bn,
            "windows": [w.to_payload() for w in offer.windows],
            "valid_from": _rfc3339(offer.valid_from),
            "valid_until": _rfc3339(offer.valid_until),
            "min_spend_minor": offer.min_spend_minor,
            "max_discount_minor": offer.max_discount_minor,
            "cap_per_day": offer.cap_per_day,
            "cap_total": offer.cap_total,
            "cap_per_customer": offer.cap_per_customer,
            "allowed_methods": list(offer.allowed_methods),
            "cap_recredit_on_refund": offer.cap_recredit_on_refund,
            "state": offer.state,
            "currency": offer.currency,
            "created_at": _rfc3339(offer.created_at),
            "updated_at": _rfc3339(offer.updated_at),
        }

    def _advisory_counter(
        self, offer: OfferRecord, scope: str, scope_key: str, *, conn: Any | None
    ) -> CounterReading | None:
        cap = offer.cap_per_day if scope == "day" else offer.cap_total
        if cap is None:
            return None
        counter_id = make_offer_id(
            "octr", {"offer_id": offer.offer_id, "scope": scope, "scope_key": scope_key}
        )
        record = self._store.get_counter(counter_id, conn=conn)
        if record is None:
            return CounterReading(used=0, cap_limit=cap)
        return CounterReading(used=record.used, cap_limit=record.cap_limit)

    def _own_offer(
        self, offer_id: str, merchant_id: str, *, conn: Any | None
    ) -> OfferRecord:
        offer = self._store.get_offer(offer_id, conn=conn)
        if offer is None:
            raise NotFoundError("offer not found", code="offer_not_found")
        if offer.merchant_id != merchant_id:
            # Merchant API keys see only their own offers (RLS posture).
            raise NotFoundError("offer not found", code="offer_not_found")
        return offer

    def _advance(
        self,
        offer: OfferRecord,
        trigger: str,
        *,
        event: str,
        event_payload: Mapping[str, object] | None = None,
        conn: Any | None = None,
    ) -> OfferRecord:
        rule = OFFER_TABLE.resolve(offer.state, trigger)
        now = self._clock.now()
        updated = replace(offer, state=rule.to_state, updated_at=now)
        self._store.update_offer(updated, conn=conn)
        self._audit_offer(
            updated, from_state=offer.state, to_state=rule.to_state, conn=conn
        )
        payload: dict[str, object] = {
            "offer_id": updated.offer_id,
            "merchant_id": updated.merchant_id,
            "state": updated.state,
            "version": updated.version,
        }
        if event_payload:
            payload.update(event_payload)
        self._emit(event, updated.offer_id, payload, conn=conn)
        return updated

    def _audit_offer(
        self,
        offer: OfferRecord,
        *,
        from_state: str | None,
        to_state: str,
        conn: Any | None,
    ) -> None:
        self._audit.append(
            AuditEventSpec(
                event_type="OFFER_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="Offer",
                subject_id=offer.offer_id,
                from_state=from_state,
                to_state=to_state,
                payload={"merchant_id": offer.merchant_id, "version": offer.version},
            ),
            clock=self._clock,
            conn=conn,
        )

    def _emit(
        self,
        event_type: str,
        subject_id: str,
        payload: Mapping[str, object],
        *,
        conn: Any | None,
    ) -> None:
        event = build_event(
            event_type=event_type,
            subject_type="Offer",
            subject_id=subject_id,
            producer=PRODUCER,
            topic=OFFER_EVENTS_TOPIC,
            payload=dict(payload),
            occurred_at=self._clock.now(),
        )
        self._outbox.enqueue(event, conn=conn)


def _parse_dt(value: object, what: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise InvalidRequestError(f"{what} must be timezone-aware")
        return value
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidRequestError(
                f"{what} must be an RFC3339 timestamp", code="invalid_request"
            ) from exc
        if parsed.tzinfo is None:
            raise InvalidRequestError(f"{what} must carry a timezone")
        return parsed
    raise InvalidRequestError(f"{what} is required", code="invalid_request")


def _opt_minor(value: object, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError(
            f"{what} must be int paisa", code="amount_must_be_integer"
        )
    if value < 0:
        raise InvalidRequestError(f"{what} must be >= 0 paisa")
    return value


def _opt_cap(value: object, what: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRequestError(f"{what} must be an integer >= 1", code="invalid_request")
    return value
