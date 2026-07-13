# Lab 06.03 — BRIN Revisited: Per-Partition Correlation vs. Whole-Table BRIN

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Lab 06.01 and 06.02 complete; Lab 05.12 BRIN theory understood.
**Estimated time:** 45 min

---

## 1. Business Problem

Lab 05.12 built a BRIN index on the unpartitioned `orders` table and measured
~0.99 physical correlation for `created_at`. That BRIN is now recreated per
partition by Migration 002. The infrastructure team asks: does per-partition
BRIN outperform the old whole-table BRIN, and if so, by how much and why?

---

## 2. Observe

Check the BRIN index now present on the partitioned table and its partitions:

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- BRIN indexes on the partitioned table and its partitions
SELECT c.relname AS table_or_partition,
       i.relname AS index_name,
       pg_size_pretty(pg_relation_size(i.oid)) AS index_size
FROM   pg_index  idx
JOIN   pg_class  c ON c.oid = idx.indrelid
JOIN   pg_class  i ON i.oid = idx.indexrelid
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  n.nspname = 'orderflow'
  AND  c.relname LIKE 'orders%'
  AND  i.relname LIKE '%brin%'
ORDER  BY c.relname;
```

Check physical correlation per partition:

```sql
-- Correlation for created_at within each partition
SELECT relname AS partition,
       attname AS column,
       ROUND(correlation::NUMERIC, 4) AS correlation
FROM   pg_stats   s
JOIN   pg_class   c ON c.relname = s.tablename
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  n.nspname = 'orderflow'
  AND  c.relname LIKE 'orders_%'
  AND  s.attname = 'created_at'
  AND  s.schemaname = 'orderflow'
ORDER  BY relname;
-- Each partition should show correlation = 1.0 or very close.
```

---

## 3. Measure (Baseline)

**Old whole-table BRIN (from Lab 05.12):**
- Correlation: ~0.99 across the whole table
- Index size: ~56 KB for 500 000 rows
- pages_per_range = 128

**Per-partition BRIN (now):**
- Record the per-partition correlation values
- Record the size of each partition's BRIN index

Compare total size of all per-partition BRINs vs the old single BRIN:

```sql
SELECT pg_size_pretty(SUM(pg_relation_size(i.oid))) AS total_brin_size
FROM   pg_index  idx
JOIN   pg_class  c ON c.oid = idx.indrelid
JOIN   pg_class  i ON i.oid = idx.indexrelid
JOIN   pg_namespace n ON n.oid = c.relnamespace
WHERE  n.nspname = 'orderflow'
  AND  c.relname LIKE 'orders_%'
  AND  i.relname LIKE '%brin%';
```

---

## 4. Optimize — Run the revenue report and observe per-partition BRIN behavior

```sql
-- Revenue report: the same query from Lab 05.12 and Lab 06.01
EXPLAIN (ANALYZE, BUFFERS)
SELECT DATE_TRUNC('week', created_at) AS week_start,
       COUNT(*),
       SUM(total_amount)
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP  BY 1
ORDER  BY 1;
```

Look specifically at the Bitmap Index Scan nodes — each should show the BRIN
index on the relevant partition, and the `Rows Removed by Recheck` value should
be very low (because the per-partition BRIN ranges are tight).

Now compare a query scoped to a single partition:

```sql
-- Single-month query: hits exactly one partition, one BRIN
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= '2024-10-01'
  AND  created_at <  '2024-11-01';
-- Expected: Bitmap Index Scan on orders_2024_10's BRIN → tiny number of
-- block ranges to check, Rows Removed by Recheck ≈ 0.
```

---

## 5. Measure Again

**After partitioning:**
- Per-partition correlation: 1.0 (every row inserted into a monthly partition
  arrived via `created_at = NOW()` in strict time order — no older rows were
  mixed in after the initial copy)
- BRIN `Rows Removed by Recheck` per partition: ~0 (ranges are tight because
  the partition boundary itself limits the data range)
- Single-month query: typically 1–3 ms (one small partition, one tiny BRIN
  with near-zero false positives)

**Why per-partition BRIN is more effective than whole-table BRIN:**

| | Whole-table BRIN | Per-partition BRIN |
|---|---|---|
| Correlation | ~0.99 (theoretical) | 1.0 (by construction) |
| Block range covers | 128 pages of mixed months | 128 pages of exactly one month |
| False positive ranges | Some at month boundaries | Near zero (month boundary IS partition boundary) |
| Index size (per partition) | N/A | ~24 KB per 50k-row partition |
| Total index size | 56 KB | Similar total, but per-partition overhead |

---

## 6. Explain

A BRIN block range stores the MIN and MAX of `created_at` for every 128 heap
pages. For the old monolithic table, each block range spanned up to 128 pages
of rows from many different months. At month boundaries, block ranges could
contain rows from both the previous and current month — making those boundary
ranges non-prunable even for single-month queries.

Within a monthly partition, all rows were inserted with `created_at = NOW()`
during that specific calendar month. The heap is physically laid out in
insertion order (no updates rearrange rows into different pages), so every
block range contains rows from exactly the same month. The BRIN's MIN and MAX
for any given block range in a monthly partition are within a few hours of each
other — dramatically tighter than a whole-table BRIN.

This means the Bitmap Heap Scan's "Recheck" step (which discards false-positive
rows at partition boundaries) has almost nothing to recheck. The BRIN on a
monthly partition is nearly perfectly selective.

**The compound benefit:** partition pruning eliminates non-relevant months from
the Append node, then BRIN within each surviving partition eliminates nearly
all non-relevant block ranges. The two mechanisms compose cleanly.

---

## 7. Cleanup / Reset Note

No schema changes in this lab. The per-partition BRIN indexes (`idx_orders_created_at_brin`)
were created by Migration 002 and **persist** as the correct production baseline.

---

## Further Reading

- [PostgreSQL docs — BRIN Indexes](https://www.postgresql.org/docs/current/brin.html)
- [PostgreSQL docs — pg_stats (correlation)](https://www.postgresql.org/docs/current/view-pg-stats.html)
