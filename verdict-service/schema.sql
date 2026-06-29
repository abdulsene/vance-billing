-- Vance Credit — Movement Verdict storage (Postgres / Supabase)
-- Apply once, then set DATABASE_URL for the service to use PostgresStorage.

create table if not exists vc_snapshots (
    client_id   text        not null,
    cycle       text        not null,          -- e.g. '2026-06'
    items       jsonb       not null,          -- list of tradeline/collection/inquiry items
    taken_at    timestamptz not null default now(),
    primary key (client_id, cycle)
);
create index if not exists vc_snapshots_prev
    on vc_snapshots (client_id, cycle desc);

create table if not exists vc_letters (
    client_id   text        not null,
    cycle       text        not null,
    outcomes    jsonb       not null,          -- list of {bureau,item_type,creditor,account_mask,outcome}
    received_at timestamptz not null default now(),
    primary key (client_id, cycle)
);

-- The dedup ledger: a change_id is billed at most once, ever.
create table if not exists vc_credited_changes (
    client_id   text        not null,
    change_id   text        not null,          -- e.g. 'EQ|collection|abc...|deletion|deleted'
    txn_id      text,                          -- NMI transaction that paid for it
    credited_at timestamptz not null default now(),
    primary key (client_id, change_id)
);
