// Display name for the signed-in portal user, derived from the login email
// (FE QA 2026-09-29: the portal always greeted "Rahim Uddin").

/** "salma.akter@shop.example" -> "Salma Akter"; "qa_pm2@x" -> "Qa Pm2". */
export function displayNameFromEmail(email: string): string {
  const local = email.split("@")[0] ?? "";
  const words = local
    .split(/[._+\-]+/)
    .map((w) => w.replace(/[^\p{L}\p{M}\p{N}]/gu, ""))
    .filter(Boolean)
    .slice(0, 4);
  if (words.length === 0) return email;
  return words.map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
}
