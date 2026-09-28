"use client";

// AdaptiveShell — for pages that are reachable both signed-out and signed-in
// (platform status, onboarding tracker). A signed-in merchant keeps the full
// portal chrome (nav, env badge, sign-out) so they never lose their way back;
// a signed-out visitor gets the minimal PublicShell.

import React from "react";
import { PublicShell } from "@/components/PublicShell";
import { Shell } from "@/components/Shell";
import { useSession } from "@/lib/useSession";

export function AdaptiveShell({ children }: { children: React.ReactNode }) {
  const { session, loading } = useSession(false);
  if (loading) return <PublicShell>{null}</PublicShell>;
  if (session) return <Shell>{children}</Shell>;
  return <PublicShell>{children}</PublicShell>;
}
