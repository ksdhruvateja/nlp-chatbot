"""PostgreSQL access: a small connection pool plus migration helper."""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import get_settings

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

_lock = threading.Lock()
_idle: list[psycopg.Connection] = []
_MAX_IDLE = 10


def _connect() -> psycopg.Connection:
    url = get_settings().database_url
    kwargs: dict = {"row_factory": dict_row, "connect_timeout": 15}
    host = url.split("@")[-1]
    if "sslmode=" not in url and not host.startswith(("localhost", "127.0.0.1", "/")):
        kwargs["sslmode"] = "require"          # Neon / RDS always need TLS
    # Neon's pooled endpoint (PgBouncer, transaction mode) can't keep server-side prepared statements
    # across transactions, so disable psycopg's automatic preparation.
    conn = psycopg.connect(url, prepare_threshold=None, **kwargs)
    return conn


def _healthy(conn: psycopg.Connection) -> bool:
    """Neon suspends idle computes and drops their connections; detect that before reuse."""
    if conn.closed or conn.broken:
        return False
    try:
        conn.execute("SELECT 1")
        conn.rollback()
        return True
    except psycopg.Error:
        return False


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Yield a pooled connection; commits on success, rolls back on error."""
    with _lock:
        conn = _idle.pop() if _idle else None
    if conn is not None and not _healthy(conn):
        conn.close()
        conn = None
    if conn is None:
        for attempt in range(3):   # a suspended Neon compute takes a moment to wake up
            try:
                conn = _connect()
                break
            except psycopg.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(1 + attempt * 2)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if conn.closed or conn.broken:
            pass
        else:
            with _lock:
                if len(_idle) < _MAX_IDLE:
                    _idle.append(conn)
                else:
                    conn.close()


def fetch_one(sql: str, params: Any = None) -> dict | None:
    with connection() as conn:
        return conn.execute(sql, params).fetchone()


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    with connection() as conn:
        return conn.execute(sql, params).fetchall()


def execute(sql: str, params: Any = None) -> None:
    with connection() as conn:
        conn.execute(sql, params)


def jsonb(value: Any) -> Jsonb:
    return Jsonb(value)


def vector_literal(vec) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def migrate() -> None:
    """Idempotent schema migration. On Neon, set MIGRATION_DATABASE_URL to the direct (non "-pooler")
    endpoint so long DDL isn't cut off by the pooler; the app itself can use the pooled URL."""
    sql = SCHEMA_PATH.read_text().replace("{dim}", str(get_settings().embedding_dim))
    url = os.getenv("MIGRATION_DATABASE_URL")
    if url:
        with psycopg.connect(url, prepare_threshold=None, autocommit=False) as conn:
            conn.execute(sql)
        return
    with connection() as conn:
        conn.execute(sql)


def close_pool() -> None:
    with _lock:
        while _idle:
            _idle.pop().close()
