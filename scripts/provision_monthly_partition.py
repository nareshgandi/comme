"""
OrderFlow — Monthly Partition Provisioning Script
==================================================
Ensures that monthly RANGE partitions exist on orderflow.orders for every
month from the earliest existing partition through a configurable lookahead
window (default: current month + 2).

Run this at the start of each calendar month — or schedule it with cron — to
prevent new rows from landing in the DEFAULT partition (orders_default).

Usage:
    python scripts/provision_monthly_partition.py
    python scripts/provision_monthly_partition.py --through 2025-06
    python scripts/provision_monthly_partition.py --dry-run

This script is the manual equivalent of what pg_partman + pg_cron automates.
Automation is deferred to Milestone 13. See:
    labs/06_partitioning/05_partition_maintenance/README.md

Requires:
    ORDERFLOW_DB_PASSWORD environment variable (or ORDERFLOW_ADMIN_PASSWORD
    if connecting as the admin user).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# Make the repo root importable so we can reuse the config loader.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from python.config.loader import load_config


def _add_months(d: date, n: int) -> date:
    """Return date d advanced by n calendar months."""
    month = d.month - 1 + n
    year  = d.year + month // 12
    month = month % 12 + 1
    return date(year, month, 1)


def _first_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _get_existing_partitions(conn: psycopg.Connection) -> set[date]:
    """
    Return the set of partition start months already provisioned on orders.
    Partition names follow the pattern orders_YYYY_MM.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.relname
            FROM   pg_inherits  i
            JOIN   pg_class     p ON p.oid = i.inhparent
            JOIN   pg_class     c ON c.oid = i.inhchild
            JOIN   pg_namespace n ON n.oid = p.relnamespace
            WHERE  p.relname  = 'orders'
              AND  n.nspname  = 'orderflow'
            ORDER  BY c.relname
        """)
        rows = cur.fetchall()
    conn.rollback()

    months: set[date] = set()
    for (name,) in rows:
        # orders_YYYY_MM  or  orders_default  (skip non-monthly)
        if not name.startswith("orders_") or name == "orders_default":
            continue
        suffix = name[len("orders_"):]  # e.g. "2024_10"
        parts = suffix.split("_")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            try:
                months.add(date(int(parts[0]), int(parts[1]), 1))
            except ValueError:
                pass
    return months


def _get_earliest_month(conn: psycopg.Connection) -> date | None:
    """Return the earliest month present in the orders_old (pre-migration)
    data, or None if no data exists.  Falls back to the current month."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT DATE_TRUNC('month', MIN(created_at))::date
            FROM   orderflow.orders
        """)
        row = cur.fetchone()
    conn.rollback()
    if row and row[0]:
        return row[0]
    return None


def _create_partition(
    conn: psycopg.Connection,
    month_start: date,
    dry_run: bool,
) -> None:
    month_end  = _add_months(month_start, 1)
    part_name  = f"orders_{month_start.strftime('%Y_%m')}"
    start_str  = month_start.isoformat()
    end_str    = month_end.isoformat()

    sql = (
        f"CREATE TABLE orderflow.{part_name} "
        f"PARTITION OF orderflow.orders "
        f"FOR VALUES FROM ('{start_str}'::timestamptz) "
        f"          TO   ('{end_str}'::timestamptz)"
    )

    if dry_run:
        print(f"  [dry-run] Would create: {part_name}  ({start_str} → {end_str})")
        return

    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print(f"  Created partition: {part_name}  ({start_str} → {end_str})")


def _check_default_partition(conn: psycopg.Connection) -> int:
    """Return the row count in orders_default.  Non-zero means rows arrived
    before a partition was created."""
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT COUNT(*) FROM orderflow.orders_default")
            count = cur.fetchone()[0]
        except psycopg.errors.UndefinedTable:
            count = 0
    conn.rollback()
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision monthly RANGE partitions on orderflow.orders."
    )
    parser.add_argument(
        "--through",
        metavar="YYYY-MM",
        help="Provision partitions through this month (default: current month + 2).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be done without executing any DDL.",
    )
    args = parser.parse_args()

    cfg = load_config()
    db  = cfg.database

    password = os.environ.get("ORDERFLOW_DB_PASSWORD")
    if not password:
        print("ERROR: ORDERFLOW_DB_PASSWORD is not set.", file=sys.stderr)
        sys.exit(1)

    conn_str = (
        f"host={db.host} port={db.port} "
        f"dbname={db.name} "
        f"user={db.user} password={password} "
        f"application_name=provision_monthly_partition "
        f"options='-c search_path=orderflow,public'"
    )

    try:
        conn = psycopg.connect(conn_str, autocommit=False)
    except psycopg.OperationalError as exc:
        print(f"ERROR: Cannot connect to database: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        # Determine the provisioning window
        today      = date.today()
        end_month  = _add_months(_first_of_month(today), 2)  # default: +2

        if args.through:
            try:
                y, m   = args.through.split("-")
                end_month = date(int(y), int(m), 1)
            except (ValueError, TypeError):
                print(
                    f"ERROR: --through must be in YYYY-MM format, got: {args.through}",
                    file=sys.stderr,
                )
                sys.exit(1)

        # Determine start of provisioning window
        earliest   = _get_earliest_month(conn)
        if earliest is None:
            start_month = _first_of_month(today)
            print(
                "WARNING: orders table is empty. "
                "Provisioning from current month only."
            )
        else:
            start_month = earliest

        existing = _get_existing_partitions(conn)
        print(
            f"Provisioning  {start_month}  →  {end_month}  "
            f"({len(existing)} partitions already exist)"
        )

        created  = 0
        skipped  = 0
        month    = start_month
        while month <= end_month:
            if month not in existing:
                _create_partition(conn, month, args.dry_run)
                created += 1
            else:
                skipped += 1
            month = _add_months(month, 1)

        action = "Would create" if args.dry_run else "Created"
        print(f"\n{action} {created} partition(s). Skipped {skipped} existing.")

        # Warn if DEFAULT partition has rows
        default_count = _check_default_partition(conn)
        if default_count > 0:
            print(
                f"\nWARNING: orders_default contains {default_count:,} row(s).\n"
                "These rows arrived before their target partition existed.\n"
                "To fix: identify the months represented, create their partitions,\n"
                "then move the rows with INSERT ... SELECT / DELETE or ATTACH PARTITION."
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
