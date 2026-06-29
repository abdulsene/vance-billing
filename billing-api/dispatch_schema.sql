-- Vance Credit — Billing-API storage (Postgres / Supabase)
-- Apply once. This service READS vc_clients (owned by the enrollment service)
-- and OWNS the two tables below. Set DATABASE_URL to use PostgresStorage.

-- Idempotency ledger: one row per (client, cycle) once a charge has succeeded.
-- /billing/due excludes any client whose current cycle is already in here.
create table if not exists vc_billed_cycles (
    client_id  text        not null,
    cycle      text        not null,          -- billing cycle, e.g. '2026-06'
    txn_id     text,                          -- NMI transaction that paid for it
    billed_at  timestamptz not null default now(),
    primary key (client_id, cycle)
);

-- Dispatch rounds: written when a dispute round is mailed (dispatch system / CRC
-- mail step). /dispatch/round-status reads this to tell the gate whether a round
-- was documented this cycle.
create table if not exists vc_dispatch_rounds (
    client_id  text        not null,
    cycle      text        not null,
    round_id   text,
    mailed_at  timestamptz,
    primary key (client_id, cycle)
);
