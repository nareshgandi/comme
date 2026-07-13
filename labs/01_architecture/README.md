# Lab 01 — Architecture & Repository Orientation

**Type:** Foundation write-up (no Business Problem / Optimize shape)
**Prerequisite:** `python bootstrap.py` completed; at least one worker running.
**Time:** 30 minutes

---

## What Was Built (Milestone 1)

Milestone 1 produced the frozen foundation that every later lab depends on:

| Deliverable | Location | Purpose |
|-------------|----------|---------|
| Schema DDL | `database/migrations/001_initial_schema.sql` | The 7-table PostgreSQL schema — frozen after approval |
| Business rules | `business_rules.md` | Normative spec for all workers and labs |
| Architecture | `architecture.md` | System design, component diagram, phase breakdown |
| Repo skeleton | `/` | Directory layout, `.gitignore`, `requirements.txt` |

Nothing in `001_initial_schema.sql` or `business_rules.md` changes after
Milestone 1 approval. If a later milestone reveals a conflict, the protocol is
to stop, document the conflict, and get explicit sign-off — not to quietly
patch the frozen files.

---

## The 7-Table Schema

The schema lives in the `orderflow` PostgreSQL schema (separate from `public`)
and contains exactly these tables:

```
employees      — staff who fulfill orders (warehouse, courier, finance, manager, admin)
customers      — buyers; loyalty tier tracked for analytics
products       — SKU catalog with category, pricing, and JSONB metadata
warehouses     — physical fulfillment centres by region (us-east, us-west, us-central, eu-central)
orders         — one row per order; status column drives the lifecycle state machine
order_items    — line items; immutable after insert (INV-07)
payments       — one row per attempt; multiple allowed per order (retries + refund)
```

### Why exactly these 7 tables?

The goal was the minimum schema that:

1. **Demonstrates every DBA concept** in the lab sequence — indexes (05),
   partitioning (06), replication (07), PITR (08), RLS + pgcrypto (09),
   FDW + pgvector (10), autovacuum / bloat (11).
2. **Fits in a single `\d+ orderflow.*` screen** — small enough to hold in
   your head, large enough to produce realistic query plans and workload
   patterns.
3. **Has at least one of every constraint type** — BIGINT GENERATED ALWAYS AS
   IDENTITY primary keys, `GENERATED ALWAYS AS STORED` computed column
   (`line_total`), CHECK constraints, partial unique index target
   (`payments.status = 'SUCCESS'`), foreign keys with ON DELETE behavior.

Deliberately excluded: inventory, suppliers, invoices, coupons, reviews. Each
of those would add tables without adding teaching value for the targeted lab
set.

### Primary key design

Every table uses `BIGINT GENERATED ALWAYS AS IDENTITY`. This means:

- PKs are never included in `INSERT` column lists (the database assigns them).
- No `SERIAL` or application-generated UUIDs — this matches what you would
  see on a PostgreSQL 10+ production system.
- Workers and labs both benefit: the factories never need to track or generate
  IDs, and EXPLAIN plans show realistic sequential integer scans.

### `line_total` — GENERATED ALWAYS AS STORED

`order_items.line_total` is a computed column:

```sql
line_total NUMERIC(12,2) GENERATED ALWAYS AS
    (quantity * unit_price * (1 - discount_pct / 100)) STORED
```

It is physically stored on disk (not recalculated on every read). Workers never
write to it — any attempt to include it in an INSERT column list will fail with
an error. This is intentional: it teaches the distinction between stored and
virtual generated columns, and it makes the payments / order total validation
lab (INV-06) interesting.

---

## Repository Layout

```
OrderFlow/
├── bootstrap.py                   # one-command setup and process control
├── business_rules.md              # normative order lifecycle spec — frozen
├── architecture.md                # system design
├── roadmap.md                     # milestone tracker
│
├── database/
│   └── migrations/
│       └── 001_initial_schema.sql # the frozen schema DDL
│
├── python/
│   ├── config/                    # config.yaml loader + typed dataclasses
│   ├── factories/                 # 5 data factories + tests
│   └── workers/                   # 4 continuous workers + history_loader
│
├── docs/
│   └── configuration.md          # all tunable parameters with defaults
│
└── labs/                          # this directory — lab guides
    ├── LAB_TEMPLATE.md            # copy for each new lab
    ├── README.md                  # golden rule + lab index
    ├── 01_architecture/           # this write-up
    └── … (04–12 follow the Golden Rule template)
```

---

## Exploring the Schema

Connect and orient yourself:

```sql
\c orderflow
SET search_path = orderflow, public;

-- List all tables
\dt orderflow.*

-- Inspect a table's columns, types, and constraints
\d+ orders

-- Count rows in every table (verify bootstrap worked)
SELECT
    schemaname,
    relname         AS table_name,
    n_live_tup      AS approx_rows
FROM pg_stat_user_tables
WHERE schemaname = 'orderflow'
ORDER BY relname;
```

If the workers are running, `orders`, `order_items`, and `payments` row counts
will increase between runs of that last query.

---

## What This Enables

Lab 01 is the prerequisite for everything. Specifically:

| Later lab | What it needs from M1 |
|-----------|----------------------|
| All labs (04–12) | The frozen 7-table schema as the common baseline |
| Lab 05 (Indexes) | `orders`, `customers`, `order_items` with realistic FK relationships |
| Lab 06 (Partitioning) | `orders.created_at` as the natural RANGE partition key |
| Lab 07 (Replication) | The entire schema replicated to a standby |
| Lab 08 (PITR) | The schema + live workload WAL stream to recover against |
| Lab 09 (Security) | `employees.salary`, `customers.email` as PII columns for RLS + pgcrypto |
| Lab 10 (Extensions) | `products.metadata` JSONB for pgvector; `warehouses.region` for FDW |
| Lab 11 (Monitoring) | `employee_updates.py` dead tuples on `employees` for VACUUM/bloat |
