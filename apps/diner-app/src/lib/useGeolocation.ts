"use client";

import { useCallback, useState } from "react";
import type { LatLng } from "@/lib/demo/geo";

export type GeoStatus = "idle" | "prompting" | "granted" | "denied" | "unavailable";

export interface GeoState {
  status: GeoStatus;
  position: LatLng | null;
  error: string | null;
}

export function useGeolocation() {
  const [state, setState] = useState<GeoState>({
    status: "idle",
    position: null,
    error: null,
  });

  const request = useCallback(() => {
    if (typeof navigator === "undefined" || !navigator.geolocation) {
      setState({ status: "unavailable", position: null, error: "unavailable" });
      return;
    }
    setState((s) => ({ ...s, status: "prompting", error: null }));
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        setState({
          status: "granted",
          position: { lat: pos.coords.latitude, lng: pos.coords.longitude },
          error: null,
        });
      },
      (err) => {
        const denied = err.code === err.PERMISSION_DENIED;
        setState({
          status: denied ? "denied" : "unavailable",
          position: null,
          error: denied ? "denied" : "unavailable",
        });
      },
      { enableHighAccuracy: false, timeout: 12000, maximumAge: 60_000 },
    );
  }, []);

  const clear = useCallback(() => {
    setState({ status: "idle", position: null, error: null });
  }, []);

  return { ...state, request, clear };
}
