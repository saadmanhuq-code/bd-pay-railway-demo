"""One-shot migration runner (compose service ``migration-runner``).

Applies every ``db/migrations/*.sql`` file in lexicographic (numbered) order
against ``DATABASE_URL``, exactly once, then exits 0. The compose file gates
the app containers on this service completing successfully, so the gateway
never boots against a half-migrated schema.

Discipline (IMPLEMENTATION.md "Migrations are append-only"):

- A ``schema_migrations`` bookkeeping table records (filename, sha256,
  applied_at). Re-runs skip files already applied with an identical hash.
- If a previously-applied file's content hash has CHANGED, the runner fails
  closed with exit 2 — editing an applied migration is refused, never
  silently re-applied. New behaviour belongs in a new numbered file.
- Each migration file runs inside its own transaction: it either lands
  whole or not at all, and the bookkeeping row commits atomically with it.

Connection settings come exclusively from the environment (``DATABASE_URL``);
no credential ever appears in this file or in its log output (the DSN itself
is never printed).
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(os.environ.get("BDPAY_MIGRATIONS_DIR", "/app/db/migrations"))
CONNECT_ATTEMPTS = 30
CONNECT_RETRY_SECONDS = 2

_BOOKKEEPING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    sha256     TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def _connect(dsn: str) -> psycopg.Connection:
    """Connect with bounded retries (covers the gap between the postgres
    container reporting healthy and accepting application connections).

    autocommit=True is LOAD-BEARING (deploy-rehearsal fix, 2026-06-13):
    psycopg3 defaults to autocommit=False, where the first bare ``execute``
    (the schema_migrations SELECT) silently opens an implicit outer
    transaction. Every per-file ``with conn.transaction():`` block then
    degrades to a SAVEPOINT inside it, and ``conn.close()`` rolls the outer
    transaction back — so NOTHING applied ever persisted, on failure or even
    on full success (observed on a fresh database: the runner printed
    "applied" for 90 files, failed at a gated file, and left an empty
    database; every re-run re-applied from 0001). With autocommit=True the
    per-file ``conn.transaction()`` blocks are real transactions committed
    file-by-file, which is the documented contract: each file lands whole
    together with its bookkeeping row, and a failed run resumes at the
    failing file instead of replaying history."""
    last_error: Exception | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return psycopg.connect(dsn, autocommit=True)
        except psycopg.OperationalError as exc:  # pragma: no cover - timing path
            last_error = exc
            print(
                f"migrations: database not ready (attempt {attempt}/{CONNECT_ATTEMPTS})",
                flush=True,
            )
            time.sleep(CONNECT_RETRY_SECONDS)
    raise SystemExit(f"migrations: could not connect to database: {last_error}")


def main() -> int:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        print("migrations: DATABASE_URL is required", file=sys.stderr)
        return 2
    if not MIGRATIONS_DIR.is_dir():
        print(f"migrations: directory not found: {MIGRATIONS_DIR}", file=sys.stderr)
        return 2

    files = sorted(p for p in MIGRATIONS_DIR.iterdir() if p.suffix == ".sql")
    if not files:
        print(f"migrations: no .sql files under {MIGRATIONS_DIR}", file=sys.stderr)
        return 2

    conn = _connect(dsn)
    try:
        with conn.transaction():
            conn.execute(_BOOKKEEPING_DDL)

        applied: dict[str, str] = {
            row[0]: row[1]
            for row in conn.execute("SELECT filename, sha256 FROM schema_migrations")
        }

        ran = 0
        for path in files:
            sql = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            previous = applied.get(path.name)
            if previous is not None:
                if previous != digest:
                    print(
                        f"migrations: REFUSED — {path.name} was already applied with a "
                        "different content hash; applied migrations are append-only "
                        "(add a new numbered file instead of editing this one)",
                        file=sys.stderr,
                    )
                    return 2
                continue  # already applied, identical content
            with conn.transaction():
                conn.execute(sql)  # type: ignore[arg-type]
                conn.execute(
                    "INSERT INTO schema_migrations (filename, sha256) VALUES (%s, %s)",
                    (path.name, digest),
                )
            ran += 1
            print(f"migrations: applied {path.name}", flush=True)

        print(
            f"migrations: complete — {ran} applied, {len(files) - ran} already current",
            flush=True,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
