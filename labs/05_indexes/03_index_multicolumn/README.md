# Lab 05.03 — Multicolumn Index: Column Order and Query Shape

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.02 complete; workers running.
**Estimated time:** 1 hr

---

## 1. Business Problem

The ops team runs a nightly SLA report: "Give me all SHIPPED and DELIVERED
orders from the last 30 days, with their customer and warehouse info." The
query filters on two columns simultaneously — `status` and `created_at`. Single-
column indexes on either column alone do not eliminate enough rows to avoid a
large heap scan: an index on `status` returns 40 % of the table for DELIVERED
alone; an index on `created_at` returns 30 days out of potentially 365 days.
Only a composite index on both columns together delivers a tight result set in
one pass. The wrong column order in the composite index produces no benefit at
all — this lab demonstrates why.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- The SLA report query (no composite index yet)
EXPLAIN (ANALYZE, BUFFERS)
SELECT o.order_id, o.status, o.total_amount, o.created_at,
       c.email    AS customer_email,
       w.code     AS warehouse_code
FROM   orders o
JOIN   customers c USING (customer_id)
JOIN   warehouses w USING (warehouse_id)
WHERE  o.status IN ('SHIPPED', 'DELIVERED')
  AND  o.created_at >= NOW() - INTERVAL '30 days'
ORDER BY o.created_at DESC;
```

**Representative output (~500 000 orders, no composite index):**

```
Hash Join  (cost=3245.61..17123.40 rows=6210 width=88)
           (actual time=31.234..589.421 rows=6187 loops=1)
  Buffers: shared hit=4512 read=892
  -> Hash Join  (cost=...
       -> Seq Scan on orders  (cost=0.00..15890.25 rows=6210 width=72)
                              (actual time=0.054..421.107 rows=6187 loops=1)
            Filter: ((status = ANY ('{SHIPPED,DELIVERED}')) AND
                     (created_at >= (now() - '30 days'::interval)))
            Rows Removed by Filter: 493813
            Buffers: shared hit=312 read=4141
Planning Time: 1.201 ms
Execution Time: 590.843 ms
```

---

## 3. Measure (Baseline)

**Baseline:** ~591 ms  |  Seq Scan on orders reading ~4 453 pages  |
493 813 rows scanned to find 6 187 results

---

## 4. Optimize

### Part A — Wrong column order (demonstrates why order matters)

```sql
-- Index with created_at first — efficient for date range scans, but
-- status comes after, so the index cannot narrow the scan significantly
-- when status has low cardinality and is the primary filter.
CREATE INDEX idx_orders_created_at_status
    ON orders (created_at, status);

EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, status, total_amount, created_at
FROM   orders
WHERE  status IN ('SHIPPED', 'DELIVERED')
  AND  created_at >= NOW() - INTERVAL '30 days';
```

**Representative output (created_at first):**

```
Bitmap Heap Scan on orders  (cost=245.12..8921.34 rows=6210 width=60)
                             (actual time=3.412..98.234 rows=6187 loops=1)
  Recheck Cond: (created_at >= ...)
  Filter: (status = ANY ('{SHIPPED,DELIVERED}'))
  Rows Removed by Filter: 3201
  Buffers: shared hit=892 read=412
Execution Time: 99.442 ms
```

Better — but the planner uses a Bitmap Heap Scan, scans 30 days of data, then
*re-filters* on status. The index helped with the date range, but status acts
as a post-filter, not a scan condition.

```sql
DROP INDEX idx_orders_created_at_status;
```

### Part B — Correct column order (high-selectivity column first)

```sql
-- status first: the planner can use equality on status to jump to the
-- exact position in the index, then scan only the matching created_at range.
CREATE INDEX idx_orders_status_created_at
    ON orders (status, created_at DESC);
```

Also add the index on `order_items` that the schema comment anticipates:

```sql
-- The composite index on order_items for the primary access pattern
CREATE INDEX idx_order_items_order_product
    ON order_items (order_id, product_id);
```

This second index **persists as a permanent baseline** — it is referenced in
the covering index lab (04.07) and the monitoring lab (04.11).

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, status, total_amount, created_at
FROM   orders
WHERE  status IN ('SHIPPED', 'DELIVERED')
  AND  created_at >= NOW() - INTERVAL '30 days'
ORDER BY created_at DESC;
```

**Representative output (status first):**

```
Index Scan using idx_orders_status_created_at on orders
                    (cost=0.56..312.45 rows=6210 width=60)
                    (actual time=0.042..8.234 rows=6187 loops=1)
  Index Cond: ((status = ANY ('{SHIPPED,DELIVERED}')) AND
               (created_at >= (now() - '30 days'::interval)))
  Buffers: shared hit=421
Planning Time: 0.312 ms
Execution Time: 8.451 ms
```

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT o.order_id, o.status, o.total_amount, o.created_at,
       c.email, w.code
FROM   orders o
JOIN   customers c USING (customer_id)
JOIN   warehouses w USING (warehouse_id)
WHERE  o.status IN ('SHIPPED', 'DELIVERED')
  AND  o.created_at >= NOW() - INTERVAL '30 days'
ORDER BY o.created_at DESC;
```

**After:** ~8 ms  |  Index Scan  |  ~421 buffer reads
**Delta:** ~70× faster; buffer reads dropped from ~5 404 to ~421 (92% reduction)

---

## 6. Explain

A multicolumn B-tree index is sorted by the **first** column, then by the
second column within groups of equal first-column values, and so on. The index
can only be used to narrow the scan starting from the **leftmost** column.

With `(created_at, status)`: the index is sorted by date. A query filtering on
`status` with no date condition cannot start at a useful position in the index —
it must scan the entire date range and post-filter on status. The index helps
when the query filters on `created_at` alone or `(created_at, status)`.

With `(status, created_at DESC)`: the index is sorted by status first. The
planner can seek directly to the portion of the index where `status = 'SHIPPED'`
and then scan only the `created_at >= 30 days ago` sub-range. Both columns are
used as index conditions, not filters — no heap rows are fetched unnecessarily.

**The leftmost prefix rule:** A multicolumn index `(a, b, c)` supports queries
that filter on `(a)`, `(a, b)`, or `(a, b, c)` — but NOT `(b)`, `(c)`, or
`(b, c)` alone. Always put the column with the query's equality condition first;
put the range column second.

---

## 7. Cleanup / Reset Note

`idx_orders_status_created_at` — **drop after this lab.** It served to
demonstrate column ordering but is not the right long-term index for
`orders.status` in isolation. A better approach (partial index on active
statuses) is covered in Lab 05.04.

`idx_order_items_order_product` — **persists.** Referenced in Labs 04.07 and
04.11.

```sql
DROP INDEX idx_orders_status_created_at;
-- idx_orders_customer_id (from Lab 05.01) remains
-- idx_order_items_order_product remains
-- payments_one_success_per_order (from Lab 04.01) remains
```

---

## Further Reading

- [PostgreSQL docs — Multicolumn Indexes](https://www.postgresql.org/docs/current/indexes-multicolumn.html)
