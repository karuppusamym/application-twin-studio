"""Per-environment, per-application state store (one SQLite file each).

Entities become real tables with real columns so that a backend automation
can read them with plain SQL. Vendor dialects (Oracle, SQL Server procedures,
locking semantics) are NOT emulated; the capability manifest says so.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_TYPES = {"string": "TEXT", "integer": "INTEGER", "decimal": "TEXT", "boolean": "INTEGER", "json": "TEXT"}


class StoreError(Exception):
    pass


def ident(name: str) -> str:
    if not _IDENT.match(name or ""):
        raise StoreError(f"unsafe identifier {name!r}")
    return f'"{name}"'  # quoted: entity names such as "case" are SQL keywords


def field_type(spec: Any) -> str:
    return spec if isinstance(spec, str) else (spec or {}).get("type", "string")


class Store:
    def __init__(self, path: Path, entities: dict):
        self.path = Path(path)
        self.entities = entities
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self._create()

    def _create(self) -> None:
        with self.lock:
            for name, ent in self.entities.items():
                cols = []
                for fname, fspec in ent["fields"].items():
                    col = f"{ident(fname)} {_SQL_TYPES[field_type(fspec)]}"
                    if fname == ent["key"]:
                        col += " PRIMARY KEY"
                    cols.append(col)
                cols.append("_rev INTEGER NOT NULL DEFAULT 1")
                self.db.execute(f"CREATE TABLE IF NOT EXISTS {ident(name)} ({', '.join(cols)})")
            self.db.executescript(
                """
                CREATE TABLE IF NOT EXISTS _idempotency (
                    op TEXT, caller TEXT, key TEXT, request_hash TEXT, status INTEGER, body TEXT,
                    PRIMARY KEY (op, caller, key));
                CREATE TABLE IF NOT EXISTS _outbox (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, payload TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS _seq (name TEXT PRIMARY KEY, value INTEGER);
                """
            )

    # -- transactions -----------------------------------------------------
    def begin(self) -> None:
        self.lock.acquire()
        self.db.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        try:
            self.db.execute("COMMIT")
        finally:
            self.lock.release()

    def rollback(self) -> None:
        try:
            self.db.execute("ROLLBACK")
        finally:
            self.lock.release()

    # -- value conversion -------------------------------------------------
    def _to_db(self, entity: str, field: str, value: Any) -> Any:
        ftype = field_type(self.entities[entity]["fields"][field])
        if value is None:
            return None
        if ftype == "decimal":
            try:
                return str(Decimal(str(value)))
            except InvalidOperation as exc:
                raise StoreError(f"{entity}.{field}: {value!r} is not a decimal") from exc
        if ftype == "integer":
            return int(value)
        if ftype == "boolean":
            return 1 if value in (True, 1, "true", "Y", "1") else 0
        if ftype == "json":
            return json.dumps(value, sort_keys=True)
        return str(value)

    def _from_db(self, entity: str, row: sqlite3.Row) -> dict:
        out = {}
        for fname, fspec in self.entities[entity]["fields"].items():
            v = row[fname]
            ftype = field_type(fspec)
            if v is not None and ftype == "boolean":
                v = bool(v)
            elif v is not None and ftype == "json":
                v = json.loads(v)
            out[fname] = v
        out["_rev"] = row["_rev"]
        return out

    # -- entity access ----------------------------------------------------
    def get(self, entity: str, key: Any) -> dict | None:
        ent = self.entities[entity]
        with self.lock:
            row = self.db.execute(
                f"SELECT * FROM {ident(entity)} WHERE {ident(ent['key'])} = ?", (key,)
            ).fetchone()
        return self._from_db(entity, row) if row else None

    def query(self, entity: str, where: dict | None = None, order_by: str | None = None) -> list[dict]:
        ent = self.entities[entity]
        clauses, args = [], []
        for f, v in (where or {}).items():
            if f not in ent["fields"]:
                raise StoreError(f"{entity} has no field {f}")
            if v is None:
                continue  # an absent filter value means "no filter"
            clauses.append(f"{ident(f)} = ?")
            args.append(self._to_db(entity, f, v))
        sql = f"SELECT * FROM {ident(entity)}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {ident(order_by or ent['key'])}"
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
        return [self._from_db(entity, r) for r in rows]

    def insert(self, entity: str, values: dict) -> dict:
        ent = self.entities[entity]
        unknown = set(values) - set(ent["fields"])
        if unknown:
            raise StoreError(f"{entity}: unknown fields {sorted(unknown)}")
        if values.get(ent["key"]) in (None, ""):
            raise StoreError(f"{entity}: key field {ent['key']} is required")
        cols = list(values)
        with self.lock:
            self.db.execute(
                f"INSERT INTO {ident(entity)} ({', '.join(map(ident, cols))}) VALUES ({', '.join('?' * len(cols))})",
                [self._to_db(entity, c, values[c]) for c in cols],
            )
        return self.get(entity, values[ent["key"]])

    def update(self, entity: str, key: Any, values: dict) -> dict:
        ent = self.entities[entity]
        unknown = set(values) - set(ent["fields"])
        if unknown:
            raise StoreError(f"{entity}: unknown fields {sorted(unknown)}")
        sets = ", ".join(f"{ident(c)} = ?" for c in values)
        args = [self._to_db(entity, c, v) for c, v in values.items()]
        with self.lock:
            self.db.execute(
                f"UPDATE {ident(entity)} SET {sets}{', ' if sets else ''}_rev = _rev + 1 WHERE {ident(ent['key'])} = ?",
                args + [key],
            )
        return self.get(entity, key)

    def delete(self, entity: str, key: Any) -> None:
        ent = self.entities[entity]
        with self.lock:
            self.db.execute(f"DELETE FROM {ident(entity)} WHERE {ident(ent['key'])} = ?", (key,))

    # -- system tables ----------------------------------------------------
    def next_seq(self, name: str, start: int = 1) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM _seq WHERE name = ?", (name,)).fetchone()
            value = (row["value"] + 1) if row else start
            self.db.execute("INSERT OR REPLACE INTO _seq (name, value) VALUES (?, ?)", (name, value))
        return value

    def emit(self, event_type: str, payload: dict, at: str) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO _outbox (event_type, payload, at) VALUES (?, ?, ?)",
                (event_type, json.dumps(payload, sort_keys=True, default=str), at),
            )

    def events(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM _outbox ORDER BY seq").fetchall()
        return [{"seq": r["seq"], "event_type": r["event_type"], "payload": json.loads(r["payload"]), "at": r["at"]} for r in rows]

    def receipt(self, op: str, caller: str, key: str) -> dict | None:
        with self.lock:
            row = self.db.execute(
                "SELECT * FROM _idempotency WHERE op = ? AND caller = ? AND key = ?", (op, caller, key)
            ).fetchone()
        return dict(row) if row else None

    def save_receipt(self, op: str, caller: str, key: str, request_hash: str, status: int, body: Any) -> None:
        with self.lock:
            self.db.execute(
                "INSERT INTO _idempotency (op, caller, key, request_hash, status, body) VALUES (?, ?, ?, ?, ?, ?)",
                (op, caller, key, request_hash, status, json.dumps(body, default=str)),
            )

    def snapshot(self) -> dict:
        return {name: self.query(name) for name in self.entities}

    def close(self) -> None:
        with self.lock:
            self.db.close()
