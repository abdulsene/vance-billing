-- Vance Credit — Enrollment storage (Postgres / Supabase)
-- Apply once, then set DATABASE_URL for the enrollment service to use PostgresStorage.
--
-- One row per enrolled client. The billing gate's /billing/due reads
-- client_id, plan_tier, monthly_amount, customer_vault_id, cycle, and contact
-- (reconstructed from email + phone). NOTHING here implies a charge — the card
-- is only vaulted at enrollment; the gate charges later, per cycle.

create table if not exists vc_clients (
    client_id         text        primary key,
    plan_tier         text        not null,          -- 'dispute' | 'complete'
    monthly_amount    integer     not null,          -- 99 | 149 (charged later by the gate)
    customer_vault_id text        not null,          -- NMI Customer Vault id (card never stored here)
    cycle             text        not null,          -- enrollment cycle, e.g. '2026-06'
    email             text        not null,
    phone             text        not null default '',
    -- Billing address (also stored on the NMI vault so AVS runs at charge time).
    name              text        not null default '',
    address1          text        not null default '',
    address2          text        not null default '',
    city              text        not null default '',
    state             text        not null default '',
    zip               text        not null default '',
    status            text        not null default 'active',
    created_at        timestamptz not null default now()
);

-- Migration for existing databases (adds the billing-address / AVS columns).
-- Safe to run repeatedly.
alter table vc_clients add column if not exists name     text not null default '';
alter table vc_clients add column if not exists address1 text not null default '';
alter table vc_clients add column if not exists address2 text not null default '';
alter table vc_clients add column if not exists city     text not null default '';
alter table vc_clients add column if not exists state    text not null default '';
alter table vc_clients add column if not exists zip      text not null default '';

-- The gate selects clients due this cycle that it hasn't billed yet.
create index if not exists vc_clients_cycle on vc_clients (cycle);
create index if not exists vc_clients_status on vc_clients (status);
