-- Vance Credit — Manual movement (Path B). Apply alongside verdict-service/schema.sql.
-- One row per (client, cycle); reviewer-confirmed favorable changes captured at re-import.
create table if not exists vc_manual_movements (
    client_id  text        not null,
    cycle      text        not null,          -- billing cycle, e.g. '2026-06'
    entries    jsonb       not null,          -- list of {bureau,item_type,creditor,account_mask,kind,target}
    updated_at timestamptz not null default now(),
    primary key (client_id, cycle)
);
