"use client";

// D06 — DualLabel factory: renders BOTH languages simultaneously. REQUIRED
// on safety-critical strings per spec/15 §i18n: dual rendering, never
// toggled. Shared verbatim between developer-portal and ops-console (they
// differ only by comments). diner-app's DualLabel renders bn first (part of
// its Bengali-first surface — a real behavioral difference, not just
// comments) and keeps its own, independent copy — see that app's
// src/lib/i18n/DualLabel.tsx.
//
// Not unit-tested in this package: it is React-bound, so it is exercised
// indirectly through each app's own build.

import React from "react";
import type { Bi } from "./lang-context";

export function createDualLabel<TCopyKey extends string>(
  copy: Record<TCopyKey, Bi>,
): (props: { k: TCopyKey; className?: string }) => React.JSX.Element {
  return function DualLabel({ k, className }: { k: TCopyKey; className?: string }) {
    const bi = copy[k];
    return (
      <span className={`dual-label ${className ?? ""}`}>
        <span className="dual-en">{bi.en}</span>
        <span className="dual-bn" lang="bn">
          {bi.bn}
        </span>
      </span>
    );
  };
}
