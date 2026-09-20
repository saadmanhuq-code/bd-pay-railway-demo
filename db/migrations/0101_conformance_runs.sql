-- 0101_conformance_runs.sql — spec/19 PSO-1 conformance runs (append-only).
--
-- One bidirectional conformance-suite execution per row (pso-conf-v1,
-- directions OUTBOUND/INBOUND). FSM: QUEUED -> RUNNING -> PASSED | FAILED |
-- ERRORED (terminal), refusal-first; status advances only via the FSM
-- function below (spec/10 webhook_fsm_advance pattern) — the app role never
-- UPDATEs the row directly.
--
-- Errata P19-3: QUEUED -> ERRORED (evidence_hash_mismatch) is an additive
-- transition the spec/19 FSM-2 table omits but its failure-modes table
-- requires ("Run ERRORED before RUNNING" on a forged/garbled INBOUND report).

CREATE TABLE conformance_runs (
  conformance_run_id TEXT PRIMARY KEY,            -- confr_<sha256_canonical({participant_id, direction, suite_version, started_at})[:24]>
  participant_id     TEXT NOT NULL REFERENCES participants(participant_id),
  direction          TEXT NOT NULL CHECK (direction IN ('OUTBOUND','INBOUND')),
  suite_version      TEXT NOT NULL,               -- 'pso-conf-v1'
  status             TEXT NOT NULL DEFAULT 'QUEUED'
    CHECK (status IN ('QUEUED','RUNNING','PASSED','FAILED','ERRORED')),
  checks             JSONB NOT NULL DEFAULT '[]'::jsonb,  -- [{check_id, verdict, evidence_hash}] (PCONF-01..06)
  evidence_pointer   TEXT,                        -- INBOUND: participant-submitted report object
  evidence_sha256    TEXT,                        -- recomputed-and-matched before RUNNING
  report_pointer     TEXT,                        -- our deterministic report (spec/10 file+hash pattern)
  report_hash        TEXT,
  triggered_by       TEXT NOT NULL,
  started_at         TIMESTAMPTZ,
  finished_at        TIMESTAMPTZ,
  created_at         TIMESTAMPTZ NOT NULL,
  schema_version     SMALLINT NOT NULL DEFAULT 1
);
CREATE INDEX idx_confr_participant ON conformance_runs (participant_id, created_at DESC);

-- Refusal-first FSM advance: the only sanctioned mutation path. Result
-- columns (checks/report/timestamps) land atomically with the status flip;
-- COALESCE keeps already-written values when a later transition passes NULL.
CREATE OR REPLACE FUNCTION conformance_fsm_advance(
    p_run_id         TEXT,
    p_from           TEXT,
    p_to             TEXT,
    p_checks         JSONB       DEFAULT NULL,
    p_report_pointer TEXT        DEFAULT NULL,
    p_report_hash    TEXT        DEFAULT NULL,
    p_started_at     TIMESTAMPTZ DEFAULT NULL,
    p_finished_at    TIMESTAMPTZ DEFAULT NULL
) RETURNS VOID AS $$
DECLARE
    v_updated INTEGER;
BEGIN
    IF NOT (
        (p_from = 'QUEUED'  AND p_to = 'RUNNING') OR
        (p_from = 'QUEUED'  AND p_to = 'ERRORED') OR   -- P19-3 evidence_hash_mismatch
        (p_from = 'RUNNING' AND p_to IN ('PASSED','FAILED','ERRORED'))
    ) THEN
        RAISE EXCEPTION 'conformance_runs: transition % -> % is not in the table (refusal-first)',
            p_from, p_to;
    END IF;
    UPDATE conformance_runs
       SET status         = p_to,
           checks         = COALESCE(p_checks, checks),
           report_pointer = COALESCE(p_report_pointer, report_pointer),
           report_hash    = COALESCE(p_report_hash, report_hash),
           started_at     = COALESCE(p_started_at, started_at),
           finished_at    = COALESCE(p_finished_at, finished_at)
     WHERE conformance_run_id = p_run_id AND status = p_from;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> 1 THEN
        RAISE EXCEPTION 'conformance_runs: run % is not in state % (stale transition refused)',
            p_run_id, p_from;
    END IF;
END;
$$ LANGUAGE plpgsql;
