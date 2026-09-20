"use client";

// DualLabel — renders BOTH languages simultaneously. REQUIRED on
// safety-critical strings (approval confirm, freeze, reject, compensation
// resolution) per spec/15 §i18n: dual rendering, never toggled.
//
// Thin per-app instance of the shared factory in @bdpay/edge/i18n/DualLabel.

import { createDualLabel } from "@bdpay/edge/i18n/DualLabel";
import { COPY, type CopyKey } from "./copy";

export const DualLabel = createDualLabel<CopyKey>(COPY);
