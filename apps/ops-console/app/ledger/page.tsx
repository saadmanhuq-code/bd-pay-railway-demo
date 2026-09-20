"use client";

// Read-only ledger viewer: journal entries with postings drill-down, chain
// entries, chain verify status banner. No mutations exist on this screen.

import React, { useEffect, useState } from "react";
import { Shell } from "@/components/Shell";
import { StateBadge } from "@/components/Badges";
import { DualLabel } from "@/lib/i18n/DualLabel";
import { getChainVerifyStatus, listChainEntries, listJournalEntries } from "@/lib/api/client";
import type { ChainVerifyStatus, JournalEntry, LedgerChainEntry } from "@/lib/api/types";
import { formatBdt, formatTs, shortId } from "@/lib/format";
import { useLang } from "@/lib/i18n/LangContext";

export default function LedgerPage() {
  const { t } = useLang();
  const [journals, setJournals] = useState<JournalEntry[]>([]);
  const [chain, setChain] = useState<LedgerChainEntry[]>([]);
  const [verify, setVerify] = useState<ChainVerifyStatus | null>(null);
  const [openJournal, setOpenJournal] = useState<string | null>(null);

  useEffect(() => {
    listJournalEntries({}).then((r) => setJournals(r.data)).catch(() => setJournals([]));
    listChainEntries({}).then((r) => setChain(r.data)).catch(() => setChain([]));
    getChainVerifyStatus().then(setVerify).catch(() => setVerify(null));
  }, []);

  return (
    <Shell>
      <h1>{t("nav_ledger")}</h1>

      {verify ? (
        <p className={`notice ${verify.status === "FAILED" ? "notice-danger" : ""}`}>
          <DualLabel k={verify.status === "VERIFIED" ? "chain_verified" : "chain_failed"} /> — {verify.entries_checked} entries ·
          last verified {formatTs(verify.last_verified_at)}
          {verify.first_bad_seq !== null ? ` · first bad seq ${verify.first_bad_seq}` : ""}
        </p>
      ) : null}

      <section className="panel">
        <h2>Journal entries</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Journal</th>
              <th>Description</th>
              <th>Subject</th>
              <th>Posted</th>
              <th>Postings</th>
            </tr>
          </thead>
          <tbody>
            {journals.map((j) => (
              <React.Fragment key={j.journal_id}>
                <tr className="clickable" onClick={() => setOpenJournal(openJournal === j.journal_id ? null : j.journal_id)}>
                  <td className="mono">{shortId(j.journal_id, 18)}</td>
                  <td>{j.description}</td>
                  <td>
                    {j.subject_type} <span className="mono subtle">{shortId(j.subject_id, 12)}</span>
                  </td>
                  <td>{formatTs(j.posted_at)}</td>
                  <td>{j.postings.length}</td>
                </tr>
                {openJournal === j.journal_id ? (
                  <tr>
                    <td colSpan={5}>
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Posting</th>
                            <th>Account</th>
                            <th>Side</th>
                            <th>Amount</th>
                          </tr>
                        </thead>
                        <tbody>
                          {j.postings.map((post) => (
                            <tr key={post.posting_id}>
                              <td className="mono">{shortId(post.posting_id, 18)}</td>
                              <td className="mono">{post.account}</td>
                              <td>
                                <StateBadge value={post.side} />
                              </td>
                              <td>{formatBdt(post.amount_minor)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </td>
                  </tr>
                ) : null}
              </React.Fragment>
            ))}
          </tbody>
        </table>
      </section>

      <section className="panel">
        <h2>Hash chain entries</h2>
        <table className="data-table">
          <thead>
            <tr>
              <th>Seq</th>
              <th>Journal</th>
              <th>Entry hash</th>
              <th>Prev hash</th>
              <th>Chained at</th>
            </tr>
          </thead>
          <tbody>
            {chain.map((c) => (
              <tr key={c.chain_seq}>
                <td>{c.chain_seq}</td>
                <td className="mono">{shortId(c.journal_id, 18)}</td>
                <td className="mono">{shortId(c.entry_hash, 24)}</td>
                <td className="mono">{shortId(c.prev_hash, 24)}</td>
                <td>{formatTs(c.chained_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </Shell>
  );
}
