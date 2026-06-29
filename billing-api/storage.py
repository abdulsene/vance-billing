"""
Storage for the billing-api — same Protocol + InMemory + Postgres style as the
verdict and enrollment services.

It READS the vc_clients table the enrollment service writes (never writes it),
and OWNS two new tables: vc_billed_cycles (the idempotency ledger) and
vc_dispatch_rounds. See dispatch_schema.sql.
"""
from __future__ import annotations
from typing import Optional, Protocol

from billing_core import Client


class Storage(Protocol):
    def get_due_clients(self, cycle: str) -> list: ...
    def is_billed(self, client_id: str, cycle: str) -> bool: ...
    def mark_billed(self, client_id: str, cycle: str, transaction_id: Optional[str]) -> None: ...
    def record_round(self, client_id: str, cycle: str, round_id: str, mailed_at: str) -> None: ...
    def get_round(self, client_id: str, cycle: str) -> Optional[dict]: ...


# --------------------------------------------------------------------------- #

class InMemoryStorage:
    def __init__(self):
        self._clients: dict = {}        # client_id -> Client (shared vc_clients view)
        self._billed: set = set()       # {(client_id, cycle)}
        self._rounds: dict = {}         # (client_id, cycle) -> {round_id, mailed_at}

    def clear(self):
        self._clients.clear(); self._billed.clear(); self._rounds.clear()

    # vc_clients is written by enrollment; tests/local seed it here.
    def save_client(self, client: Client) -> None:
        self._clients[client.client_id] = client

    def get_due_clients(self, cycle: str) -> list:
        return [c for c in self._clients.values()
                if c.status == "active" and (c.client_id, cycle) not in self._billed]

    def is_billed(self, client_id: str, cycle: str) -> bool:
        return (client_id, cycle) in self._billed

    def mark_billed(self, client_id: str, cycle: str, transaction_id=None) -> None:
        self._billed.add((client_id, cycle))   # set => "on conflict do nothing"

    def record_round(self, client_id: str, cycle: str, round_id: str, mailed_at: str) -> None:
        self._rounds[(client_id, cycle)] = {"round_id": round_id, "mailed_at": mailed_at}

    def get_round(self, client_id: str, cycle: str) -> Optional[dict]:
        return self._rounds.get((client_id, cycle))


# --------------------------------------------------------------------------- #

class PostgresStorage:
    """
    Supabase/Postgres-backed. Requires `psycopg` and DATABASE_URL. Reads
    vc_clients (from enrollment); apply dispatch_schema.sql once for the two
    tables this service owns. Not exercised by the test suite.
    """
    def __init__(self, dsn: str):
        import psycopg  # noqa: F401  (lazy import so tests don't need it)
        self._dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True)

    def get_due_clients(self, cycle: str) -> list:
        with self._conn() as c:
            rows = c.execute(
                """select client_id, plan_tier, monthly_amount, customer_vault_id,
                          cycle, email, phone, status, created_at
                   from vc_clients cl
                   where cl.status = 'active'
                     and not exists (
                         select 1 from vc_billed_cycles b
                         where b.client_id = cl.client_id and b.cycle = %s)""",
                (cycle,)).fetchall()
        return [_client_from_row(r) for r in rows]

    def is_billed(self, client_id: str, cycle: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "select 1 from vc_billed_cycles where client_id=%s and cycle=%s",
                (client_id, cycle)).fetchone()
        return row is not None

    def mark_billed(self, client_id: str, cycle: str, transaction_id=None) -> None:
        with self._conn() as c:
            c.execute(
                """insert into vc_billed_cycles (client_id, cycle, txn_id)
                   values (%s, %s, %s)
                   on conflict (client_id, cycle) do nothing""",
                (client_id, cycle, transaction_id))

    def record_round(self, client_id: str, cycle: str, round_id: str, mailed_at: str) -> None:
        with self._conn() as c:
            c.execute(
                """insert into vc_dispatch_rounds (client_id, cycle, round_id, mailed_at)
                   values (%s, %s, %s, %s)
                   on conflict (client_id, cycle) do update set
                       round_id = excluded.round_id,
                       mailed_at = excluded.mailed_at""",
                (client_id, cycle, round_id, mailed_at))

    def get_round(self, client_id: str, cycle: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute(
                "select round_id, mailed_at from vc_dispatch_rounds where client_id=%s and cycle=%s",
                (client_id, cycle)).fetchone()
        return {"round_id": row[0], "mailed_at": str(row[1]) if row[1] is not None else None} if row else None


def _client_from_row(row) -> Client:
    return Client(
        client_id=row[0], plan_tier=row[1], monthly_amount=row[2],
        customer_vault_id=row[3], cycle=row[4],
        contact={"email": row[5], "phone": row[6]},
        status=row[7], created_at=str(row[8]))
