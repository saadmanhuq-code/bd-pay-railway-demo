"use client";

// LangContext — bn/en with localStorage persistence (spec/15 §i18n).
//
// Thin per-app instance of the shared factory in @bdpay/edge/i18n/lang-context.

import { createLangContext } from "@bdpay/edge/i18n/lang-context";
import { COPY, pick, type CopyKey } from "./copy";

const instance = createLangContext<CopyKey>({
  storageKey: "bdpay-portal-lang",
  copy: COPY,
  pick,
});

export const LangProvider = instance.LangProvider;
export const useLang = instance.useLang;
