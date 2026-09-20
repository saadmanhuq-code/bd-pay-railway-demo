-- 0003_approval_requests.sql — the two-eyes spine (spec/15 §Data model
-- APPROVAL REQUESTS; FSM spec/15 §State machines). Platform-owned engine:
-- bdpay/platform/approval.py.
--
-- The initiator can never approve their own request: enforced in the
-- application AND by the CHECK below (defence in depth). One live request
-- per (action_type, subject_id) via the partial unique index — no racing
-- duplicate approvals.

CREATE TABLE approval_requests (
    approval_request_id TEXT        PRIMARY KEY,          -- appr_<sha256(action+subject+payload_hash+created_at)[:24]>
    action_type         TEXT        NOT NULL
        CHECK (action_type IN (
            'refund_over_threshold', 'merchant_activation', 'merchant_edd_clearance',
            'kyc_manual_review', 'settlement_batch_retry_large', 'settlement_release_override',
            'ledger_adjustment', 'mdr_fee_rule_change', 'aml_case_close', 'str_withdrawal',
            'sanctions_clear_or_unfreeze', 'camlco_freeze', 'camlco_unfreeze',
            'rule_pack_promotion', 'signing_key_ceremony', 'connector_mode_to_production',
            'connector_manual_result', 'rail_dictionary_activation', 'npsb_session_manual_action',
            'compensation_resolution', 'dispute_resolution_money', 'bulk_disbursement_release',
            'operator_create_or_role_change', 'bb_incident_report_release'
        )),                                               -- generated from the spec/15 catalogue
    subject_type        TEXT        NOT NULL,             -- canonical entity name
    subject_id          TEXT        NOT NULL,
    payload             JSONB       NOT NULL,             -- action params; PII-redacted; amounts *_minor ints
    payload_hash        TEXT        NOT NULL,             -- sha256(canonical_json(payload)) — what is approved
    reason              TEXT        NOT NULL,
    state               TEXT        NOT NULL DEFAULT 'PENDING_SECOND_APPROVER'
        CHECK (state IN ('PENDING_SECOND_APPROVER', 'APPROVED', 'APPROVED_EXECUTION_FAILED',
                         'REJECTED', 'EXPIRED', 'WITHDRAWN')),
    initiator_id        TEXT        NOT NULL,             -- oper_<...> or service principal
    approver_id         TEXT,
    decision_reason     TEXT,
    threshold_minor     BIGINT,                           -- catalogue threshold snapshot at creation (paisa)
    expires_at          TIMESTAMPTZ NOT NULL,             -- created_at + 24h
    decided_at          TIMESTAMPTZ,
    executed_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    schema_version      SMALLINT    NOT NULL DEFAULT 1,
    CHECK (approver_id IS NULL OR approver_id <> initiator_id)   -- initiator != approver, DB-enforced
);

-- one live request per (action_type, subject): no racing duplicate approvals
CREATE UNIQUE INDEX idx_appr_live ON approval_requests (action_type, subject_id)
    WHERE state IN ('PENDING_SECOND_APPROVER', 'APPROVED_EXECUTION_FAILED');
CREATE INDEX idx_appr_queue ON approval_requests (state, created_at)
    WHERE state = 'PENDING_SECOND_APPROVER';
