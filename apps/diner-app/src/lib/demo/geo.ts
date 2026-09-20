// Geolocation helpers for EatClub-style "near me" browse.
// Distances are great-circle (haversine) in kilometres.

export interface LatLng {
  lat: number;
  lng: number;
}

/** Earth-radius haversine distance in km. */
export function haversineKm(a: LatLng, b: LatLng): number {
  const R = 6371;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(b.lat - a.lat);
  const dLng = toRad(b.lng - a.lng);
  const lat1 = toRad(a.lat);
  const lat2 = toRad(b.lat);
  const h =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLng / 2) * Math.sin(dLng / 2);
  return R * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
}

/** Format for cards: "0.8 km" / "1.2 km" / "12 km". */
export function formatDistanceKm(km: number, lang: "bn" | "en"): string {
  const rounded = km < 10 ? Math.round(km * 10) / 10 : Math.round(km);
  if (lang === "bn") {
    const bnDigits = String(rounded).replace(/\d/g, (d) => "০১২৩৪৫৬৭৮৯"[Number(d)]!);
    return `${bnDigits} কিমি`;
  }
  return `${rounded} km`;
}

/** Dhaka city centre — used when geolocation is denied (browse still works). */
export const DHAKA_CENTRE: LatLng = { lat: 23.7808, lng: 90.4074 };
