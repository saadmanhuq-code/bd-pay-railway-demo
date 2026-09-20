"use client";

// Free OpenStreetMap embed via Leaflet (bundled — no paid Google Maps).
// Client-only: Leaflet touches `window`.

import React, { useEffect, useRef } from "react";

export interface MerchantMapProps {
  lat: number;
  lng: number;
  label: string;
  height?: number;
  zoom?: number;
}

export function MerchantMap({ lat, lng, label, height = 220, zoom = 15 }: MerchantMapProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    let map: import("leaflet").Map | null = null;

    async function mount() {
      if (!containerRef.current) return;
      const L = await import("leaflet");
      // Leaflet CSS — Next bundles it with the app chunk.
      await import("leaflet/dist/leaflet.css");
      if (cancelled || !containerRef.current) return;

      // Default marker icons break under bundlers; use a simple divIcon.
      const pin = L.divIcon({
        className: "bdpay-map-pin",
        html: `<span class="bdpay-map-pin-dot" title="${label.replace(/"/g, "")}"></span>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
      });

      map = L.map(containerRef.current, {
        scrollWheelZoom: false,
        attributionControl: true,
      }).setView([lat, lng], zoom);

      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
      }).addTo(map);

      L.marker([lat, lng], { icon: pin }).addTo(map).bindPopup(label);
    }

    void mount();
    return () => {
      cancelled = true;
      if (map) {
        map.remove();
        map = null;
      }
    };
  }, [lat, lng, label, zoom]);

  return (
    <div className="merchant-map-wrap" style={{ height }}>
      <div ref={containerRef} className="merchant-map" style={{ height: "100%", width: "100%" }} />
      <p className="merchant-map-attrib subtle">OpenStreetMap · demo pin</p>
    </div>
  );
}
