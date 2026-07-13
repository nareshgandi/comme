"""
Shared database connection helper for all OrderFlow workers.

Design decisions (documented here once so they don't need to be repeated
in every worker):

  1. Raw psycopg v3, no ORM.
     DBA labs need to see real SQL, real execution plans, and real cursor
     semantics.  An ORM layer would hide exactly what the labs are designed
     to expose.

  2. One connection per process, opened at startup, reused for the process
     lifetime.  Workers are separate long-running processes — that matches
     how a real HA/replication lab kills a connection and expects a reconnect.

  3. application_name is set on every connection.
     In Lab 11 (Monitoring), pg_stat_activity needs to distinguish which
     worker is running which query.  Skipping this would make the monitoring
     lab harder for no reason.

  4. search_path=orderflow,public on every connection.
     Workers use unqualified table names (e.g. just `orders`, not
     `orderflow.orders`) so queries stay readable.

  5. autocommit=False (psycopg3 default) — workers manage transactions
     explicitly with conn.commit() / conn.rollback().
"""
from __future__ import annotations

import psycopg

from python.config.loader import DatabaseConfig


def get_connection(application_name: str, db_cfg: DatabaseConfig) -> psycopg.Connection:
    """Open and return a psycopg v3 connection.

    Args:
        application_name: Shown in pg_stat_activity.application_name.
                          Pass the worker's own script name (no .py suffix).
        db_cfg:           DatabaseConfig loaded from config.yaml + env.

    Returns:
        Open psycopg.Connection, autocommit=False, search_path=orderflow,public.
    """
    return psycopg.connect(
        host=db_cfg.host,
        port=db_cfg.port,
        dbname=db_cfg.dbname,
        user=db_cfg.user,
        password=db_cfg.password,
        application_name=application_name,
        options="-c search_path=orderflow,public",
    )
