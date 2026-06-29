"""
Storage for the enrollment service — same shape as the verdict service's layer.

InMemoryStorage  -> tests and local runs.
PostgresStorage  -> Supabase/Postgres-backed (set DATABASE_URL). See clients_schema.sql.

The Postgres table stores contact as flat email/phone columns; this layer
reconstructs the nested `contact` object on read so the loaded Client matches
exactly what the billing gate expects.
"""
from __future__ import annotations
from typing import Optional, Protocol

from enroll_core import Client


class Storage(Protocol):
    def save_client(self, client: Client) -> None: ...
    def get_client(self, client_id: str) -> Optional[Client]: ...
    def list_clients(self) -> list: ...


# --------------------------------------------------------------------------- #

class InMemoryStorage:
    def __init__(self):
        self._clients: dict = {}        # client_id -> Client

    def clear(self):
        self._clients.clear()

    def save_client(self, client: Client) -> None:
        self._clients[client.client_id] = client

    def get_client(self, client_id: str) -> Optional[Client]:
        return self._clients.get(client_id)

    def list_clients(self) -> list:
        return list(self._clients.values())


# --------------------------------------------------------------------------- #

class PostgresStorage:
    """
    Supabase/Postgres-backed storage. Requires `psycopg` and DATABASE_URL.
    Apply clients_schema.sql once before use. Not exercised by the test suite.
    """
    def __init__(self, dsn: str):
        import psycopg  # noqa: F401  (imported lazily so tests don't need it)
        self._dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True)

    def save_client(self, client: Client) -> None:
        with self._conn() as c:
            c.execute(
                """insert into vc_clients
                       (client_id, plan_tier, monthly_amount, customer_vault_id,
                        cycle, email, phone, status, created_at)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (client_id) do update set
                       plan_tier=excluded.plan_tier,
                       monthly_amount=excluded.monthly_amount,
                       customer_vault_id=excluded.customer_vault_id,
                       cycle=excluded.cycle,
                       email=excluded.email,
                       phone=excluded.phone,
                       status=excluded.status""",
                (client.client_id, client.plan_tier, client.monthly_amount,
                 client.customer_vault_id, client.cycle,
                 client.contact.get("email", ""), client.contact.get("phone", ""),
                 client.status, client.created_at))

    def get_client(self, client_id: str) -> Optional[Client]:
        with self._conn() as c:
            row = c.execute(
                """select client_id, plan_tier, monthly_amount, customer_vault_id,
                          cycle, email, phone, status, created_at
                   from vc_clients where client_id=%s""",
                (client_id,)).fetchone()
        return _client_from_row(row) if row else None

    def list_clients(self) -> list:
        with self._conn() as c:
            rows = c.execute(
                """select client_id, plan_tier, monthly_amount, customer_vault_id,
                          cycle, email, phone, status, created_at
                   from vc_clients""").fetchall()
        return [_client_from_row(r) for r in rows]


def _client_from_row(row) -> Client:
    return Client(
        client_id=row[0], plan_tier=row[1], monthly_amount=row[2],
        customer_vault_id=row[3], cycle=row[4],
        contact={"email": row[5], "phone": row[6]},
        status=row[7], created_at=str(row[8]))
