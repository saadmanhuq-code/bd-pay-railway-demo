import type { Metadata } from "next";
import React from "react";
import { LangProvider } from "@/lib/i18n/LangContext";
import { ModeProvider } from "@/lib/mode";
import "./globals.css";

export const metadata: Metadata = {
  title: "BD-PAY Developer Portal",
  description: "Merchant developer portal for the BD-PAY payment platform",
};

// SEC-03: render every route dynamically so the per-request CSP nonce minted in
// proxy.ts is stamped onto Next.js's inline hydration scripts. Statically
// prerendered pages are built before any request exists, so they cannot carry a
// per-request nonce; under the strict 'strict-dynamic' script-src those
// un-nonced inline bootstrap scripts would be blocked and hydration would break.
// Forcing dynamic rendering is the supported way to make nonce-based CSP work.
export const dynamic = "force-dynamic";

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <LangProvider>
          <ModeProvider>{children}</ModeProvider>
        </LangProvider>
      </body>
    </html>
  );
}
