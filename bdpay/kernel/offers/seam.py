"""The zero-offer seam — OfferRedemption reservation INSIDE intent creation.

spec/18 §Extension to POST /v1/payment-intents (binding semantics):

1. ``offer_id`` absent -> behavior byte-identical to spec/02: this module
   delegates straight to the orchestrator's ``create_intent`` and touches
   NOTHING else (zero-offer invariant — pinned by regression test).
2. ``offer_id`` present -> the request MUST carry an authenticated
   ``customer_id`` (decision D1; guests get
   ``401 authentication/offer_requires_login``) and ``gross_amount_minor``
   is REQUIRED; the server runs ``evaluate_eligibility``, atomically
   RESERVES the redemption (counters, fail-closed), computes
   ``discount_minor``, and creates the intent with
   ``amount_minor = gross - discount`` (the NET — the only amount the rail,
   the ledger, and settlement ever see). No mid-flow repricing, ever.
3. If reservation fails, the intent is never created
   (``422 limit_exceeded/offer_cap_exhausted_<scope>`` with the narrowest
   exhausted scope as the code suffix, or
   ``400 invalid_request/offer_not_eligible``).

Dynamic-QR dependency (spec/18 regulatory mapping): redemption requires a
DYNAMIC-amount Bangla QR — the net is computed per-transaction. A
``BANGLA_QR`` reservation whose QR mode is not declared DYNAMIC refuses with
``method_not_allowed_static_qr`` (fail closed; static-QR-only merchants
cannot run offers).

PRE_FLIGHT guard: :meth:`OfferPaymentSeam.guard_reservation_held` implements
``offer_reservation_held`` / ``offer_reservation_lost``. The additive spec/02
FSM-1 rows are shipped as data (:data:`SPEC02_FSM1_AMENDMENT`) for the
spec/02 owner to merge at activation (errata S18-E13); until then the seam
wrapper enforces the guard refusal-first before dispatch.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol, runtime_checkable

from bdpay.kernel.fsm import TransitionRule
from bdpay.kernel.models import PaymentIntentRecord
from bdpay.kernel.offers.eligibility import (
    CounterReading,
    ReasonCode,
    dhaka_date_str,
    evaluate_eligibility,
)
from bdpay.kernel.offers.ids_ext import make_offer_id
from bdpay.kernel.offers.ledger_legs import commission_minor_for, subsidy_minor_for
from bdpay.kernel.offers.models import OfferRecord, OfferRedemptionRecord
from bdpay.kernel.offers.repository import OfferStore
from bdpay.kernel.offers.service import OFFER_EVENTS_TOPIC, PRODUCER, OfferMerchantPort
from bdpay.kernel.offers.states import OFFER_TABLE, REDEMPTION_TABLE
from bdpay.kernel.orchestrator import PaymentIntentRequest
from bdpay.platform.clock import Clock
from bdpay.platform.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    InvalidRequestError,
    LimitExceededError,
    NotFoundError,
)
from bdpay.platform.interfaces import AuditEventSpec, AuditPort, OutboxPort
from bdpay.platform.outbox import build_event

__all__ = ["SPEC02_FSM1_AMENDMENT", "OfferPaymentSeam"]

#: spec/18 §Spec 02 FSM 1 amendment — additive rows, shipped as data for the
#: spec/02 owner to merge into INTENT_TABLE at activation (errata S18-E13).
SPEC02_FSM1_AMENDMENT: tuple[TransitionRule, ...] = (
    TransitionRule(
        from_state="PRE_FLIGHT",
        trigger="preflight_passed",
        to_state="PROCESSING",
        guard="offer_reservation_held",
        side_effects=("unchanged",),
    ),
    TransitionRule(
        from_state="PRE_FLIGHT",
        trigger="offer_reservation_lost",
        to_state="FAILED",
        side_effects=("aud:OFFER_RESERVATION_LOST", "event:payment_intent.failed"),
    ),
)


@runtime_checkable
class PaymentIntentCreator(Protocol):
    """The slice of PaymentOrchestrator the seam composes with."""

    def create_intent(
        self, request: PaymentIntentRequest, *, conn: Any | None = None
    ) -> PaymentIntentRecord: ...


@runtime_checkable
class IntentLookupPort(Protocol):
    """Read-only intent lookups (PaymentStore satisfies this protocol)."""

    def find_intent_by_idempotency_key(
        self, merchant_id: str, idempotency_key: str, *, conn: Any | None = None
    ) -> PaymentIntentRecord | None: ...


class OfferPaymentSeam:
    """Reserve -> create-net-intent, atomically; plus the PRE_FLIGHT guard."""

    def __init__(
        self,
        *,
        store: OfferStore,
        payments: PaymentIntentCreator,
        intents: IntentLookupPort,
        merchants: OfferMerchantPort,
        audit: AuditPort,
        outbox: OutboxPort,
        clock: Clock,
    ) -> None:
        self._store = store
        self._payments = payments
        self._intents = intents
        self._merchants = merchants
        self._audit = audit
        self._outbox = outbox
        self._clock = clock

    # ------------------------------------------------------------------
    # the seam entry point
    # ------------------------------------------------------------------

    def create_intent_with_offer(
        self,
        request: PaymentIntentRequest,
        *,
        offer_id: str | None = None,
        gross_amount_minor: int | None = None,
        qr_mode: str | None = None,
        conn: Any | None = None,
    ) -> tuple[PaymentIntentRecord, OfferRedemptionRecord | None]:
        """The spec/18 payment-intents extension, seam-side.

        With ``offer_id is None`` this is a pure passthrough — the exact
        same ``create_intent`` call spec/02 makes, no offer code reachable
        (zero-offer invariant). With Postgres, the caller passes ``conn`` so
        counters, intent, and redemption commit or roll back together.
        """
        if offer_id is None:
            return self._payments.create_intent(request, conn=conn), None

        # D1: redemption REQUIRES an authenticated customer.
        if not request.customer_id:
            raise AuthenticationError(
                "offer redemption requires diner-app login (no guest redemption)",
                code="offer_requires_login",
            )
        if isinstance(gross_amount_minor, bool) or not isinstance(gross_amount_minor, int):
            raise InvalidRequestError(
                "gross_amount_minor is required when offer_id is present",
                code="gross_amount_required",
            )
        if gross_amount_minor <= 0:
            raise InvalidRequestError(
                "gross_amount_minor must be > 0 paisa", code="amount_not_positive"
            )

        offer = self._store.get_offer(offer_id, conn=conn)
        if offer is None:
            raise NotFoundError("offer not found", code="offer_not_found")
        if offer.merchant_id != request.merchant_id:
            raise AuthorizationError(
                "offer belongs to a different merchant", code="offer_merchant_mismatch"
            )
        if offer.kind == "BOGO":
            # Decision D2: build-refused until the principal lifts it.
            raise InvalidRequestError(
                "BOGO offers are not yet enabled (pilot is PERCENT_OFF-only)",
                code="bogo_not_yet_enabled",
            )
        # Dynamic-QR dependency, honored at the seam: net is per-transaction,
        # so a static QR cannot carry a redemption. Fail closed when the QR
        # mode is not declared DYNAMIC.
        if request.method == "BANGLA_QR" and qr_mode != "DYNAMIC":
            raise InvalidRequestError(
                "offer redemption over Bangla QR requires a dynamic-amount QR",
                code="method_not_allowed_static_qr",
            )

        # Replay (client Idempotency-Key): the stored intent + its redemption
        # return verbatim; counters are NOT touched again.
        existing = self._intents.find_intent_by_idempotency_key(
            request.merchant_id, request.idempotency_key, conn=conn
        )
        if existing is not None:
            intent = self._payments.create_intent(
                replace(request, amount_minor=existing.amount_minor), conn=conn
            )
            redemption = self._store.get_redemption_by_intent(
                intent.payment_intent_id, conn=conn
            )
            return intent, redemption

        now = self._clock.now()
        result = self._evaluate_fail_closed(
            offer, gross_amount_minor=gross_amount_minor, request=request, now=now, conn=conn
        )
        discount_minor = result.discount_minor
        net_amount_minor = gross_amount_minor - discount_minor

        taken = self._reserve_counters(offer, request.customer_id, now=now, conn=conn)
        try:
            intent = self._payments.create_intent(
                replace(request, amount_minor=net_amount_minor), conn=conn
            )
        except BaseException:
            # In-memory compensation; under Postgres the aborted transaction
            # rolls the increments back anyway (spec/18: if anything fails,
            # the intent is never created and no slot stays consumed).
            self._recredit(taken, now=now, conn=conn)
            raise

        redemption = OfferRedemptionRecord(
            redemption_id=make_offer_id(
                "ored",
                {"offer_id": offer.offer_id, "payment_intent_id": intent.payment_intent_id},
            ),
            offer_id=offer.offer_id,
            offer_version=offer.version,
            payment_intent_id=intent.payment_intent_id,
            merchant_id=offer.merchant_id,
            customer_id=request.customer_id,
            gross_amount_minor=gross_amount_minor,
            discount_minor=discount_minor,
            net_amount_minor=net_amount_minor,
            commission_minor=commission_minor_for(net_amount_minor, offer.commission_bps),
            subsidy_minor=subsidy_minor_for(discount_minor, offer.subsidy_bps),
            state="RESERVED",
            reserved_at=now,
            reserved_expires_at=intent.expires_at,
        )
        self._store.insert_redemption(redemption, conn=conn)
        self._store.set_intent_offer_id(
            intent.payment_intent_id, offer.offer_id, conn=conn
        )
        self._audit_redemption(redemption, from_state=None, to_state="RESERVED", conn=conn)
        self._emit_redemption(
            "offer_redemption.reserved",
            redemption,
            {
                "redemption_id": redemption.redemption_id,
                "offer_id": offer.offer_id,
                "payment_intent_id": intent.payment_intent_id,
                "gross_amount_minor": gross_amount_minor,
                "discount_minor": discount_minor,
                "net_amount_minor": net_amount_minor,
            },
            conn=conn,
        )
        self._maybe_exhaust(offer, conn=conn)
        return intent, redemption

    @staticmethod
    def intent_offer_block(redemption: OfferRedemptionRecord) -> dict[str, object]:
        """The read-only ``offer`` block the intent response gains (spec/18)."""
        return {
            "offer": {
                "offer_id": redemption.offer_id,
                "gross_amount_minor": redemption.gross_amount_minor,
                "discount_minor": redemption.discount_minor,
            }
        }

    # ------------------------------------------------------------------
    # PRE_FLIGHT guard (spec/02 FSM 1 amendment, seam-enforced until merge)
    # ------------------------------------------------------------------

    def guard_reservation_held(
        self, payment_intent_id: str, *, conn: Any | None = None
    ) -> OfferRedemptionRecord | None:
        """``offer_reservation_held``: cheap lookup, no re-evaluation, no reprice.

        No redemption row -> the intent carries no offer -> guard passes
        (the ``offer_id IS NULL`` short-circuit). A redemption that is not
        ``RESERVED``-and-unexpired refuses with
        ``conflict/offer_reservation_lost`` (audit ``OFFER_RESERVATION_LOST``).
        """
        redemption = self._store.get_redemption_by_intent(payment_intent_id, conn=conn)
        if redemption is None:
            return None
        now = self._clock.now()
        if redemption.state == "RESERVED" and now <= redemption.reserved_expires_at:
            return redemption
        self._audit.append(
            AuditEventSpec(
                event_type="OFFER_RESERVATION_LOST",
                actor_id=PRODUCER,
                subject_type="OfferRedemption",
                subject_id=redemption.redemption_id,
                from_state=redemption.state,
                to_state=redemption.state,
                payload={"payment_intent_id": payment_intent_id},
            ),
            clock=self._clock,
            conn=conn,
        )
        raise ConflictError(
            "offer reservation is no longer held; payment refused",
            code="offer_reservation_lost",
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _evaluate_fail_closed(
        self,
        offer: OfferRecord,
        *,
        gross_amount_minor: int,
        request: PaymentIntentRequest,
        now,
        conn: Any | None,
    ):
        """Run the evaluator; an evaluator exception is ineligible (fail closed)."""
        customer_counter = None
        if offer.cap_per_customer is not None:
            counter = self._store.get_counter(
                make_offer_id(
                    "octr",
                    {
                        "offer_id": offer.offer_id,
                        "scope": "customer",
                        "scope_key": request.customer_id,
                    },
                ),
                conn=conn,
            )
            customer_counter = CounterReading(
                used=counter.used if counter is not None else 0,
                cap_limit=offer.cap_per_customer,
            )
        try:
            result = evaluate_eligibility(
                offer,
                merchant_active=self._merchants.is_active(offer.merchant_id),
                now_utc=now,
                gross_amount_minor=gross_amount_minor,
                method=request.method,
                day_counter=self._advisory(offer, "day", dhaka_date_str(now), conn=conn),
                total_counter=self._advisory(offer, "total", "ALL", conn=conn),
                customer_counter=customer_counter,
            )
        except InvalidRequestError:
            raise  # typed refusals (bogo_not_yet_enabled, discount_equals_gross)
        except Exception as exc:
            # Failure-mode table: evaluator exception -> ineligible, fail closed.
            self._audit.append(
                AuditEventSpec(
                    event_type="OFFER_EVAL_ERROR",
                    actor_id=PRODUCER,
                    subject_type="Offer",
                    subject_id=offer.offer_id,
                    payload={"error": type(exc).__name__},
                ),
                clock=self._clock,
                conn=conn,
            )
            raise InvalidRequestError(
                "offer eligibility could not be evaluated; redemption refused",
                code="offer_not_eligible",
            ) from exc
        if not result.eligible:
            # Cap exhaustion is ALWAYS the 422 limit_exceeded refusal with the
            # narrowest exhausted scope as the code suffix — whether detected
            # by the advisory fast-fail here or by the atomic reservation
            # below (the advisory read only makes the refusal cheaper; it
            # never changes the contract).
            cap_scope_by_reason = {
                ReasonCode.CAP_CUSTOMER_EXHAUSTED: "customer",
                ReasonCode.CAP_DAY_EXHAUSTED: "day",
                ReasonCode.CAP_TOTAL_EXHAUSTED: "total",
            }
            non_cap = [r for r in result.reasons if r not in cap_scope_by_reason]
            if not non_cap:
                for reason in (
                    ReasonCode.CAP_CUSTOMER_EXHAUSTED,
                    ReasonCode.CAP_DAY_EXHAUSTED,
                    ReasonCode.CAP_TOTAL_EXHAUSTED,
                ):
                    if reason in result.reasons:
                        raise LimitExceededError(
                            f"offer cap exhausted at the {cap_scope_by_reason[reason]} "
                            "scope; reservation refused",
                            code=f"offer_cap_exhausted_{cap_scope_by_reason[reason]}",
                        )
            reason_codes = ", ".join(r.value for r in result.reasons)
            raise InvalidRequestError(
                f"offer not eligible: {reason_codes}", code="offer_not_eligible"
            )
        return result

    def _advisory(
        self, offer: OfferRecord, scope: str, scope_key: str, *, conn: Any | None
    ) -> CounterReading | None:
        cap = offer.cap_per_day if scope == "day" else offer.cap_total
        if cap is None:
            return None
        record = self._store.get_counter(
            make_offer_id(
                "octr", {"offer_id": offer.offer_id, "scope": scope, "scope_key": scope_key}
            ),
            conn=conn,
        )
        if record is None:
            return CounterReading(used=0, cap_limit=cap)
        return CounterReading(used=record.used, cap_limit=record.cap_limit)

    def _applicable_scopes(
        self, offer: OfferRecord, customer_id: str, *, now
    ) -> list[tuple[str, str, int]]:
        """(scope, scope_key, cap_limit) rows, NARROWEST first.

        Order matters: the narrowest exhausted scope names the refusal code
        suffix, so reservation attempts run customer -> day -> total.
        """
        scopes: list[tuple[str, str, int]] = []
        if offer.cap_per_customer is not None:
            scopes.append(("customer", customer_id, offer.cap_per_customer))
        if offer.cap_per_day is not None:
            scopes.append(("day", dhaka_date_str(now), offer.cap_per_day))
        if offer.cap_total is not None:
            scopes.append(("total", "ALL", offer.cap_total))
        return scopes

    def _reserve_counters(
        self, offer: OfferRecord, customer_id: str, *, now, conn: Any | None
    ) -> list[tuple[str, str]]:
        """ALL applicable scopes must take a slot or the reservation refuses.

        Returns the (counter_id, scope_key) pairs actually taken so the
        caller can compensate when a later step fails in-memory; under
        Postgres the raise aborts the enclosing transaction anyway.
        """
        taken: list[tuple[str, str]] = []
        for scope, scope_key, cap_limit in self._applicable_scopes(
            offer, customer_id, now=now
        ):
            counter_id = make_offer_id(
                "octr", {"offer_id": offer.offer_id, "scope": scope, "scope_key": scope_key}
            )
            self._store.get_or_create_counter(
                counter_id,
                offer_id=offer.offer_id,
                scope=scope,
                scope_key=scope_key,
                cap_limit=cap_limit,
                now=now,
                conn=conn,
            )
            if not self._store.try_reserve_counter(counter_id, now=now, conn=conn):
                self._recredit(taken, now=now, conn=conn)
                raise LimitExceededError(
                    f"offer cap exhausted at the {scope} scope; reservation refused",
                    code=f"offer_cap_exhausted_{scope}",
                )
            taken.append((counter_id, scope_key))
        return taken

    def _recredit(
        self, taken: list[tuple[str, str]], *, now, conn: Any | None
    ) -> None:
        for counter_id, _scope_key in reversed(taken):
            self._store.recredit_counter(counter_id, now=now, conn=conn)

    def _maybe_exhaust(self, offer: OfferRecord, *, conn: Any | None) -> None:
        """Post-reservation check: total counter at cap -> ACTIVE -> EXHAUSTED."""
        if offer.cap_total is None:
            return
        counter = self._store.get_counter(
            make_offer_id(
                "octr", {"offer_id": offer.offer_id, "scope": "total", "scope_key": "ALL"}
            ),
            conn=conn,
        )
        if counter is None or counter.used < counter.cap_limit:
            return
        current = self._store.get_offer(offer.offer_id, conn=conn)
        if current is None or current.state != "ACTIVE":
            return
        rule = OFFER_TABLE.resolve(current.state, "cap_total_reached")
        now = self._clock.now()
        updated = replace(current, state=rule.to_state, updated_at=now)
        self._store.update_offer(updated, conn=conn)
        self._audit.append(
            AuditEventSpec(
                event_type="OFFER_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="Offer",
                subject_id=updated.offer_id,
                from_state=current.state,
                to_state=rule.to_state,
                payload={"merchant_id": updated.merchant_id, "trigger": "cap_total_reached"},
            ),
            clock=self._clock,
            conn=conn,
        )
        self._outbox.enqueue(
            build_event(
                event_type="offer.exhausted",
                subject_type="Offer",
                subject_id=updated.offer_id,
                producer=PRODUCER,
                topic=OFFER_EVENTS_TOPIC,
                payload={
                    "offer_id": updated.offer_id,
                    "merchant_id": updated.merchant_id,
                    "state": updated.state,
                },
                occurred_at=now,
            ),
            conn=conn,
        )

    def _audit_redemption(
        self,
        redemption: OfferRedemptionRecord,
        *,
        from_state: str | None,
        to_state: str,
        conn: Any | None,
    ) -> None:
        if to_state not in REDEMPTION_TABLE.states:
            raise ConflictError(f"unknown redemption state {to_state!r}")
        self._audit.append(
            AuditEventSpec(
                event_type="OFFER_REDEMPTION_STATE_TRANSITION",
                actor_id=PRODUCER,
                subject_type="OfferRedemption",
                subject_id=redemption.redemption_id,
                from_state=from_state,
                to_state=to_state,
                payload={
                    "offer_id": redemption.offer_id,
                    "payment_intent_id": redemption.payment_intent_id,
                },
            ),
            clock=self._clock,
            conn=conn,
        )

    def _emit_redemption(
        self,
        event_type: str,
        redemption: OfferRedemptionRecord,
        payload: dict[str, object],
        *,
        conn: Any | None,
    ) -> None:
        self._outbox.enqueue(
            build_event(
                event_type=event_type,
                subject_type="OfferRedemption",
                subject_id=redemption.redemption_id,
                producer=PRODUCER,
                topic=OFFER_EVENTS_TOPIC,
                payload=payload,
                occurred_at=self._clock.now(),
            ),
            conn=conn,
        )
