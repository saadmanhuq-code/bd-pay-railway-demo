import Link from "next/link";

// Friendly 404 for operators (FE QA 2026-09-28): the bare Next.js 404 gave no
// way back into the console. Bilingual inline (no client hooks needed).
export default function NotFound() {
  return (
    <main className="centered">
      <div className="login-card" role="alert">
        <h1>
          Page not found <span lang="bn">· পেজটি পাওয়া যায়নি</span>
        </h1>
        <p>This address isn&apos;t part of the ops console. Use the menu, or go back to the dashboard.</p>
        <p lang="bn">এই ঠিকানাটি অপস কনসোলের অংশ নয়। মেনু ব্যবহার করুন, অথবা ড্যাশবোর্ডে ফিরে যান।</p>
        <p>
          Looking for transactions? They are in the <Link href="/ledger">Ledger</Link>; payment exceptions are in{" "}
          <Link href="/cases">Cases</Link>.
        </p>
        <Link className="btn btn-primary" href="/">
          Back to dashboard · ড্যাশবোর্ডে ফিরে যান
        </Link>
      </div>
    </main>
  );
}
