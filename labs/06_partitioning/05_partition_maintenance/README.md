# Lab 06.05 — Partition Maintenance: VACUUM, Indexes, and Monthly Provisioning

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Lab 06.01 complete; `orders` is partitioned; workers running.
**Estimated time:** 1 hr

> **Automation deferred:** This lab teaches partition maintenance by hand.
> `pg_partman` and `pg_cron` — the tools that automate what you do here — are
> covered in Milestone 13 (Extensions). Doing it manually first teaches what
> those extensions are actually automating, and what goes wrong when they don't
> run.

---

## 1. Business Problem

Autovacuum, VACUUM, ANALYZE, and REINDEX all work differently on a partitioned
table than on a monolithic one. The operations are *per-partition*, and the
DBA must understand the implications before setting autovacuum thresholds or
scheduling maintenance windows.

A separate concern: `order_generator.py` inserts ~2.5 orders/second. At the
start of each new calendar month, a new monthly partition must exist before the
first insert of that month — otherwise the row lands in `orders_default`
(the fallback partition), which should stay empty.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- 2a: Per-partition autovacuum stats (each partition is an independent table)
SELECT c.relname            AS partition,
       t.n_live_tup,
       t.n_dead_tup,
       ROUND(t.n_dead_tup * 100.0 / NULLIF(t.n_live_tup + t.n_dead_tup, 0), 1)
                            AS dead_pct,
       t.last_autovacuum,
       t.last_autoanalyze
FROM   pg_stat_user_tables t
JOIN   pg_class            c ON c.relname = t.relname
JOIN   pg_namespace        n ON n.oid = c.relnamespace
WHERE  n.nspname   = 'orderflow'
  AND  c.relname  LIKE 'orders_%'
ORDER  BY c.relname;
```

```sql
-- 2b: Per-partition index sizes
SELECT c.relname       AS partition,
       i.relname       AS index_name,
       pg_size_pretty(pg_relation_size(i.oid)) AS index_size
FROM   pg_class     c
JOIN   pg_namespace n ON n.oid = c.relnamespace
JOIN   pg_index     x ON x.indrelid = c.oid
JOIN   pg_class     i ON i.oid = x.indexrelid
WHERE  n.nspname   = 'orderflow'
  AND  c.relname  LIKE 'orders_%'
ORDER  BY c.relname, i.relname;
```

```sql
-- 2c: Check if the DEFAULT partition has any rows (it should be empty)
SELECT COUNT(*) AS rows_in_default FROM orders_default;
-- If > 0: a month partition was missing when rows were inserted.
-- Run the provisioning script (scripts/provision_monthly_partition.py) to fix.
```

---

## 3. Measure (Baseline)

**Baseline observations:**
1. `n_dead_tup` per partition — older partitions with completed orders (all
   DELIVERED or REFUNDED) will have low dead-tuple counts; the current-month
   partition will have higher activity.
2. `last_autovacuum` timestamps — autovacuum treats each partition as an
   independent table; it may not have run on every partition yet.
3. `orders_default` row count — should be 0.

---

## 4. Optimize

### 4a — Per-partition VACUUM ANALYZE

```sql
-- VACUUM a specific partition directly by its child table name
VACUUM (VERBOSE, ANALYZE) orders_2024_10;

-- Or trigger VACUUM on the parent (runs on ALL partitions)
VACUUM ANALYZE orders;
-- Note: vacuuming the parent is convenient but may trigger unnecessary work
-- on recently-vacuumed partitions.  For targeted maintenance, vacuum
-- individual partitions.
```

Per-partition autovacuum configuration (from Lab 05.14):

```sql
-- Older "cold" partitions with low update/delete activity can use a more
-- aggressive scale factor to minimize dead-tuple accumulation
ALTER TABLE orders_2024_01 SET (
    autovacuum_vacuum_scale_factor = 0.02,
    autovacuum_vacuum_cost_delay   = 2
);

-- The active current-month partition inherits the payments table's tuning
-- set in Lab 05.14; no change needed here.
```

### 4b — Per-partition index maintenance

```sql
-- REINDEX a specific partition's index (blocks writes to that partition only)
REINDEX INDEX CONCURRENTLY idx_orders_customer_id_orders_2024_10;

-- List the actual index names on a specific partition:
SELECT indexname FROM pg_indexes
WHERE  tablename = 'orders_2024_10' AND schemaname = 'orderflow';
```

Note: `REINDEX INDEX CONCURRENTLY` on the parent index name operates on the
entire partitioned index, recreating sub-indexes per-partition. On a live
system, target individual partition indexes to limit the scope of concurrent
index rebuilds.

### 4c — Manual monthly partition creation

**The problem:** the migration created partitions through current month + 2.
After those months pass, new rows will land in `orders_default`.

**Manual steps — create the next month's partition by hand:**

```sql
-- Example: creating the March 2025 partition manually
CREATE TABLE orderflow.orders_2025_03
    PARTITION OF orderflow.orders
    FOR VALUES FROM ('2025-03-01'::timestamptz)
              TO   ('2025-04-01'::timestamptz);
```

PostgreSQL automatically applies the parent table's indexes and triggers to
the new partition.

**Verify the new partition is registered:**

```sql
SELECT c.relname AS partition
FROM   pg_inherits  i
JOIN   pg_class     p ON p.oid = i.inhparent
JOIN   pg_class     c ON c.oid = i.inhchild
JOIN   pg_namespace n ON n.oid = p.relnamespace
WHERE  p.relname = 'orders' AND n.nspname = 'orderflow'
ORDER  BY c.relname;
```

### 4d — Use the provisioning script for automation

`scripts/provision_monthly_partition.py` performs the same logic as the
migration's DO block: checks which months are already provisioned, and creates
any missing months up to the configured lookahead window. Run it as a cron job
or manually at the start of each month.

```bash
# Provision through current month + 2 (default)
python scripts/provision_monthly_partition.py

# Provision through a specific end month
python scripts/provision_monthly_partition.py --through 2025-06
```

> This script is the manual equivalent of what `pg_partman` + `pg_cron` would
> automate in Milestone 13. Understanding what it does — and what happens when
> it's late — is the point of this lab.

---

## 5. Measure Again

```sql
-- After VACUUM: dead tuple counts should drop
SELECT c.relname, n_live_tup, n_dead_tup, last_autovacuum
FROM   pg_stat_user_tables t
JOIN   pg_class            c ON c.relname = t.relname
JOIN   pg_namespace        n ON n.oid = c.relnamespace
WHERE  n.nspname = 'orderflow'
  AND  c.relname LIKE 'orders_%';

-- After adding a new partition: it appears in the inherits query
-- After running provisioning script: orders_default should still be 0
SELECT COUNT(*) FROM orders_default;
```

**Delta:** Each partition is vacuumed independently. Old "cold" partitions
(no more new writes) accrue no new dead tuples after their final VACUUM and
can be maintained on a low-frequency schedule. The current-month partition
needs frequent autovacuum tuning (as set for `payments` in Lab 05.14).

---

## 6. Explain

**Per-partition autovacuum:** autovacuum treats every partition as an
independent heap table. Each has its own `pg_stat_user_tables` entry, its own
dead-tuple counter, and its own autovacuum threshold calculation. The
`autovacuum_vacuum_scale_factor = 0.2` default means autovacuum fires when
20 % of a partition's rows are dead — useful for active partitions, wasteful
for cold ones.

**Why cold partitions are special:** once all orders in a partition have reached
terminal states (DELIVERED, REFUNDED), the `order_processor` worker stops
writing to it. Dead tuples from historical updates accumulate but new ones
stop arriving. A single VACUUM ANALYZE run "seals" the partition — no further
autovacuum work is needed until it is archived or detached.

**The provisioning gap:** if no partition exists for a given month when an
INSERT arrives, PostgreSQL routes the row to the DEFAULT partition. The DEFAULT
partition is a design safety net, not a production destination. A non-empty
DEFAULT partition means provisioning fell behind and must be corrected: create
the missing partition(s) and then move rows from DEFAULT into the correct child
using `INSERT ... SELECT ... DELETE` or `ALTER TABLE ATTACH PARTITION` (Lab 06.06).

**Why manual before automated:** `pg_partman` automates partition creation and
retention policies. But if the configuration is wrong, it silently fails to
create partitions, and rows accumulate in DEFAULT. Knowing the manual steps
means you can diagnose and fix `pg_partman` failures when they occur in
production.

---

## 7. Cleanup / Reset Note

The `autovacuum_vacuum_scale_factor` override on `orders_2024_01` **persists**
as a reasonable production tuning for cold partitions.

The provisioning script at `scripts/provision_monthly_partition.py` is a
permanent repo asset — it should be scheduled as a monthly cron job until
`pg_partman` is installed in Milestone 13.

No demonstration-only objects were created in this lab.

---

## Further Reading

- [PostgreSQL docs — autovacuum](https://www.postgresql.org/docs/current/runtime-config-autovacuum.html)
- [PostgreSQL docs — REINDEX](https://www.postgresql.org/docs/current/sql-reindex.html)
- `scripts/provision_monthly_partition.py` — the manual provisioning script
