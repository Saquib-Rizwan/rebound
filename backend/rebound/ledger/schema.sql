-- Rebound audit ledger.
--
-- Design rule: this is an append-only record of what the agent believed, what it
-- considered, what it chose and what happened. Nothing is ever updated in place
-- except an outcome landing against an existing decision. If you cannot
-- reconstruct why the agent did something from these five tables alone, the
-- audit trail has failed.

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    policy_version  TEXT NOT NULL,
    provider        TEXT,
    model           TEXT,
    dry_run         INTEGER NOT NULL,
    batch_size      INTEGER NOT NULL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id     TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    payment_id      TEXT NOT NULL,
    merchant_id     TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,

    -- diagnosis, with full provenance so a wrong action can be traced to the
    -- component that caused it rather than to "the AI"
    failure_class   TEXT NOT NULL,
    confidence      REAL NOT NULL,
    diag_source     TEXT NOT NULL,
    rule_id         TEXT,
    rationale       TEXT,
    flags           TEXT,          -- JSON array of sanitizer findings
    llm_cost_usd    REAL DEFAULT 0,

    -- chosen action
    intervention    TEXT NOT NULL,
    delay_hours     REAL NOT NULL,
    channel         TEXT,
    target_rail     TEXT,
    p_recover       REAL,
    gross_paise     REAL,
    cost_paise      REAL,
    annoyance_paise REAL,
    ev_paise        REAL,

    guardrails      TEXT,          -- JSON array of guardrail ids that fired
    policy_version  TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    explanation     TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_payment  ON decisions(payment_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run      ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_class    ON decisions(failure_class);
CREATE INDEX IF NOT EXISTS idx_decisions_action   ON decisions(intervention);

-- Every option the engine priced, including the ones it was not allowed to take.
-- This is the table that answers "why did you NOT do X", which is the question a
-- merchant actually asks.
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     TEXT NOT NULL REFERENCES decisions(decision_id),
    intervention    TEXT NOT NULL,
    delay_hours     REAL NOT NULL,
    channel         TEXT,
    p_recover       REAL,
    cost_paise      REAL,
    annoyance_paise REAL,
    ev_paise        REAL,
    blocked_by      TEXT,
    chosen          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_candidates_decision ON candidates(decision_id);
CREATE INDEX IF NOT EXISTS idx_candidates_blocked  ON candidates(blocked_by);

CREATE TABLE IF NOT EXISTS executions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       TEXT NOT NULL REFERENCES decisions(decision_id),
    payment_id        TEXT NOT NULL,
    intervention      TEXT NOT NULL,
    executed          INTEGER NOT NULL,
    -- Client-side idempotency. Razorpay does not offer an idempotency header on
    -- the payment-link APIs, so uniqueness is enforced here: the same decision
    -- replayed cannot send a second message.
    idempotency_key   TEXT NOT NULL UNIQUE,
    external_ref      TEXT,
    scheduled_for     TEXT,
    error             TEXT,
    dry_run           INTEGER NOT NULL,
    executed_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_executions_payment ON executions(payment_id);

CREATE TABLE IF NOT EXISTS outcomes (
    payment_id          TEXT PRIMARY KEY,
    run_id              TEXT REFERENCES runs(run_id),
    recovered           INTEGER NOT NULL,
    recovered_paise     INTEGER NOT NULL DEFAULT 0,
    hours_to_recovery   REAL,
    customer_contacts   INTEGER NOT NULL DEFAULT 0,
    action_cost_paise   REAL NOT NULL DEFAULT 0,
    source              TEXT NOT NULL          -- 'simulated' or 'observed'
);
