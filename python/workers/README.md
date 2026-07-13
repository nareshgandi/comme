# python/workers/

Long-running worker processes that drive the live OrderFlow PostgreSQL workload.
Workers are the **only** code that talks to the database.

## Architecture decisions (recorded here, not repeated in every file)

| Decision | Rationale |
|----------|-----------|
| Raw `psycopg` v3 — no ORM | DBA labs need to see real SQL and real `EXPLAIN` plans; an ORM would hide the very thing being taught |
| One connection per process | Matches how real HA/replication labs kill a single connection and observe failover |
| `application_name` on every connection | `pg_stat_activity` in Lab 11 needs to identify workers at a glance |
| `FOR UPDATE SKIP LOCKED` on scan queries | Multiple worker instances can run without competing for the same row |
| Each transition = its own transaction | Matches `business_rules.md §7`; no long transactions held across sleep cycles |

## Scripts

| Script | Type | Description |
|--------|------|-------------|
| `history_loader.py` | **One-shot** | Bulk-loads ~1 GB of historical data (employees, customers, products, orders). Run once before starting continuous workers. |
| `order_generator.py` | Continuous | Creates new `NEW` orders at ~2.5/sec (~200 MB/day target). |
| `payment_processor.py` | Continuous | Simulates payment gateway; advances `NEW → PROCESSING` on success. |
| `order_processor.py` | Continuous | Drives `PROCESSING → PACKED → SHIPPED → DELIVERED → RETURNED → REFUNDED`. |
| `employee_updates.py` | Continuous | Applies random UPDATEs to employee rows to generate UPDATE-heavy WAL and dead tuples for VACUUM/bloat labs. |

## Shared helper

`db.py` — single source of truth for database connection parameters. All
workers import `get_connection()` from here; none define their own connection
strings.

## Running

```bash
# Terminal 1 — load historical data once
cd /path/to/OrderFlow
python python/workers/history_loader.py

# Terminal 2-5 — start continuous workers
python python/workers/order_generator.py
python python/workers/payment_processor.py
python python/workers/order_processor.py
python python/workers/employee_updates.py
```

All workers stop gracefully on `SIGINT` (Ctrl-C) or `SIGTERM`. The current
transaction always commits before exit.

## Configuration

All tunables live in `python/config/config.yaml`. See `docs/configuration.md`
for the full reference. Workers call `load_config()` at startup and thread
the returned `Config` object to factories and helper functions.
The database password is read from `ORDERFLOW_DB_PASSWORD` (env var only —
never stored in YAML).
