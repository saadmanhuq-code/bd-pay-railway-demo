"use client";

import { createUseSession } from "@bdpay/edge/use-session";
import { getSession } from "@/lib/api/client";
import type { Session } from "@/lib/api/types";

export const useSession: (redirectOnMissing?: boolean) => {
  session: Session | null;
  loading: boolean;
  reload: () => void;
} = createUseSession<Session>(getSession);
