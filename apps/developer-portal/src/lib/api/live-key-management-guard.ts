import type { ErrorEnvelope } from "./types";

export const LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_CODE =
  "live_key_management_requires_merchant_auth";

export const LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_MESSAGE =
  "Live API-key management requires real merchant-member authentication before keys can be created, rotated, or revoked.";

export function isLiveApiKeyMutation(method: string, path: readonly string[]): boolean {
  if (method.toUpperCase() !== "POST") return false;
  if (path[0] !== "v1" || path[1] !== "api-keys") return false;
  if (path.length === 2) return true;
  return path.length === 4 && (path[3] === "rotate" || path[3] === "revoke");
}

export function liveKeyManagementRequiresMerchantAuthEnvelope(
  requestId = "frontend_live_adapter",
): ErrorEnvelope {
  return {
    error: {
      type: "authorization",
      code: LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_CODE,
      message: LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_MESSAGE,
      request_id: requestId,
      doc_url: `https://docs.bdpay.example/errors/${LIVE_KEY_MANAGEMENT_REQUIRES_MERCHANT_AUTH_CODE}`,
    },
  };
}
