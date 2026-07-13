# Lab 05.04 — Partial Index: Index Only What You Query

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.03 complete; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

`payment_processor.py` runs every 2 seconds and executes the same query
thousands of times per hour: "Give me the next batch of NEW orders that need a
payment attempt." The `orders` table grows by ~2.5 rows per second and currently
has 500 000 rows, but the vast majority are in terminal states (`DELIVERED`,
`RETURNED`, `REFUNDED`) — roughly 90 % of the table. A full B-tree index on
`orders.status` would index all 500 000 rows, but the payment worker only ever
queries two of seven status values. The other 450 000 entries in that index are
dead weight — they consume disk space, slow down every write to `orders`, and
pollute the buffer cache.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Check current status distribution
SELECT status, COUNT(*) AS cnt,
       ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
FROM orders
GROUP BY status
ORDER BY cnt DESC;
```

Typical output on a running system shows 40–50 % DELIVERED, 20 % SHIPPED, 12 %
RETURNED, etc. Only 4–6 % of rows are NEW or PROCESSING.

```sql
-- The payment worker's hot query
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id
FROM   orders
WHERE  status = 'NEW'
ORDER BY created_at
LIMIT  50
FOR UPDATE SKIP LOCKED;
```

**Representative output (no index on status):**

```
LockRows  (cost=15890.61..15891.86 rows=2500 width=14)
           (actual time=413.214..413.287 rows=50 loops=1)
  -> Limit  (cost=15890.61..15891.49 rows=50 width=14)
       -> Sort  (cost=15890.61..17015.61 rows=2500 width=14)
            Sort Key: created_at
            Sort Method: top-N heapsort  Memory: 29kB
            -> Seq Scan on orders  (cost=0.00..15828.12 rows=2500 width=14)
                                   (actual time=0.152..399.234 rows=23412 loops=1)
                 Filter: ((status)::text = 'NEW')
                 Rows Removed by Filter: 476588
                 Buffers: shared hit=312 read=4141
Planning Time: 0.198 ms
Execution Time: 413.401 ms
```

---

## 3. Measure (Baseline)

**Baseline:** ~413 ms per payment worker cycle  |  Seq Scan reading ~4 453
pages  |  Worker runs every 2 s → ~207 full-table scans per hour, consuming
~185 000 buffer reads per minute from the buffer cache

---

## 4. Optimize

```sql
-- Index ONLY the rows the payment worker actually queries.
-- The partial predicate eliminates ~95% of the table from the index entirely.
CREATE INDEX idx_orders_active_status_created
    ON orders (created_at)
    WHERE status IN ('NEW', 'PROCESSING');
```

The index is dramatically smaller than a full index: it tracks only ~25 000
rows (the active orders) instead of 500 000, and it shrinks automatically as
orders advance to terminal states.

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id
FROM   orders
WHERE  status = 'NEW'
ORDER BY created_at
LIMIT  50
FOR UPDATE SKIP LOCKED;
```

**Representative output after partial index:**

```
LockRows  (cost=0.56..4.38 rows=50 width=14)
           (actual time=0.041..0.058 rows=50 loops=1)
  -> Limit  (cost=0.56..4.26 rows=50 width=14)
       -> Index Scan using idx_orders_active_status_created on orders
                    (cost=0.56..1521.23 rows=2500 width=14)
                    (actual time=0.038..0.052 rows=50 loops=1)
            Index Cond: (created_at IS NOT NULL)
            Filter: ((status)::text = 'NEW')
            Buffers: shared hit=4
Planning Time: 0.234 ms
Execution Time: 0.073 ms
```

**After:** ~0.07 ms per cycle  |  4 buffer reads
**Delta:** ~5 800× faster; buffer pool savings: ~185 000 reads/min → ~660
reads/min (99.6% reduction in cache pressure from the payment worker alone)

---

## 6. Explain

A partial index includes only rows matching the `WHERE` predicate at index-
build time, and stays in sync with the table: as `payment_processor.py`
advances orders from NEW → PROCESSING, those rows move within the index. When
an order reaches PACKED and is no longer in `('NEW', 'PROCESSING')`, its entry
is deleted from the index automatically. The index never grows larger than the
current count of active orders.

**Why the planner uses it:** PostgreSQL's planner knows the partial index
predicate. When a query's `WHERE` clause implies the predicate (here,
`status = 'NEW'` implies `status IN ('NEW', 'PROCESSING')`), the planner
considers the partial index as a candidate. If the selectivity is better than a
full-table scan, it uses the partial index.

**Buffer cache impact:** A 500 000-row full index on `status` occupies ~8 MB
of the buffer cache. A partial index on ~25 000 rows occupies ~400 KB — 20×
smaller. Because the payment worker queries this index every 2 seconds, it
stays hot in cache. A full index cold-starts every cycle for most of its pages.
This is why partial indexes improve not just latency but overall database
throughput.

---

## 7. Cleanup / Reset Note

`idx_orders_active_status_created` **persists.** This is the correct
production index for the payment worker's hot path. It will be compared to
a BRIN index in Lab 05.12 and analysed in the monitoring lab.

---

## Further Reading

- [PostgreSQL docs — Partial Indexes](https://www.postgresql.org/docs/current/indexes-partial.html)
