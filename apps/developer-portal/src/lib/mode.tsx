"use client";

// Env mode context — sandbox/live toggle for the whole portal shell.
// Persisted in localStorage; defaults to sandbox (safe default for a
// developer portal).

import React, { createContext, useCallback, useContext, useEffect, useState } from "react";
import type { Env } from "@/lib/api/types";

interface ModeValue {
  env: Env;
  setEnv: (env: Env) => void;
}

const ModeContext = createContext<ModeValue>({ env: "sandbox", setEnv: () => undefined });

const STORAGE_KEY = "bdpay-portal-env";

export function ModeProvider({ children }: { children: React.ReactNode }) {
  const [env, setEnvState] = useState<Env>("sandbox");

  useEffect(() => {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (saved === "live" || saved === "sandbox") {
      setEnvState(saved);
    }
  }, []);

  const setEnv = useCallback((next: Env) => {
    setEnvState(next);
    window.localStorage.setItem(STORAGE_KEY, next);
  }, []);

  return <ModeContext.Provider value={{ env, setEnv }}>{children}</ModeContext.Provider>;
}

export function useMode(): ModeValue {
  return useContext(ModeContext);
}
