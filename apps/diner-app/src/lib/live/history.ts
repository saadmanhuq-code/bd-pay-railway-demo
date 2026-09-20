// Live adapter normalization: raw gateway payment-intent rows -> History screen
// contract. Kept pure so it can be unit-tested without the Next.js runtime.

import type { PaymentIntentView } from "@/lib/api/types";

export const REDEMPTION_STATES = new Set([
  "RESERVED",
  "APPLIED",
  "RELEASED",
  "REVERSED",
  "SETTLED",
]);

/**
 * F10: a payment status is NOT settlement evidence.
 *
 * This helper used to answer SETTLED whenever the gateway row carried no (or
 * an unrecognised) redemption_state and the payment had SUCCEEDED. The History
 * screen renders SETTLED as "settled with the restaurant", but the backend
 * redemption FSM moves RESERVED -> APPLIED on payment success and reaches
 * SETTLED only on a later settlement confirmation
 * (bdpay/kernel/offers/states.py, consumer.py). The inference therefore showed
 * diners a restaurant settlement that had not happened (review F10 / evidence
 * E33, E36, E37).
 *
 * Missing or unrecognised evidence now renders as unknown (null); only an
 * explicit upstream state is reported.
 */
export function normalizeRedemptionState(
  value: unknown,
  _status: PaymentIntentView["status"],
): PaymentIntentView["redemption_state"] {
  if (typeof value === "string" && REDEMPTION_STATES.has(value)) {
    return value as PaymentIntentView["redemption_state"];
  }
  return null;
}

export interface EnrichOptions {
  defaultMerchantDisplayName?: string;
  defaultMerchantDisplayNameBn?: string;
  defaultCustomerId?: string;
  defaultCurrency?: PaymentIntentView["currency"];
}

export function enrichIntentView(
  intent: PaymentIntentView,
  options: EnrichOptions = {},
): PaymentIntentView {
  const {
    defaultMerchantDisplayName = "BDPay Demo Merchant",
    defaultMerchantDisplayNameBn = "বিডিপে ডেমো মার্চেন্ট",
    defaultCustomerId = "cust_demo",
    defaultCurrency = "BDT",
  } = options;

  // The real gateway intent row carries amount_minor as the net. If the
  // gateway has not yet attached diner-specific display fields or offer data,
  // normalize the row into the History screen contract without fabricating an
  // offer. The raw gateway row remains the source of truth for status/amounts.
  return {
    ...intent,
    merchant_display_name: intent.merchant_display_name ?? defaultMerchantDisplayName,
    merchant_display_name_bn: intent.merchant_display_name_bn ?? defaultMerchantDisplayNameBn,
    customer_id: intent.customer_id ?? defaultCustomerId,
    currency: intent.currency ?? defaultCurrency,
    offer: intent.offer ?? null,
    redemption_state: normalizeRedemptionState(intent.redemption_state, intent.status),
  };
}
