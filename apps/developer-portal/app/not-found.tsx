import Link from "next/link";

// Friendly 404 for merchants (FE QA 2026-09-28): the bare Next.js 404 gave no
// way back into the portal. Bilingual inline (no client hooks needed).
export default function NotFound() {
  return (
    <main className="centered">
      <div className="login-card" role="alert">
        <h1>
          Page not found <span lang="bn">· পেজটি পাওয়া যায়নি</span>
        </h1>
        <p>This address isn&apos;t part of the BD-PAY developer portal. Use the menu, or go back to your dashboard.</p>
        <p lang="bn">এই ঠিকানাটি বিডি-পে ডেভেলপার পোর্টালের অংশ নয়। মেনু ব্যবহার করুন, অথবা ড্যাশবোর্ডে ফিরে যান।</p>
        <p>
          <Link className="btn btn-primary" href="/dashboard">
            Back to dashboard · ড্যাশবোর্ডে ফিরে যান
          </Link>{" "}
          <Link href="/docs">API docs</Link>
        </p>
      </div>
    </main>
  );
}
