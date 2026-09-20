"use client";

// D06 — LangContext factory: bn/en with localStorage persistence
// (spec/15 §i18n), shared verbatim between developer-portal and ops-console
// (they differ only by STORAGE_KEY and comments). diner-app's LangContext
// differs by more than that (a Bengali-first default, no navigator
// sniffing) and keeps its own, independent copy — see that app's
// src/lib/i18n/LangContext.tsx.
//
// Not unit-tested in this package: it is React-bound (context, hooks), so it
// is exercised indirectly through each app's own build.

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";

export type Lang = "en" | "bn";

export interface Bi {
  en: string;
  bn: string;
}

export interface LangValue<TCopyKey extends string> {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: TCopyKey) => string;
}

export interface LangContextConfig<TCopyKey extends string> {
  /** localStorage key this instance reads/writes (the one per-app difference
   * beyond comments: "bdpay-portal-lang", "bdpay-ops-lang", ...). */
  storageKey: string;
  /** The app's own bilingual copy table. */
  copy: Record<TCopyKey, Bi>;
  /** The app's own `pick(bi, lang)` from its copy.ts (always
   * `lang === "bn" ? bi.bn : bi.en`, but kept as a parameter so this stays a
   * verbatim behavioral copy rather than a re-implementation). */
  pick: (bi: Bi, lang: Lang) => string;
}

export interface LangContextInstance<TCopyKey extends string> {
  LangProvider: (props: { children: React.ReactNode }) => React.JSX.Element;
  useLang: () => LangValue<TCopyKey>;
}

export function createLangContext<TCopyKey extends string>(
  config: LangContextConfig<TCopyKey>,
): LangContextInstance<TCopyKey> {
  const { storageKey, copy, pick } = config;

  const LangContext = createContext<LangValue<TCopyKey>>({
    lang: "en",
    setLang: () => undefined,
    t: (key) => copy[key].en,
  });

  function LangProvider({ children }: { children: React.ReactNode }) {
    const [lang, setLangState] = useState<Lang>("en");

    useEffect(() => {
      const saved = window.localStorage.getItem(storageKey);
      if (saved === "bn" || saved === "en") {
        setLangState(saved);
      } else if (typeof navigator !== "undefined" && navigator.language.startsWith("bn")) {
        setLangState("bn");
      }
    }, []);

    const setLang = useCallback((next: Lang) => {
      setLangState(next);
      window.localStorage.setItem(storageKey, next);
    }, []);

    const t = useCallback((key: TCopyKey) => pick(copy[key], lang), [lang]);

    return <LangContext.Provider value={{ lang, setLang, t }}>{children}</LangContext.Provider>;
  }

  function useLang(): LangValue<TCopyKey> {
    return useContext(LangContext);
  }

  return { LangProvider, useLang };
}
