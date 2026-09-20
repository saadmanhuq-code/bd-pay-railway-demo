"use client";

// Client-side QR image from an ENGINE-issued payload string (mock/demo only).
// The TLV bytes are never constructed here — we only render what the server returned.

import React, { useMemo } from "react";
import { renderSVG } from "@/lib/vendor/uqr.mjs";

export function QrCode({ payload, label }: { payload: string; label?: string }) {
  const svg = useMemo(() => {
    if (!payload) return null;
    try {
      return renderSVG(payload, { ecc: "M", border: 2 });
    } catch {
      return null;
    }
  }, [payload]);

  if (!svg) {
    return (
      <div className="qr-frame qr-frame-fallback" role="img" aria-label={label ?? "QR unavailable"}>
        <p className="subtle">QR render unavailable</p>
        <div className="qr-payload mono">{payload}</div>
      </div>
    );
  }

  return (
    <div
      className="qr-frame"
      role="img"
      aria-label={label ?? "Bangla QR"}
      // Trusted: SVG produced locally from the payload string by vendored uqr.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
