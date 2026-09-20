"use client";

// DualLabel — renders BOTH languages simultaneously, bn first (Bengali-first
// surface). REQUIRED on safety-critical strings (reservation hold notice,
// cap refusals, D1 login notice) per the spec/15 §i18n posture the other
// edge apps follow: dual rendering, never toggled.

import React from "react";
import { COPY, type CopyKey } from "./copy";

export function DualLabel({ k, className }: { k: CopyKey; className?: string }) {
  const bi = COPY[k];
  return (
    <span className={`dual-label ${className ?? ""}`}>
      <span className="dual-bn" lang="bn">
        {bi.bn}
      </span>
      <span className="dual-en">{bi.en}</span>
    </span>
  );
}
