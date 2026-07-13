# OrderFlow — Production Engineering Platform

OrderFlow is a simulated online commerce system whose sole purpose is to
generate a continuously-evolving, realistic PostgreSQL workload. It is not a
demo project — it is a **teaching platform** that powers every DBA lab from
indexing to cloud migration, all against the **same live dataset**.

Every concept — indexes, partitioning, replication, backup/PITR, security,
monitoring, extensions, cloud — is taught against the same schema and the same
workload that previous labs already shaped. A B-tree index added in Lab 05 is
still there when you measure replication lag in Lab 07.

---

## Prerequisites

| Requirement | Minimum version | Notes |
|-------------|-----------------|-------|
| PostgreSQL  | 14              | Must be installed natively on the host (no Docker) |
| Python      | 3.10            | 3.11+ recommended |
| psql client | matches server  | `sudo apt-get install postgresql-client` |

---

## Quickstart

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd OrderFlow

# 2. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and edit the config file
cp python/config/config.yaml.example python/config/config.yaml
# Edit python/config/config.yaml — set host, port, dbname, user

# 5. Export required environment variables
export ORDERFLOW_DB_PASSWORD=<app-user-password>        # password for the app DB role
export ORDERFLOW_ADMIN_USER=postgres                    # PostgreSQL superuser (default: postgres)
export ORDERFLOW_ADMIN_PASSWORD=<superuser-password>    # superuser password

# 6. Bootstrap everything
python bootstrap.py
```

Bootstrap creates the database, applies the schema, loads ~500 000 historical
orders, and starts all four continuous workers as detached background processes.
The full run takes **5–15 minutes** depending on your hardware (historical load
dominates).

---

## What success looks like

When bootstrap finishes you will see a summary table. Within a minute of the
workers starting, order counts should be climbing. Verify with:

```sql
-- Run this in psql a few times, 30 seconds apart
\c orderflow
SET search_path = orderflow, public;

SELECT
    status,
    COUNT(*)                          AS orders,
    MAX(updated_at)                   AS last_updated
FROM orders
GROUP BY status
ORDER BY status;
```

You should see `NEW`, `PROCESSING`, `PACKED`, `SHIPPED`, `DELIVERED`,
`RETURNED`, `REFUNDED` rows, with counts changing between runs as workers
advance orders through the lifecycle.

---

## Process control

```bash
# Check which workers are alive
python bootstrap.py --status

# Stop all workers gracefully (SIGTERM — each worker finishes its current transaction)
python bootstrap.py --stop

# Restart (re-run is idempotent — skips steps already completed)
python bootstrap.py
```

Worker logs live in `logs/<worker-name>.log`. Tail one live:

```bash
tail -f logs/order_generator.log
```

---

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `ORDERFLOW_DB_PASSWORD` | **yes** | — | Password for the app database role |
| `ORDERFLOW_ADMIN_USER` | first run only | `postgres` | PostgreSQL superuser for DB/role creation |
| `ORDERFLOW_ADMIN_PASSWORD` | first run only | — | Superuser password |

`ORDERFLOW_ADMIN_USER` / `ORDERFLOW_ADMIN_PASSWORD` are only needed when
bootstrap provisions a new database and role. After the first successful run
they are not needed by the continuous workers (only `ORDERFLOW_DB_PASSWORD`
is required).

The database password is **never** stored in `config.yaml` or anywhere in the
repo. See `docs/configuration.md` for the full rationale.

---

## Repository layout

| Path | Contents |
|------|----------|
| `bootstrap.py` | One-command setup and process control |
| `database/migrations/` | Versioned DDL — frozen after milestone approval |
| `python/config/` | Config loader: reads `config.yaml` + env vars |
| `python/factories/` | Data factories: generate realistic seed data |
| `python/workers/` | Background workers: simulate live order traffic |
| `docs/` | Configuration reference, architecture decisions |

---

## Architecture & business rules

- `architecture.md` — system design, component diagram, deployment assumptions
- `business_rules.md` — canonical order lifecycle (normative; all workers conform)
- `docs/configuration.md` — all tunable parameters with defaults

## Milestone status

See `roadmap.md`.
