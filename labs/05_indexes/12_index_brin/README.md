# Lab 05.12 — BRIN Index: Block Range Indexes for Naturally Ordered Data

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.11 complete; workers running.
**Estimated time:** 1 hr

---

## 1. Business Problem

Finance runs a monthly revenue report: "Total and average order value for all
orders placed in the last 3 months, broken down by week." The query filters
`orders` on `created_at` — a date range scan. The `orders` table has 500 000
rows and will grow by ~2.5 rows/second indefinitely. The schema comment for
`orders.created_at` explicitly anticipates this lab: *"Do not add a BRIN index
here — that is Lab 05's work."*

A B-tree index on `created_at` would work, but it would be ~9 MB for 500 000
rows and grow proportionally. BRIN is designed exactly for this case: monotonic
or near-monotonic data where new rows are always appended in order. A BRIN index
on `orders.created_at` over 500 000 rows is roughly 50 KB — 180× smaller.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- The monthly revenue report query
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    DATE_TRUNC('week', created_at)  AS week_start,
    COUNT(*)                         AS orders,
    SUM(total_amount)                AS total_revenue,
    AVG(total_amount)                AS avg_order_value
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1
ORDER BY 1;
```

**Representative output (~500 000 orders, no index on created_at):**

```
HashAggregate  (cost=16012.50..16015.50 rows=13 width=48)
               (actual time=481.234..481.312 rows=13 loops=1)
  Buffers: shared hit=312 read=4141
  -> Seq Scan on orders  (cost=0.00..15890.25 rows=24451 width=16)
                         (actual time=0.054..419.234 rows=24380 loops=1)
       Filter: (created_at >= (now() - '90 days'::interval))
       Rows Removed by Filter: 475620
       Buffers: shared hit=312 read=4141
Planning Time: 0.189 ms
Execution Time: 481.423 ms
```

Now check the physical correlation of `created_at` with heap insertion order —
this is what makes BRIN effective:

```sql
SELECT correlation
FROM   pg_stats
WHERE  tablename = 'orders'
  AND  attname = 'created_at';
-- Should be very close to 1.0 — rows are inserted in timestamp order
-- by the workers (they always use NOW())
```

---

## 3. Measure (Baseline)

**Baseline:** ~481 ms  |  Seq Scan reading ~4 453 pages  |  475 620 rows
scanned to find 24 380 matching (last 90 days)

**Physical correlation of `created_at`:** ~0.99 (near-perfect — `order_generator.py`
always inserts with `created_at = NOW()`, so rows are physically stored in
timestamp order on disk)

---

## 4. Optimize

First compare index sizes, then add the BRIN:

```sql
-- Temporarily build a B-tree to compare sizes
CREATE INDEX idx_orders_created_at_btree
    ON orders (created_at);

SELECT
    indexname,
    pg_size_pretty(pg_relation_size(indexname::regclass)) AS index_size
FROM pg_indexes
WHERE tablename = 'orders'
  AND indexname IN ('idx_orders_created_at_btree');
-- Example output: idx_orders_created_at_btree | 11 MB

DROP INDEX idx_orders_created_at_btree;

-- Now build the BRIN index
CREATE INDEX idx_orders_created_at_brin
    ON orders USING brin (created_at)
    WITH (pages_per_range = 128);
```

```sql
-- Check the BRIN index size
SELECT pg_size_pretty(pg_relation_size('idx_orders_created_at_brin'::regclass));
-- Example output: 56 kB  (≈ 200× smaller than the B-tree)
```

This index **persists as a permanent baseline change** — it is the correct
long-term index for `orders.created_at` and is referenced in the partitioning
and monitoring labs.

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    DATE_TRUNC('week', created_at) AS week_start,
    COUNT(*)                        AS orders,
    SUM(total_amount)               AS total_revenue,
    AVG(total_amount)               AS avg_order_value
FROM   orders
WHERE  created_at >= NOW() - INTERVAL '90 days'
GROUP BY 1
ORDER BY 1;
```

**Representative output (BRIN index, pages_per_range=128):**

```
HashAggregate  (cost=8234.50..8237.50 rows=13 width=48)
               (actual time=89.234..89.312 rows=13 loops=1)
  Buffers: shared hit=1203 read=412
  -> Bitmap Heap Scan on orders  (cost=24.00..8112.25 rows=24451 width=16)
                                  (actual time=2.234..72.891 rows=24380 loops=1)
       Recheck Cond: (created_at >= ...)
       Rows Removed by Recheck: 1205
       Buffers: shared hit=1203 read=412
       -> Bitmap Index Scan on idx_orders_created_at_brin
                    (cost=0.00..18.00 rows=24451 width=0)
                    (actual time=1.234..1.234 rows=24380 loops=1)
            Index Cond: (created_at >= ...)
Planning Time: 0.198 ms
Execution Time: 89.423 ms
```

**After:** ~89 ms  |  ~1 615 pages read (vs 4 453)
**Delta:** ~5.4× faster; pages read reduced 64 % — and the index is only 56 KB
vs. ~11 MB for an equivalent B-tree

---

## 6. Explain

A BRIN (Block Range INdex) does not index individual rows. It divides the heap
into fixed-size **ranges of pages** (default 128 pages = 1 MB each) and stores,
for each range, the **minimum and maximum** value of the indexed column within
that range.

For a query `WHERE created_at >= '2024-01-01'`, the BRIN scan:
1. Reads the BRIN index (56 KB — fits in a single I/O).
2. Eliminates every page range whose maximum `created_at` is before 2024-01-01.
3. Reads only the candidate page ranges — the ones whose range includes the
   query boundary.
4. Performs a Bitmap Heap Scan on those pages (with a recheck for boundary
   ranges where rows from both sides of the cutoff exist).

**Why correlation matters:** BRIN is only effective when rows are physically
stored in the same order as their indexed values (high correlation ≥ ~0.9).
If old and new orders were interleaved on disk (correlation near 0), every
page range would contain both early and late timestamps — all ranges would be
candidates, and BRIN would be useless. `order_generator.py` always inserts
with `created_at = NOW()`, so rows are physically appended in timestamp order.
BRIN's min/max ranges are tight and selective.

**When to choose BRIN over B-tree:**
- Monotonically or near-monotonically increasing columns (serial IDs, insert
  timestamps, log sequence numbers)
- Queries that read a significant fraction of the table (5–30 %) — B-tree is
  better for highly selective single-row lookups
- Disk space is a constraint — BRIN is 100–1 000× smaller than B-tree
- Write throughput matters — BRIN has minimal write overhead (updates the
  range summary rather than inserting an index entry per row)

**When NOT to use BRIN:**
- Random insert order (correlation < 0.5) — range summaries are too loose
- Highly selective equality lookups — a B-tree or partial index is better
- The table is small enough that a Seq Scan is already fast

---

## 7. Cleanup / Reset Note

`idx_orders_created_at_brin` **persists** as a permanent baseline change.
It is the correct production index for `orders.created_at` and is referenced
in the partitioning lab (Lab 06) and the monitoring lab (Lab 11).

---

## Further Reading

- [PostgreSQL docs — BRIN Indexes](https://www.postgresql.org/docs/current/brin.html)
- [PostgreSQL docs — pg_stats (correlation)](https://www.postgresql.org/docs/current/view-pg-stats.html)
