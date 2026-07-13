#!/usr/bin/env python3
"""
OrderFlow Bootstrap
===================
Sets up and starts the full OrderFlow workload on a fresh VM in one command.

Default mode  : preflight → DB provisioning → schema → historical load → workers.
--status      : check whether each worker process is alive.
--stop        : gracefully stop all workers via SIGTERM.

Usage:
    python bootstrap.py            # first-time setup + start workers
    python bootstrap.py --status   # check worker health
    python bootstrap.py --stop     # gracefully stop all workers

Prerequisites — see README.md for the full walkthrough:
    1. PostgreSQL 14+ installed and running on this host.
    2. python -m venv .venv && source .venv/bin/activate
    3. pip install -r requirements.txt
    4. cp python/config/config.yaml.example python/config/config.yaml
       (edit host / port / dbname / user to match your setup)
    5. export ORDERFLOW_DB_PASSWORD=<app-user-password>
       export ORDERFLOW_ADMIN_USER=postgres        # default
       export ORDERFLOW_ADMIN_PASSWORD=<superuser-password>
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Ensure repo root is importable regardless of working directory.
_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORKERS = [
    "order_generator",
    "order_processor",
    "payment_processor",
    "employee_updates",
]

_PID_DIR   = _REPO_ROOT / ".orderflow" / "pids"
_LOG_DIR   = _REPO_ROOT / "logs"
_MIGRATION = _REPO_ROOT / "database" / "migrations" / "001_initial_schema.sql"


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


def _die(msg: str) -> None:
    print(f"\n  [ERROR] {msg}\n", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# PID / process helpers
# ---------------------------------------------------------------------------

def _pid_file(name: str) -> Path:
    return _PID_DIR / f"{name}.pid"


def _log_file(name: str) -> Path:
    return _LOG_DIR / f"{name}.log"


def _read_pid(name: str) -> int | None:
    p = _pid_file(name)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except (ValueError, OSError):
        return None


def _is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists; we just can't signal it


# ---------------------------------------------------------------------------
# Step 1 — Preflight checks
# ---------------------------------------------------------------------------

def _check_python_version() -> None:
    if sys.version_info < (3, 10):
        _die(
            f"Python 3.10+ required; found {sys.version_info.major}.{sys.version_info.minor}.\n"
            "  Activate a venv with a recent Python interpreter and retry."
        )
    _ok(f"Python {sys.version_info.major}.{sys.version_info.minor}")


def _check_config() -> None:
    config_path = _REPO_ROOT / "python" / "config" / "config.yaml"
    if not config_path.exists():
        _die(
            "python/config/config.yaml not found.\n"
            "  Create it from the example and fill in your values:\n"
            "    cp python/config/config.yaml.example python/config/config.yaml"
        )
    _ok("python/config/config.yaml found")


def _check_psql() -> None:
    import shutil
    if not shutil.which("psql"):
        _die(
            "psql not found in PATH.\n"
            "  Bootstrap applies the schema migration via psql.\n"
            "  Install the PostgreSQL client:\n"
            "    sudo apt-get install postgresql-client   # Debian / Ubuntu\n"
            "    sudo dnf install postgresql              # RHEL / Fedora"
        )
    _ok("psql found in PATH")


def _check_env_vars() -> tuple[str, str]:
    """Verify required env vars are set. Returns (admin_user, admin_pass)."""
    if not os.environ.get("ORDERFLOW_DB_PASSWORD"):
        _die(
            "ORDERFLOW_DB_PASSWORD is not set.\n"
            "  This is the password for the OrderFlow app database user.\n"
            "  export ORDERFLOW_DB_PASSWORD=<password>"
        )
    _ok("ORDERFLOW_DB_PASSWORD is set")

    admin_user = os.environ.get("ORDERFLOW_ADMIN_USER", "postgres")
    admin_pass = os.environ.get("ORDERFLOW_ADMIN_PASSWORD")
    if not admin_pass:
        _die(
            "ORDERFLOW_ADMIN_PASSWORD is not set.\n"
            "  Bootstrap needs a PostgreSQL superuser to create the database\n"
            "  and app role on first run.\n"
            "  export ORDERFLOW_ADMIN_USER=postgres          # or your superuser\n"
            "  export ORDERFLOW_ADMIN_PASSWORD=<superuser-password>"
        )
    _ok(f"Admin credentials ready (ORDERFLOW_ADMIN_USER={admin_user})")
    return admin_user, admin_pass


def _load_cfg():
    try:
        from python.config.loader import load_config
        return load_config()
    except ImportError as exc:
        _die(f"Cannot import config loader: {exc}\n  Run: pip install -r requirements.txt")
    except FileNotFoundError as exc:
        _die(str(exc))
    except EnvironmentError as exc:
        _die(str(exc))


def _check_pg_reachable(cfg, admin_user: str, admin_pass: str) -> None:
    try:
        import psycopg
    except ImportError:
        _die("psycopg is not installed.\n  Run: pip install -r requirements.txt")

    try:
        conn = psycopg.connect(
            host=cfg.database.host,
            port=cfg.database.port,
            dbname="postgres",
            user=admin_user,
            password=admin_pass,
            application_name="bootstrap",
            connect_timeout=5,
        )
        conn.close()
        _ok(
            f"PostgreSQL reachable at {cfg.database.host}:{cfg.database.port}"
            f" (admin_user={admin_user})"
        )
    except Exception as exc:
        _die(
            f"Cannot connect to PostgreSQL as '{admin_user}':\n  {exc}\n"
            "  Check that PostgreSQL is running and that ORDERFLOW_ADMIN_PASSWORD is correct."
        )


# ---------------------------------------------------------------------------
# Step 2 — Database & role provisioning (admin connection, autocommit)
# ---------------------------------------------------------------------------

def _provision_db_and_role(cfg, admin_user: str, admin_pass: str) -> None:
    import psycopg
    from psycopg import sql

    conn = psycopg.connect(
        host=cfg.database.host,
        port=cfg.database.port,
        dbname="postgres",          # maintenance DB — safe target for CREATE DATABASE
        user=admin_user,
        password=admin_pass,
        application_name="bootstrap",
        autocommit=True,            # DDL must run outside a transaction block
    )
    try:
        # ── Database ────────────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (cfg.database.dbname,),
            )
            if cur.fetchone():
                _info(f"Database '{cfg.database.dbname}' already exists, skipping.")
            else:
                cur.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(cfg.database.dbname)
                    )
                )
                _ok(f"Database '{cfg.database.dbname}' created.")

        # ── App role ────────────────────────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s",
                (cfg.database.user,),
            )
            if cur.fetchone():
                _info(f"Role '{cfg.database.user}' already exists, skipping.")
            else:
                cur.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD %s").format(
                        sql.Identifier(cfg.database.user)
                    ),
                    (cfg.database.password,),
                )
                _ok(f"Role '{cfg.database.user}' created.")

        # ── GRANT CONNECT (idempotent) ───────────────────────────────────────
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(cfg.database.dbname),
                    sql.Identifier(cfg.database.user),
                )
            )
        _ok(f"GRANT CONNECT on '{cfg.database.dbname}' to '{cfg.database.user}'.")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 3 — Schema application (psql -f; handles $…$ function bodies)
# ---------------------------------------------------------------------------

def _apply_schema(cfg, admin_user: str, admin_pass: str) -> None:
    import psycopg
    from psycopg import sql

    # Check whether the schema has already been applied.
    admin_conn = psycopg.connect(
        host=cfg.database.host,
        port=cfg.database.port,
        dbname=cfg.database.dbname,
        user=admin_user,
        password=admin_pass,
        application_name="bootstrap",
    )
    try:
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'orderflow' AND table_name = 'orders'"
            )
            schema_exists = cur.fetchone() is not None
    finally:
        admin_conn.close()

    if schema_exists:
        _info("Schema already applied (orderflow.orders exists), skipping.")
        return

    # Apply via psql -f so that $…$-delimited PL/pgSQL function bodies
    # are treated as a single statement, which psycopg.execute() cannot do.
    _info(f"Applying {_MIGRATION.name} via psql …")
    env = os.environ.copy()
    env["PGPASSWORD"] = admin_pass
    result = subprocess.run(
        [
            "psql",
            "-h", cfg.database.host,
            "-p", str(cfg.database.port),
            "-U", admin_user,
            "-d", cfg.database.dbname,
            "-v", "ON_ERROR_STOP=1",
            "-f", str(_MIGRATION),
        ],
        env=env,
    )
    if result.returncode != 0:
        _die(f"Schema migration failed (psql exit code {result.returncode}).")
    _ok("Schema applied.")

    # Grant object-level permissions to the app role.
    grant_conn = psycopg.connect(
        host=cfg.database.host,
        port=cfg.database.port,
        dbname=cfg.database.dbname,
        user=admin_user,
        password=admin_pass,
        application_name="bootstrap",
        autocommit=True,
    )
    try:
        app = sql.Identifier(cfg.database.user)
        with grant_conn.cursor() as cur:
            cur.execute(
                sql.SQL("GRANT USAGE ON SCHEMA orderflow TO {}").format(app)
            )
            cur.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE "
                    "ON ALL TABLES IN SCHEMA orderflow TO {}"
                ).format(app)
            )
            cur.execute(
                sql.SQL(
                    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA orderflow TO {}"
                ).format(app)
            )
            # Cover objects created by future migrations.
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA orderflow "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(sql.Identifier(admin_user), app)
            )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA orderflow "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(sql.Identifier(admin_user), app)
            )
        _ok(f"Permissions granted to '{cfg.database.user}'.")
    finally:
        grant_conn.close()


# ---------------------------------------------------------------------------
# Step 4 — Historical data load (subprocess — history_loader uses argparse)
# ---------------------------------------------------------------------------

def _historical_load(cfg) -> None:
    from python.workers.db import get_connection

    conn = get_connection("bootstrap", cfg.database)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM orders")
            n = cur.fetchone()[0]
    finally:
        conn.close()

    if n > 0:
        _info(f"Historical data already loaded ({n:,} orders), skipping.")
        return

    _info(
        "Starting historical data load — this populates ~500 k orders and will\n"
        "  take several minutes. Progress is logged to stdout in real time."
    )
    result = subprocess.run(
        [
            sys.executable,
            str(_REPO_ROOT / "python" / "workers" / "history_loader.py"),
        ],
        cwd=str(_REPO_ROOT),
    )
    if result.returncode != 0:
        _die(
            f"Historical data load failed (exit code {result.returncode}).\n"
            "  Check the output above. Re-run bootstrap once the issue is fixed;\n"
            "  if data is partially loaded, use:\n"
            "    python python/workers/history_loader.py --force"
        )
    _ok("Historical data loaded.")


# ---------------------------------------------------------------------------
# Step 5 — Launch workers as detached background processes
# ---------------------------------------------------------------------------

def _launch_workers() -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    _PID_DIR.mkdir(parents=True, exist_ok=True)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    launched: list[tuple[str, int]] = []
    skipped:  list[tuple[str, int]] = []

    for name in WORKERS:
        existing_pid = _read_pid(name)
        if existing_pid is not None and _is_alive(existing_pid):
            _info(f"Worker '{name}' already running (PID {existing_pid}), skipping.")
            skipped.append((name, existing_pid))
            continue

        log_path = _log_file(name)
        with open(log_path, "a") as log_fh:
            platform_kwargs: dict = {}
            if sys.platform == "win32":
                platform_kwargs["creationflags"] = (
                    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                platform_kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(_REPO_ROOT / "python" / "workers" / f"{name}.py"),
                ],
                stdout=log_fh,
                stderr=log_fh,
                cwd=str(_REPO_ROOT),
                **platform_kwargs,
            )

        _pid_file(name).write_text(str(proc.pid))
        launched.append((name, proc.pid))
        _ok(f"Worker '{name}' started — PID {proc.pid}  log → {log_path}")

    return launched, skipped


# ---------------------------------------------------------------------------
# Step 6 — Summary
# ---------------------------------------------------------------------------

def _print_summary(cfg, launched: list, skipped: list) -> None:
    print()
    print("=" * 62)
    print("  OrderFlow — Ready")
    print("=" * 62)

    try:
        from python.workers.db import get_connection

        conn = get_connection("bootstrap", cfg.database)
        tables = [
            "employees", "customers", "products", "warehouses",
            "orders", "order_items", "payments",
        ]
        print()
        print(f"  {'Table':<16} {'Rows':>12}")
        print("  " + "-" * 30)
        with conn.cursor() as cur:
            for tbl in tables:
                cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                n = cur.fetchone()[0]
                print(f"  {tbl:<16} {n:>12,}")
        conn.close()
    except Exception as exc:
        _info(f"(could not query row counts: {exc})")

    print()
    print(f"  {'Worker':<25} {'Status'}")
    print("  " + "-" * 45)
    for name in WORKERS:
        pid = _read_pid(name)
        if pid and _is_alive(pid):
            print(f"  {name:<25} RUNNING  (PID {pid})")
        else:
            print(f"  {name:<25} STOPPED")

    print()
    print("  Logs:    ls logs/")
    print("  Status:  python bootstrap.py --status")
    print("  Stop:    python bootstrap.py --stop")
    print("=" * 62)
    print()


# ---------------------------------------------------------------------------
# Part B — Process-control commands (--status, --stop)
# ---------------------------------------------------------------------------

def cmd_status() -> None:
    print()
    print(f"  {'Worker':<25} {'Status':<24} {'PID':<8} Log")
    print("  " + "-" * 80)
    for name in WORKERS:
        pid = _read_pid(name)
        if pid is None:
            status  = "STOPPED (no PID file)"
            pid_str = "-"
        elif _is_alive(pid):
            status  = "RUNNING"
            pid_str = str(pid)
        else:
            status  = "DEAD (stale PID file)"
            pid_str = str(pid)
        print(f"  {name:<25} {status:<24} {pid_str:<8} {_log_file(name)}")
    print()


def cmd_stop() -> None:
    print("\nStopping OrderFlow workers …\n")
    stopped: list[str] = []
    failed:  list[str] = []

    for name in WORKERS:
        pid = _read_pid(name)

        if pid is None:
            _info(f"{name}: no PID file — already stopped.")
            continue

        if not _is_alive(pid):
            _info(f"{name}: PID {pid} is not alive — removing stale PID file.")
            _pid_file(name).unlink(missing_ok=True)
            continue

        _info(f"{name}: sending SIGTERM to PID {pid} …")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _info(f"{name}: process already gone.")
            _pid_file(name).unlink(missing_ok=True)
            continue
        except Exception as exc:
            print(f"  [WARN] {name}: could not send SIGTERM: {exc}")
            failed.append(name)
            continue

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if not _is_alive(pid):
                break
            time.sleep(0.25)

        if _is_alive(pid):
            print(
                f"  [WARN] {name}: PID {pid} still alive after 10 s."
                " Manual intervention may be required."
            )
            failed.append(name)
        else:
            _ok(f"{name}: stopped.")
            _pid_file(name).unlink(missing_ok=True)
            stopped.append(name)

    print(f"\n  Stopped: {len(stopped)}   Not stopped: {len(failed)}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OrderFlow bootstrap and process control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python bootstrap.py            # first-time setup + start workers\n"
            "  python bootstrap.py --status   # check worker health\n"
            "  python bootstrap.py --stop     # gracefully stop all workers\n"
        ),
    )
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument("--status", action="store_true", help="Print worker status table")
    grp.add_argument("--stop",   action="store_true", help="Send SIGTERM to all workers")
    args = parser.parse_args()

    if args.status:
        cmd_status()
        return

    if args.stop:
        cmd_stop()
        return

    # ── Default: full bootstrap ──────────────────────────────────────────────
    print()
    print("OrderFlow Bootstrap")
    print("=" * 62)

    print("\n[Step 1] Preflight checks")
    _check_python_version()
    _check_config()
    _check_psql()
    admin_user, admin_pass = _check_env_vars()
    cfg = _load_cfg()
    _check_pg_reachable(cfg, admin_user, admin_pass)

    print("\n[Step 2] Database & role provisioning")
    _provision_db_and_role(cfg, admin_user, admin_pass)

    print("\n[Step 3] Schema application")
    _apply_schema(cfg, admin_user, admin_pass)

    print("\n[Step 4] Historical data load")
    _historical_load(cfg)

    print("\n[Step 5] Launching workers")
    launched, skipped = _launch_workers()

    print("\n[Step 6] Summary")
    _print_summary(cfg, launched, skipped)


if __name__ == "__main__":
    main()
