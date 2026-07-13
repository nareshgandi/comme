# Lab 06.01 — RANGE Partitioning: Converting a Live Orders Table

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Labs 04.01 and 05.01–05.14 complete; all four workers running.
**Estimated time:** 1.5 hr

> **This lab modifies the frozen schema.** Applying
> `database/migrations/002_partition_orders.sql` is the single approved
> exception to the "never revisit a frozen schema" rule. The design decision
> and the trade-off (dropping the FKs from `order_items` and `payments` to
> `orders`) are documented in `labs/06_partitioning/00_design_decision.md`.
> Read that document before running the migration.

---

## 1. Business Problem

The finance team runs a monthly revenue report:

```sql
SELECT DATE_TRUNC('week', created_at) AS week,
       COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1 ORDER BY 1;
```

This query was the focus of Lab 05.12 (BRIN index). The BRIN reduced it from
481 ms to ~89 ms. But BRIN is only a filter — it still reads every heap page
in the candidate block ranges, and as the table grows to millions of rows that
number rises. The DBA team's projection: at 10 M orders (~45 days of workload),
BRIN alone will no longer be sufficient.

The long-term fix is **declarative RANGE partitioning** on `created_at`. A
query for the last 90 days touches exactly 3–4 monthly partitions instead of
the entire table. Each partition can be independently vacuumed, indexed, and
eventually archived.

---

## 2. Observe (Baseline — before migration)

Capture the baseline query plan on the unpartitioned table. Run this before
applying the migration.

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Baseline: revenue report on the unpartitioned table
EXPLAIN (ANALYZE, BUFFERS)
SELECT DATE_TRUNC('week', created_at) AS week_start,
       COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1 ORDER BY 1;
```

Note the current table structure:

```sql
-- Confirm orders is an ordinary heap table (not yet partitioned)
SELECT relkind, relname
FROM   pg_class c
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  c.relname = 'orders' AND n.nspname = 'orderflow';
-- relkind = 'r'  (ordinary table)
```

Check the current table size:

```sql
SELECT pg_size_pretty(pg_total_relation_size('orderflow.orders')) AS total_size,
       pg_size_pretty(pg_relation_size('orderflow.orders'))        AS heap_size;
```

---

## 3. Measure (Baseline)

**Baseline:** Record actual timing from your EXPLAIN ANALYZE output. Typical
values from Lab 05.12 after the BRIN index:
- Execution time: ~89 ms
- Pages read: ~1 600 (the BRIN eliminates ~64 % of pages)
- Plan node: Bitmap Index Scan → Bitmap Heap Scan

The remaining heap page reads are the ceiling that partitioning breaks through.

---

## 4. Optimize — Apply the Migration

Stop all workers before running the migration. The copy step inside the
migration takes a full table lock for the duration of the INSERT.

```bash
python bootstrap.py --stop
```

Apply the migration:

```bash
psql -U postgres -d orderflow \
     -v ON_ERROR_STOP=1 \
     -f database/migrations/002_partition_orders.sql
```

The migration output includes NOTICE lines showing the partition range created
and the final row count. Read them — if any error fires, the transaction rolls
back and the original table is intact.

After the migration, restart workers:

```bash
python bootstrap.py
```

---

## 5. Measure Again

```sql
-- Revenue report on the partitioned table
EXPLAIN (ANALYZE, BUFFERS)
SELECT DATE_TRUNC('week', created_at) AS week_start,
       COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1 ORDER BY 1;
```

**Representative output after partitioning:**

```
Append  (cost=24.00..8230.50 rows=24451 width=48)
        (actual time=1.234..51.234 rows=13 loops=1)
  ->  Bitmap Heap Scan on orders_2024_10  ...
        Rows Removed by Recheck: 0
        Buffers: shared hit=412
  ->  Bitmap Heap Scan on orders_2024_11  ...
  ->  Bitmap Heap Scan on orders_2024_12  ...
Planning Time: 3.1 ms
Execution Time: 51.4 ms
```

Partitions not in the last 90 days are absent from the Append node — the
planner **pruned** them at planning time. The BRIN on each partition now covers
a much smaller, highly-correlated range of pages (covered in Lab 06.03).

```sql
-- Confirm table is now partitioned
SELECT relkind, relname
FROM   pg_class c
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  c.relname = 'orders' AND n.nspname = 'orderflow';
-- relkind = 'p'  (partitioned table)

-- See all partitions with row counts
SELECT tableoid::regclass AS partition,
       COUNT(*)            AS rows
FROM   orders
GROUP  BY 1
ORDER  BY 1;
```

**Delta:** ~89 ms → ~51 ms (pages read drops further as only 3–4 of N
partitions are scanned). More importantly: the plan is now *bounded* — adding
another year of history adds new partitions, but the 90-day query always
touches at most 4 partitions regardless of how large the total dataset grows.

---

## 6. Explain

**Declarative RANGE partitioning** (introduced as the default in PG10, matured
in PG11–PG13) divides a table into named child tables, each storing a specific
range of the partition key. Writes and reads are transparently routed by the
executor:

- **INSERT**: PostgreSQL evaluates `created_at` against the partition bounds
  and inserts the row into the matching child table. Because `created_at`
  has `DEFAULT NOW()`, the workers supply no explicit value and are routed
  correctly without any code change.
- **SELECT with partition-key predicate**: the planner compares the predicate's
  constant against each partition's bound and excludes partitions that cannot
  contain matching rows — this is **partition pruning** (Lab 06.02).
- **FOR UPDATE SKIP LOCKED**: works per-partition row, exactly as before.

**Why the PK changed:** PostgreSQL requires that every UNIQUE and PRIMARY KEY
constraint on a partitioned table includes the partition key column. The old
`PRIMARY KEY (order_id)` is replaced by `PRIMARY KEY (order_id, created_at)`.
The approved consequence: the FKs from `order_items` and `payments` to
`orders(order_id)` are dropped. The `order_id` values remain globally unique
by construction (monotonic identity sequence), so orphan rows are structurally
impossible from the workers.

**Worker verification:**

```sql
-- Confirm all workers are routing correctly: last few rows go to today's partition
SELECT tableoid::regclass AS partition,
       MAX(created_at)    AS latest_row
FROM   orders
GROUP  BY 1
ORDER  BY 2 DESC
LIMIT  3;
-- Top row should be the current month's partition (e.g. orders_2024_12)
```

---

## 7. Cleanup / Reset Note

The partitioned table **persists** — it is the new permanent baseline for all
subsequent labs (06.02 through 06.06) and for the monitoring lab (Lab 11).

The dropped FKs are the approved trade-off (documented in
`00_design_decision.md`); they are NOT restored. Run the post-migration
verification queries to confirm INV-11 and INV-12:

```sql
-- INV-11: no orphan order_items
SELECT COUNT(*) AS orphan_items
FROM   order_items oi
WHERE  NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = oi.order_id);

-- INV-12: no orphan payments
SELECT COUNT(*) AS orphan_payments
FROM   payments p
WHERE  NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = p.order_id);
-- Both should return 0.
```

---

## Further Reading

- [PostgreSQL docs — Declarative Partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- [PostgreSQL docs — Partition Pruning](https://www.postgresql.org/docs/current/ddl-partitioning.html#DDL-PARTITION-PRUNING)
