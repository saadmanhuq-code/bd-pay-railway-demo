"use client";

// The developer-portal instance of the shared TOTP step-up modal
// (@bdpay/edge/ui/TotpModal): bound to this app's DualLabel, useLang and copy
// keys. Pages import { TotpModal } from "@/components/TotpModal".

import { createTotpModal } from "@bdpay/edge/ui/TotpModal";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { useLang } from "@/lib/i18n/LangContext";
import type { CopyKey } from "@/lib/i18n/copy";

export type { TotpModalProps } from "@bdpay/edge/ui/TotpModal";

export const TotpModal = createTotpModal<CopyKey>({ DualLabel, useLang });
