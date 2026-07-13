# Lab 06.06 — Archive: DETACH and ATTACH Partitions

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Lab 06.01 complete; `orders` is partitioned; workers running.
**Estimated time:** 1 hr

---

## 1. Business Problem

After a year of operation, the cluster has 14+ monthly partitions. The oldest
ones — say, everything older than 12 months — contain only terminal-state
orders (`DELIVERED` or `REFUNDED`). Those partitions are read-only in practice:
no worker updates them, and no new rows arrive. But they occupy primary storage
and bloat every full-table scan.

The operations team wants to:
1. **Detach** old closed partitions from the live `orders` table (primary
   cluster no longer carries them; partition-pruning queries ignore them).
2. **Archive** the detached table to a separate schema for compliance-driven
   long-term access.
3. **Attach** a freshly-created future partition to prove the reverse operation
   (onboarding a pre-populated partition from an import or migration).

The business rule: **only partitions containing exclusively terminal-state
orders are candidates for archival.** An order in `NEW`, `PROCESSING`, `PACKED`,
`SHIPPED`, or `RETURNED` is still active.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

Identify archival candidates — partitions where every row is in a terminal
state:

```sql
SET search_path = orderflow, public;

-- List all partitions with their row counts and status breakdown
SELECT c.relname               AS partition,
       COUNT(*)                AS total_rows,
       SUM(CASE WHEN o.status IN ('DELIVERED', 'REFUNDED') THEN 1 ELSE 0 END)
                               AS terminal_rows,
       SUM(CASE WHEN o.status NOT IN ('DELIVERED', 'REFUNDED') THEN 1 ELSE 0 END)
                               AS active_rows
FROM   pg_inherits  i
JOIN   pg_class     p ON p.oid = i.inhparent
JOIN   pg_class     c ON c.oid = i.inhchild
JOIN   pg_namespace n ON n.oid = p.relnamespace
JOIN   orders       o ON o.tableoid = c.oid
WHERE  p.relname  = 'orders'
  AND  n.nspname  = 'orderflow'
  AND  c.relname != 'orders_default'
GROUP  BY c.relname
ORDER  BY c.relname;
```

Any partition where `active_rows = 0` is a candidate.

```sql
-- Confirm the archive schema does not yet exist
SELECT nspname FROM pg_namespace WHERE nspname = 'orderflow_archive';
```

---

## 3. Measure (Baseline)

**Baseline:**
1. Record which partitions have `active_rows = 0` (candidates).
2. Record total size of the live `orders` table:

```sql
SELECT pg_size_pretty(pg_total_relation_size('orderflow.orders')) AS live_orders_size;
```

3. A full-table scan (no date predicate) visits all partitions:

```sql
EXPLAIN SELECT COUNT(*) FROM orders;
-- Note the number of child scan nodes in the Append.
```

---

## 4. Optimize

### Step 1 — Create the archive schema

```sql
CREATE SCHEMA IF NOT EXISTS orderflow_archive;
```

### Step 2 — Identify and detach a candidate partition

Pick the oldest partition confirmed to have `active_rows = 0` from the Observe
step. Substitute the actual name below.

```sql
-- Example: detach the oldest all-terminal partition
-- DETACH PARTITION is a metadata operation — no rows are moved.
ALTER TABLE orderflow.orders
    DETACH PARTITION orderflow.orders_2024_01;
```

After DETACH, `orders_2024_01` is a standalone ordinary table in the
`orderflow` schema. It is no longer part of `orders`:
- Queries against `orders` with any date predicate will never touch it.
- The table still exists and is queryable directly.
- All indexes (BRIN, customer_id, etc.) are still present on it.

```sql
-- Verify: the partition no longer appears in the inherits list
SELECT c.relname FROM pg_inherits i
JOIN pg_class p ON p.oid = i.inhparent
JOIN pg_class c ON c.oid = i.inhchild
WHERE p.relname = 'orders'
ORDER BY c.relname;
-- orders_2024_01 should be absent.

-- Verify: data is still queryable directly
SELECT COUNT(*) FROM orderflow.orders_2024_01;
SELECT status, COUNT(*) FROM orderflow.orders_2024_01 GROUP BY status;
-- All rows should be DELIVERED or REFUNDED.
```

### Step 3 — Move the detached table to the archive schema

```sql
ALTER TABLE orderflow.orders_2024_01
    SET SCHEMA orderflow_archive;
-- The table is now orderflow_archive.orders_2024_01
-- It is accessible to any role with SELECT on the archive schema.
```

Verify:

```sql
SELECT COUNT(*) FROM orderflow_archive.orders_2024_01;
-- Still queryable; just in a different schema.
```

### Step 4 — Confirm live orders size shrank

```sql
SELECT pg_size_pretty(pg_total_relation_size('orderflow.orders')) AS live_orders_size_after;
-- Should be smaller by the size of the detached partition.

EXPLAIN SELECT COUNT(*) FROM orders;
-- The Append node now has one fewer child scan.
```

### Step 5 — Demonstrate ATTACH PARTITION (reverse operation)

Create a future partition that doesn't exist yet, populate it with test rows,
then attach it:

```sql
-- Create a standalone table with the same structure as orders (inheriting columns)
-- using LIKE to copy the column definitions
CREATE TABLE orderflow.orders_future_demo
    (LIKE orderflow.orders INCLUDING DEFAULTS INCLUDING CONSTRAINTS);

-- Insert a test row in the correct range for January 2030
INSERT INTO orderflow.orders_future_demo
    (order_id, customer_id, status, total_amount, shipping_country, created_at, updated_at)
SELECT 999999999, customer_id, 'DELIVERED', 0.01, 'US',
       '2030-01-15'::timestamptz, NOW()
FROM customers LIMIT 1;

-- Attach it as a new partition for January 2030
-- PostgreSQL validates: every row in the table must satisfy the partition bounds.
ALTER TABLE orderflow.orders
    ATTACH PARTITION orderflow.orders_future_demo
    FOR VALUES FROM ('2030-01-01'::timestamptz)
              TO   ('2030-02-01'::timestamptz);

-- Verify the attachment
SELECT c.relname FROM pg_inherits i
JOIN pg_class p ON p.oid = i.inhparent
JOIN pg_class c ON c.oid = i.inhchild
WHERE p.relname = 'orders' ORDER BY c.relname DESC LIMIT 3;
-- orders_future_demo should appear.

-- Clean up the demo partition
ALTER TABLE orderflow.orders DETACH PARTITION orderflow.orders_future_demo;
DROP TABLE orderflow.orders_future_demo;
```

---

## 5. Measure Again

```sql
-- Live orders size after archival
SELECT pg_size_pretty(pg_total_relation_size('orderflow.orders')) AS after_archive;

-- Archived data still accessible
SELECT COUNT(*) FROM orderflow_archive.orders_2024_01;

-- Full-table EXPLAIN shows fewer child scan nodes
EXPLAIN SELECT COUNT(*) FROM orders;
```

**Delta:** The primary `orders` table is smaller by one partition's worth of
storage. Full-table scans have one fewer Append child. Date-range queries
covering 2024-01 no longer see that data via `orders` — they must query
`orderflow_archive.orders_2024_01` directly if needed.

---

## 6. Explain

`ALTER TABLE ... DETACH PARTITION` is a **metadata-only** operation. It removes
the partition from the parent's inheritance list and updates the partition
constraint catalog. No rows are moved, copied, or deleted. It completes in
milliseconds regardless of partition size.

After detach:
- The table becomes an ordinary heap table with no partition bounds.
- All its indexes remain intact.
- PostgreSQL routes no new inserts to it (because it is no longer a partition).
- Workers querying `orders` do not see its rows.

`ALTER TABLE ... ATTACH PARTITION` is the reverse. PostgreSQL runs a constraint
validation scan: every row in the being-attached table must satisfy the partition
bounds. For large tables this scan can be slow; use `ATTACH PARTITION ... NOT
VALIDATED` (PG15+) to skip validation and add a background check instead.

**Why terminal-state gates matter:** The business rule "only detach all-terminal
partitions" exists because detached rows become invisible to `order_processor.py`
and `payment_processor.py`. If a RETURNED order's partition were detached, the
refund worker would never find it. The status-check query in the Observe step
enforces this gate.

**Archival schema:** Moving the detached table to `orderflow_archive` schema:
- Keeps the primary `orderflow` schema clean.
- Enables schema-level permission control (analysts get SELECT on archive; app
  role `orderflow` gets no privileges on archive by default).
- Is reversible: `ALTER TABLE orderflow_archive.orders_2024_01 SET SCHEMA orderflow`
  followed by `ATTACH PARTITION` would restore the data to the live table.

---

## 7. Cleanup / Reset Note

The `orderflow_archive` schema **persists** as a permanent repo baseline — it
is the archive destination for all future partition retirements and is
referenced in the monitoring lab (Lab 11) and the security lab (Lab 09 — RLS
on the archive schema).

The detached partition `orderflow_archive.orders_2024_01` also **persists** —
it demonstrates the archive pattern and can be used for PITR exercises in Lab 08.

The demonstration `orders_future_demo` partition is dropped in Step 5 above
(cleanup is in-lab).

---

## Further Reading

- [PostgreSQL docs — ALTER TABLE DETACH PARTITION](https://www.postgresql.org/docs/current/sql-altertable.html)
- [PostgreSQL docs — ALTER TABLE ATTACH PARTITION](https://www.postgresql.org/docs/current/sql-altertable.html)
- `scripts/provision_monthly_partition.py` — create future partitions ahead of schedule
