"use client";

// D05 — the shared client-side session hook, built by each app from its own
// session-fetching client function. Byte-identical between dp and ops today
// (both fetch a `Session`); diner-app differs only by the session type
// (`DinerSession`) and the fetch function (`getDinerSession`). Each app
// keeps its own src/lib/useSession.ts exporting `useSession`, so no
// consumer of that hook changes.
//
// Not unit-tested in this package: it is React/Next-bound (`useRouter` from
// next/navigation), so it is exercised indirectly through each app's own
// build and page tests, the same way edge-proxy.ts and live-proxy.ts are.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

export interface UseSessionResult<TSession> {
  session: TSession | null;
  loading: boolean;
  reload: () => void;
}

/**
 * Builds a `useSession(redirectOnMissing = true)` hook around `fetchSession`.
 * On mount (and whenever `reload()` bumps an internal nonce), fetches the
 * session; a rejected fetch clears the session and, unless
 * `redirectOnMissing` is false, redirects to `/login`. Guards against
 * setting state after unmount the same way every app's hook did before.
 */
export function createUseSession<TSession>(
  fetchSession: () => Promise<TSession>,
): (redirectOnMissing?: boolean) => UseSessionResult<TSession> {
  return function useSession(redirectOnMissing = true): UseSessionResult<TSession> {
    const [session, setSession] = useState<TSession | null>(null);
    const [loading, setLoading] = useState(true);
    const [nonce, setNonce] = useState(0);
    const router = useRouter();

    useEffect(() => {
      let live = true;
      fetchSession()
        .then((s) => {
          if (live) {
            setSession(s);
            setLoading(false);
          }
        })
        .catch(() => {
          if (live) {
            setSession(null);
            setLoading(false);
            if (redirectOnMissing) router.replace("/login");
          }
        });
      return () => {
        live = false;
      };
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [router, redirectOnMissing, nonce]);

    return { session, loading, reload: () => setNonce((n) => n + 1) };
  };
}
