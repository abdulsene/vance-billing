"""Storage for the verdict service — InMemory + Postgres, now with manual movement."""
from __future__ import annotations
from typing import Optional, Protocol
from verdict_core import Item, Snapshot, LetterOutcome


def _snapshot_from_payload(client_id, cycle, items) -> Snapshot:
    return Snapshot(client_id=client_id, cycle=cycle, items=[Item(**i) for i in items])


class Storage(Protocol):
    def save_snapshot(self, snapshot: Snapshot) -> None: ...
    def get_snapshot(self, client_id: str, cycle: str) -> Optional[Snapshot]: ...
    def get_previous_snapshot(self, client_id: str, cycle: str) -> Optional[Snapshot]: ...
    def save_letters(self, client_id: str, cycle: str, outcomes: list) -> None: ...
    def get_letters(self, client_id: str, cycle: str) -> list: ...
    def save_manual(self, client_id: str, cycle: str, entries: list) -> None: ...
    def get_manual(self, client_id: str, cycle: str) -> list: ...
    def get_credited(self, client_id: str) -> set: ...
    def add_credited(self, client_id: str, change_ids: list) -> None: ...


class InMemoryStorage:
    def __init__(self):
        self._snaps, self._letters, self._manual, self._credited = {}, {}, {}, {}

    def clear(self):
        self._snaps.clear(); self._letters.clear()
        self._manual.clear(); self._credited.clear()

    def save_snapshot(self, snapshot):
        self._snaps[(snapshot.client_id, snapshot.cycle)] = snapshot

    def get_snapshot(self, client_id, cycle):
        return self._snaps.get((client_id, cycle))

    def get_previous_snapshot(self, client_id, cycle):
        prior = [c for (cid, c) in self._snaps if cid == client_id and c < cycle]
        return self._snaps[(client_id, max(prior))] if prior else None

    def save_letters(self, client_id, cycle, outcomes):
        self._letters[(client_id, cycle)] = outcomes

    def get_letters(self, client_id, cycle):
        return self._letters.get((client_id, cycle), [])

    def save_manual(self, client_id, cycle, entries):
        # merge with anything already logged for this cycle (reviewer may add over time)
        self._manual.setdefault((client_id, cycle), [])
        self._manual[(client_id, cycle)].extend(entries)

    def get_manual(self, client_id, cycle):
        return self._manual.get((client_id, cycle), [])

    def get_credited(self, client_id):
        return set(self._credited.get(client_id, set()))

    def add_credited(self, client_id, change_ids):
        self._credited.setdefault(client_id, set()).update(change_ids)


class PostgresStorage:
    """Supabase/Postgres-backed. Requires psycopg + DATABASE_URL. Apply schema.sql."""
    def __init__(self, dsn: str):
        import psycopg  # noqa: F401
        self._dsn = dsn

    def _conn(self):
        import psycopg
        return psycopg.connect(self._dsn, autocommit=True)

    def save_snapshot(self, snapshot):
        import json
        items = [vars(i) for i in snapshot.items]
        with self._conn() as c:
            c.execute("""insert into vc_snapshots (client_id, cycle, items) values (%s,%s,%s)
                         on conflict (client_id, cycle) do update set items = excluded.items""",
                      (snapshot.client_id, snapshot.cycle, json.dumps(items)))

    def get_snapshot(self, client_id, cycle):
        with self._conn() as c:
            row = c.execute("select items from vc_snapshots where client_id=%s and cycle=%s",
                            (client_id, cycle)).fetchone()
        return _snapshot_from_payload(client_id, cycle, row[0]) if row else None

    def get_previous_snapshot(self, client_id, cycle):
        with self._conn() as c:
            row = c.execute("""select cycle, items from vc_snapshots
                               where client_id=%s and cycle < %s order by cycle desc limit 1""",
                            (client_id, cycle)).fetchone()
        return _snapshot_from_payload(client_id, row[0], row[1]) if row else None

    def save_letters(self, client_id, cycle, outcomes):
        import json
        with self._conn() as c:
            c.execute("""insert into vc_letters (client_id, cycle, outcomes) values (%s,%s,%s)
                         on conflict (client_id, cycle) do update set outcomes = excluded.outcomes""",
                      (client_id, cycle, json.dumps([vars(o) for o in outcomes])))

    def get_letters(self, client_id, cycle):
        with self._conn() as c:
            row = c.execute("select outcomes from vc_letters where client_id=%s and cycle=%s",
                            (client_id, cycle)).fetchone()
        return [LetterOutcome(**o) for o in row[0]] if row else []

    def save_manual(self, client_id, cycle, entries):
        import json
        with self._conn() as c:
            # append-merge: combine with any existing entries for the cycle
            row = c.execute("select entries from vc_manual_movements where client_id=%s and cycle=%s",
                            (client_id, cycle)).fetchone()
            combined = (row[0] if row else []) + list(entries)
            c.execute("""insert into vc_manual_movements (client_id, cycle, entries) values (%s,%s,%s)
                         on conflict (client_id, cycle) do update set entries = excluded.entries""",
                      (client_id, cycle, json.dumps(combined)))

    def get_manual(self, client_id, cycle):
        with self._conn() as c:
            row = c.execute("select entries from vc_manual_movements where client_id=%s and cycle=%s",
                            (client_id, cycle)).fetchone()
        return row[0] if row else []

    def get_credited(self, client_id):
        with self._conn() as c:
            rows = c.execute("select change_id from vc_credited_changes where client_id=%s",
                             (client_id,)).fetchall()
        return {r[0] for r in rows}

    def add_credited(self, client_id, change_ids):
        with self._conn() as c:
            for cid in change_ids:
                c.execute("""insert into vc_credited_changes (client_id, change_id) values (%s,%s)
                             on conflict do nothing""", (client_id, cid))
