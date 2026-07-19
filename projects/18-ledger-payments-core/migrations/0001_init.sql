-- The ledger schema. Five tables, one job each:
--   accounts          — who holds money (currency + overdraft policy)
--   transactions      — a balanced unit of money movement (Σ entries == 0)
--   entries           — the immutable, append-only source of truth for balances
--   idempotency_keys  — the durable record behind the Redis replay cache (V3)
--   webhook_outbox    — settlement events enqueued in the money transaction (V4)
--
-- Money is ALWAYS integer minor units (BIGINT), never a float. A balance is
-- DERIVED from entries (SUM(amount)); it is never a mutable column you decrement.
-- That immutability is what makes V2's concurrency story tractable and the whole
-- ledger auditable.

-- Postgres provides gen_random_uuid() via pgcrypto on modern builds; enable it so
-- ids need no coordination (unlike a shared BIGSERIAL sequence).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS accounts (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    name           TEXT        NOT NULL,
    currency       TEXT        NOT NULL,                 -- ISO 4217; all entries share it (V1)
    -- Can this account go below zero? A customer wallet is false (no overdraft, V2);
    -- a house/settlement account may be true. The invariant is enforced in code.
    allow_negative BOOLEAN     NOT NULL DEFAULT false,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A transaction groups the entries of one money movement. Its entries MUST sum to
-- zero (V1). It carries no amount of its own — the amount lives in the entries.
CREATE TABLE IF NOT EXISTS transactions (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        TEXT        NOT NULL DEFAULT 'transfer', -- transfer | reversal | ...
    reference   TEXT,                                    -- external ref / memo
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The heart of the ledger: immutable entries. One transfer = two rows (a debit and
-- a credit) whose signed `amount` sums to zero. Sign convention (credit +, debit -,
-- or vice-versa) is your V1 decision — record it in docs/18-design.md and be
-- consistent. There is intentionally NO update_at / no soft-delete: a correction is
-- a NEW reversing transaction, never an edit of a posted row.
CREATE TABLE IF NOT EXISTS entries (
    id             BIGSERIAL   PRIMARY KEY,
    transaction_id UUID        NOT NULL REFERENCES transactions(id),
    account_id     UUID        NOT NULL REFERENCES accounts(id),
    amount         BIGINT      NOT NULL,                 -- signed minor units
    currency       TEXT        NOT NULL,                 -- must match the account's (V1)
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TODO(V1): balance reads are `SELECT SUM(amount) FROM entries WHERE account_id=$1`.
-- Add the index that keeps that selective as entries grow to millions of rows
-- (an index on (account_id) — or (account_id, created_at) if you also page history).
-- Left out on purpose: measuring the before/after is a V1 lesson.

-- TODO(V2): consider a per-account concurrency guard. Depending on the isolation
-- strategy you pick (SERIALIZABLE vs SELECT ... FOR UPDATE), you may add a
-- `balance_cache` column or a lock row here — but only if the SPEC's derived-balance
-- invariant still holds. Don't add a mutable balance column that can drift from the
-- entries; that's the bug this project exists to prevent.

-- The durable idempotency record (V3). Redis caches the response for fast replay;
-- THIS table is the source of truth, so a cache miss re-reads it and still never
-- re-executes. `request_hash` binds the key to the request body: same key + a
-- different body is a conflict, not a silent replay of the old result.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key            TEXT        PRIMARY KEY,
    request_hash   TEXT        NOT NULL,                 -- fingerprint of the request body
    transaction_id UUID        REFERENCES transactions(id),
    status_code    INT,                                  -- the stored HTTP status
    response_body  JSONB,                                -- the stored response to replay
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at     TIMESTAMPTZ NOT NULL                  -- TTL; past this the key is "new"
);

-- The transactional outbox (V4). A settlement event is INSERTed here in the SAME
-- transaction as the posting, so a posting and its event commit atomically (no
-- event without a posting, no posting without an event). A background dispatcher
-- claims pending rows, signs + delivers them, and retries with backoff -> DLQ.
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id              BIGSERIAL   PRIMARY KEY,
    event_type      TEXT        NOT NULL,                -- e.g. transfer.settled
    payload         JSONB       NOT NULL,
    endpoint_url    TEXT        NOT NULL,
    state           TEXT        NOT NULL DEFAULT 'pending', -- pending|delivered|dead
    attempts        INT         NOT NULL DEFAULT 0,
    max_attempts    INT         NOT NULL DEFAULT 8,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- backoff pushes this out
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- TODO(V4): the dispatcher's claim query is roughly
--     SELECT ... FROM webhook_outbox
--     WHERE state = 'pending' AND next_attempt_at <= now()
--     ORDER BY next_attempt_at
--     FOR UPDATE SKIP LOCKED
--     LIMIT $1
-- (SKIP LOCKED so two dispatcher instances never grab the same event). Add the
-- partial index over (next_attempt_at) WHERE state = 'pending' that keeps it cheap,
-- and prove the difference. Left out on purpose — it's a V4 lesson.
