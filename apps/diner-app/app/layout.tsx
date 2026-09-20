import type { Metadata } from "next";
import React from "react";
import localFont from "next/font/local";
import { LangProvider } from "@/lib/i18n/LangContext";
import "./globals.css";

// Self-hosted so `next build` works on CI runners that cannot reach
// fonts.googleapis.com (local-runner egress flake).
const notoBengali = localFont({
  src: "../src/fonts/NotoSansBengali-Variable.ttf",
  display: "swap",
  variable: "--font-noto-bengali",
  weight: "100 900",
});

export const metadata: Metadata = {
  title: "বিডি-পে ডাইনার — BD-PAY Diner",
  description:
    "রেস্তোরাঁর অফার, পেমেন্টের সময়েই প্রযোজ্য। Restaurant offers applied at payment on BD-PAY.",
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
    <html lang="bn" className={notoBengali.variable}>
      <body className={notoBengali.className}>
        <LangProvider>{children}</LangProvider>
      </body>
    </html>
  );
}
