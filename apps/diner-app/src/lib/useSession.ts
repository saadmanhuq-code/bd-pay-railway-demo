"use client";

import { createUseSession } from "@bdpay/edge/use-session";
import { getDinerSession } from "@/lib/api/client";
import type { DinerSession } from "@/lib/api/types";

export const useSession: (redirectOnMissing?: boolean) => {
  session: DinerSession | null;
  loading: boolean;
  reload: () => void;
} = createUseSession<DinerSession>(getDinerSession);
