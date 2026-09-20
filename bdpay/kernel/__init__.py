"""bdpay.kernel — payment lifecycle, merchant KYB, customer eKYC (specs 02/08/09).

Owns the PaymentIntent / PaymentAttempt / Refund state machines and the
payment orchestrator (spec/02), the merchant-KYB onboarding service
(spec/08), and the customer-eKYC service (spec/09). All FSMs are
refusal-first: any (state, trigger) pair not declared in the transition
table is denied with a typed error (spec/00 §8).

Cross-package calls go ONLY through the ports in
:mod:`bdpay.platform.interfaces`; the ledger package is the sole writer of
postings; events are produced only via the outbox.
"""
