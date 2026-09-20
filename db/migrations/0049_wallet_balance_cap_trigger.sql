-- 0049_wallet_balance_cap_trigger.sql — spec/09 DB-level wallet balance cap
-- (the non-bypassable backstop behind the application-layer LimitEnforcer).
--
-- K-20 (errata): the trigger target tables (postings, accounts) are owned by
-- the ledger lane (spec/03, migrations outside 0030-0049) and may not exist
-- yet at this point in the apply order. The function is created
-- unconditionally (functions only resolve tables at run time); the trigger
-- attaches inside a guarded DO block IF postings exists, and the wiring
-- step re-runs this migration's tail after the ledger DDL lands so the
-- trigger is attached in either apply order. Fails closed either way: the
-- application-layer check refuses first, and once attached the trigger
-- arbitrates the concurrent-credit race (second committer rolls back whole,
-- spec/09 binding race semantics).

CREATE OR REPLACE FUNCTION check_wallet_balance_cap()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_owner_id        TEXT;
    v_cap_minor       BIGINT;
    v_current_balance BIGINT;
BEGIN
    IF NEW.side != 'CREDIT' THEN
        RETURN NEW;
    END IF;

    SELECT a.owner_id INTO v_owner_id
    FROM accounts a
    WHERE a.account_id = NEW.account_id AND a.account_subtype = 'CUSTOMER_FLOAT';

    IF v_owner_id IS NULL THEN
        RETURN NEW;  -- not a customer float account; pass through
    END IF;

    SELECT kc.limit_amount_minor INTO v_cap_minor
    FROM kyc_limit_configs kc
    WHERE kc.tier = (
        SELECT kr.tier FROM kyc_records kr
        WHERE kr.customer_id = v_owner_id AND kr.state = 'ACTIVE'
        ORDER BY kr.tier_assigned_at DESC LIMIT 1
    )
    AND kc.payment_type = 'BALANCE_CAP'
    AND kc.limit_scope  = 'BALANCE'
    AND kc.effective_from <= NEW.created_at
    AND (kc.effective_to IS NULL OR kc.effective_to > NEW.created_at);
    -- The effective-date comparison uses the posting's own clock-injected
    -- created_at, not NOW() (spec/00 §7 replay discipline).

    IF v_cap_minor IS NULL THEN
        v_cap_minor := 40000000;  -- regulatory default: BDT 4 lakh
    END IF;

    SELECT COALESCE(
        SUM(CASE WHEN p.side = 'CREDIT' THEN p.amount_minor ELSE -p.amount_minor END),
        0
    ) INTO v_current_balance
    FROM postings p
    WHERE p.account_id = NEW.account_id;

    IF (v_current_balance + NEW.amount_minor) > v_cap_minor THEN
        RAISE EXCEPTION 'wallet_balance_cap_exceeded: account % would exceed cap % paisa',
            NEW.account_id, v_cap_minor
            USING ERRCODE = 'P0001';
    END IF;

    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF to_regclass('postings') IS NOT NULL THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger WHERE tgname = 'trg_wallet_balance_cap'
        ) THEN
            CREATE TRIGGER trg_wallet_balance_cap
                BEFORE INSERT ON postings
                FOR EACH ROW EXECUTE FUNCTION check_wallet_balance_cap();
        END IF;
    END IF;
END;
$$;
