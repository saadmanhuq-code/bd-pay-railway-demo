"use client";

// LangContext — Bengali-FIRST bn/en with localStorage persistence (spec/18
// language requirements; same provider shape as ops-console/developer-portal,
// but the default language is bn — the diner-app is a Bangladeshi consumer
// surface, so English is the opt-in, not Bengali).

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import { COPY, pick, type CopyKey, type Lang } from "./copy";

interface LangValue {
  lang: Lang;
  setLang: (lang: Lang) => void;
  t: (key: CopyKey) => string;
}

const LangContext = createContext<LangValue>({
  lang: "bn",
  setLang: () => undefined,
  t: (key) => COPY[key].bn,
});

const STORAGE_KEY = "bdpay-diner-lang";

export function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>("bn");

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "bn" || saved === "en") {
      setLangState(saved);
    }
    // No navigator sniffing: bn is the default unless the user chose en.
  }, []);

  const setLang = useCallback((next: Lang) => {
    setLangState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }, []);

  const t = useCallback((key: CopyKey) => pick(COPY[key], lang), [lang]);

  return <LangContext.Provider value={{ lang, setLang, t }}>{children}</LangContext.Provider>;
}

export function useLang(): LangValue {
  return useContext(LangContext);
}
