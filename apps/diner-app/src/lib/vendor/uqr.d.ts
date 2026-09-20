/** Ambient types for vendored uqr (MIT). */
declare module "@/lib/vendor/uqr.mjs" {
  export type ErrorCorrectionLevel = "L" | "M" | "Q" | "H";

  export function renderSVG(
    data: string,
    options?: {
      ecc?: ErrorCorrectionLevel;
      border?: number;
      pixelSize?: number;
    },
  ): string;

  export function encode(
    data: string,
    options?: {
      ecc?: ErrorCorrectionLevel;
      border?: number;
    },
  ): { size: number; data: Uint8Array };
}
