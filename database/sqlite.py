#!/usr/bin/env python3
# src/core/database/sqlite.py

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Optional, Iterable, Dict, List, Tuple, Union

from PyQt5.QtWidgets import QApplication

try:
    from core.helper import Helper
    from core.log import Log
except ImportError:
    from helper import Helper
    from log import Log


Row = Dict[str, Any]


class SQLite:
    """
    Lightweight SQLite wrapper for corePY.

    Features:
      - Auto-wires Helper/Logger from QApplication when available
      - Centralized DB path handling
      - Connection lifecycle (connect/close)
      - CRUD for tables (exists, create, drop, list, columns)
      - CRUD for records (insert/select/update/delete/upsert)
      - Parameterized queries (safe)
      - Transaction context manager
    """

    def __init__(
        self,
        *,
        helper: Optional[Helper] = None,
        logger: Optional[Log] = None,
        db_path: Optional[str] = None,
        app_name: Optional[str] = None,
        ensure_dir: bool = True,
        timeout: float = 30.0,
    ):
        super().__init__()

        # --- auto-wire from QApplication if not provided ---
        if helper is None or logger is None:
            app = QApplication.instance()
            if app is not None:
                helper = helper or getattr(app, "helper", None)
                logger = logger or getattr(app, "logger", None)

        self._helper: Helper = helper or Helper()
        self._logger: Optional[Log] = logger

        self._timeout = float(timeout)

        # Determine DB path
        self._db_path = self._resolve_db_path(
            db_path=db_path,
            app_name=app_name or getattr(QApplication.instance(), "name", None) or "corePY",
        )

        if ensure_dir:
            self._ensure_parent_dir(self._db_path)

        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Path / connection
    # ------------------------------------------------------------------

    def _resolve_db_path(self, *, db_path: Optional[str], app_name: str) -> str:
        if db_path:
            return os.path.abspath(db_path)

        # Prefer corePY's config/data folder if Helper exposes one; else fallback to ~/.<app_name>/
        # Adjust this to your Helper conventions if you already have something like get_path("config") etc.
        base = None
        try:
            # If your helper has a dedicated config/data directory, use it
            # Example patterns used in your projects: helper.get_path("config"), helper.get_path("Data"), etc.
            base = self._helper.get_path("config")  # may be None depending on your helper
        except Exception:
            base = None

        if not base or not os.path.isdir(base):
            home = os.path.expanduser("~")
            base = os.path.join(home, f".{app_name.lower()}")

        return os.path.join(base, "database.sqlite")

    def _ensure_parent_dir(self, path: str) -> None:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)

    @property
    def path(self) -> str:
        return self._db_path

    def connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn

        self._log(f"[SQLite] opening: {self._db_path}", level="debug", channel="sqlite")

        conn = sqlite3.connect(
            self._db_path,
            timeout=self._timeout,
            isolation_level=None,  # we manage transactions manually when needed
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row

        # IMPORTANT: set _conn before running any PRAGMAs to avoid recursion
        self._conn = conn

        # Sensible defaults (execute directly on the connection to avoid calling self.execute/connect)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")   # good concurrency for desktop apps
        conn.execute("PRAGMA synchronous = NORMAL;") # good balance for WAL

        return conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    # ------------------------------------------------------------------
    # Low-level query helpers
    # ------------------------------------------------------------------

    def execute(self, sql: str, params: Union[Tuple[Any, ...], Dict[str, Any], None] = None) -> sqlite3.Cursor:
        conn = self.connect()
        cur = conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return cur

    def executemany(self, sql: str, seq: Iterable[Union[Tuple[Any, ...], Dict[str, Any]]]) -> int:
        conn = self.connect()
        cur = conn.cursor()
        cur.executemany(sql, seq)
        return cur.rowcount

    def query(self, sql: str, params: Union[Tuple[Any, ...], Dict[str, Any], None] = None) -> List[Row]:
        cur = self.execute(sql, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

    def one(self, sql: str, params: Union[Tuple[Any, ...], Dict[str, Any], None] = None) -> Optional[Row]:
        cur = self.execute(sql, params)
        r = cur.fetchone()
        return dict(r) if r else None

    def scalar(self, sql: str, params: Union[Tuple[Any, ...], Dict[str, Any], None] = None) -> Any:
        cur = self.execute(sql, params)
        r = cur.fetchone()
        if not r:
            return None
        # sqlite3.Row is indexable
        return r[0]

    @contextmanager
    def transaction(self):
        """
        Usage:
            with db.transaction():
                db.insert(...)
                db.update(...)
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN;")
            yield
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise

    # ------------------------------------------------------------------
    # Table CRUD
    # ------------------------------------------------------------------

    def list_tables(self) -> List[str]:
        rows = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
        )
        return [r["name"] for r in rows]

    def table_exists(self, table: str) -> bool:
        r = self.scalar(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;",
            (table,),
        )
        return bool(r)

    def drop_table(self, table: str) -> None:
        self.execute(f'DROP TABLE IF EXISTS "{table}";')

    def create_table(self, table: str, columns_sql: str) -> None:
        """
        columns_sql example:
            "id INTEGER PRIMARY KEY, name TEXT NOT NULL, created_utc TEXT"
        """
        self.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({columns_sql});')

    def columns(self, table: str) -> List[Row]:
        # PRAGMA doesn't accept binding for identifiers; table must be trusted input
        return self.query(f'PRAGMA table_info("{table}");')

    # ------------------------------------------------------------------
    # Record CRUD
    # ------------------------------------------------------------------

    def insert(self, table: str, data: Row) -> int:
        keys = list(data.keys())
        cols = ", ".join([f'"{k}"' for k in keys])
        placeholders = ", ".join([f":{k}" for k in keys])
        sql = f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders});'
        cur = self.execute(sql, data)
        return int(cur.lastrowid or 0)

    def select(
        self,
        table: str,
        where: Optional[str] = None,
        params: Union[Tuple[Any, ...], Dict[str, Any], None] = None,
        *,
        columns: Optional[List[str]] = None,
        order_by: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[Row]:
        cols = "*"
        if columns:
            cols = ", ".join([f'"{c}"' for c in columns])

        sql = f'SELECT {cols} FROM "{table}"'
        if where:
            sql += f" WHERE {where}"
        if order_by:
            sql += f" ORDER BY {order_by}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        if offset is not None:
            sql += f" OFFSET {int(offset)}"
        sql += ";"
        return self.query(sql, params)

    def update(self, table: str, data: Row, where: str, params: Union[Tuple[Any, ...], Dict[str, Any]]) -> int:
        keys = list(data.keys())
        set_clause = ", ".join([f'"{k}"=:{k}' for k in keys])
        sql = f'UPDATE "{table}" SET {set_clause} WHERE {where};'

        merged: Dict[str, Any]
        if isinstance(params, dict):
            merged = dict(params)
        else:
            # If params is tuple, we can't merge by name; require dict for update WHERE params
            raise ValueError("SQLite.update requires WHERE params as dict for safe named binding.")

        merged.update(data)
        cur = self.execute(sql, merged)
        return int(cur.rowcount or 0)

    def delete(self, table: str, where: str, params: Union[Tuple[Any, ...], Dict[str, Any]]) -> int:
        sql = f'DELETE FROM "{table}" WHERE {where};'
        cur = self.execute(sql, params)
        return int(cur.rowcount or 0)

    def upsert(
        self,
        table: str,
        data: Row,
        conflict_columns: List[str],
        update_columns: Optional[List[str]] = None,
    ) -> None:
        """
        INSERT ... ON CONFLICT(...) DO UPDATE SET ...
        """
        keys = list(data.keys())
        cols = ", ".join([f'"{k}"' for k in keys])
        placeholders = ", ".join([f":{k}" for k in keys])

        conflict = ", ".join([f'"{c}"' for c in conflict_columns])

        if update_columns is None:
            update_columns = [k for k in keys if k not in conflict_columns]

        if update_columns:
            set_clause = ", ".join([f'"{k}"=excluded."{k}"' for k in update_columns])
            sql = (
                f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) '
                f"ON CONFLICT({conflict}) DO UPDATE SET {set_clause};"
            )
        else:
            sql = (
                f'INSERT INTO "{table}" ({cols}) VALUES ({placeholders}) '
                f"ON CONFLICT({conflict}) DO NOTHING;"
            )

        self.execute(sql, data)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log(self, msg: str, *, level: str = "info", channel: str = "sqlite") -> None:
        if self._logger is not None and hasattr(self._logger, "append"):
            self._logger.append(msg, level=level, channel=channel)  # type: ignore[call-arg]
        else:
            print(msg)
