"use client";

// Client-side SHA-256 of a selected file (KYB documents, dispute evidence).
// The portal sends pointer + sha256 — the bytes themselves go to object
// storage out of band; the mock accepts the digest as-is.

export async function sha256OfFile(file: File): Promise<string> {
  const buf = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", buf);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export function evidencePointer(bucket: string, fileName: string, sha256: string): string {
  const safe = fileName.replace(/[^a-zA-Z0-9._-]/g, "_");
  return `s3://${bucket}/${sha256.slice(0, 12)}/${safe}`;
}
