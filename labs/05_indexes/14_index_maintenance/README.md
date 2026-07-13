# Lab 05.14 — Index Maintenance: Bloat, REINDEX, and Visibility Map

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.13 complete; workers running.
**Estimated time:** 1 hr

---

## 1. Business Problem

After several weeks of live workers running, the `payments` table has had
hundreds of thousands of UPDATEs (payment status transitions: `PENDING` →
`SUCCESS` / `FAILED`). Each UPDATE creates a dead heap tuple and a dead
index entry in every index on `payments`. Autovacuum cleans the heap, but
index bloat accumulates separately. The infrastructure team wants to know:
how bloated are the indexes, how much space is wasted, and when should a
DBA run `REINDEX` — and can they do it without taking a write outage?

A secondary concern: the covering index on `gateway_reference` (Lab 05.06)
relies on Index Only Scans, which in turn rely on the visibility map. If the
visibility map has stale entries because autovacuum hasn't run recently, the
Index Only Scan falls back to heap fetches. This lab demonstrates that
connection and shows how to trigger `VACUUM` to fix it.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Check autovacuum history and dead tuple counts on high-write tables
SELECT
    relname,
    n_live_tup,
    n_dead_tup,
    ROUND(n_dead_tup * 100.0 / NULLIF(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
    last_autovacuum,
    last_autoanalyze
FROM pg_stat_user_tables
WHERE schemaname = 'orderflow'
  AND relname IN ('orders', 'payments', 'employees')
ORDER BY n_dead_tup DESC;
```

Inspect index bloat using `pgstatindex` (built-in function for B-tree indexes):

```sql
-- Measure B-tree index bloat on the payments table indexes
SELECT
    pi.indexname,
    pg_size_pretty(pg_relation_size(pi.indexname::regclass)) AS index_size,
    s.leaf_fragmentation,
    s.avg_leaf_density
FROM pg_indexes pi
CROSS JOIN LATERAL pgstatindex(pi.indexname) s
WHERE pi.tablename = 'payments'
  AND pi.schemaname = 'orderflow'
ORDER BY pi.indexname;
```

Check the visibility map state (for Index Only Scan health):

```sql
-- How many heap pages are marked all-visible (good for Index Only Scans)?
SELECT
    relname,
    heap_blks_total,
    heap_blks_hit,
    idx_blks_read,
    idx_blks_hit
FROM pg_statio_user_tables
WHERE schemaname = 'orderflow'
  AND relname = 'payments';

-- Direct visibility map query
SELECT
    relname,
    all_visible,        -- pages marked all-visible in visibility map
    all_frozen,         -- pages marked all-frozen
    reltuples::bigint   AS estimated_rows
FROM pg_class c
JOIN pg_visibility_summary(c.oid) ON TRUE
WHERE relname = 'payments'
  AND relkind = 'r';
```

Force a visibility map check using the covering index:

```sql
SELECT gateway_reference FROM payments WHERE status = 'SUCCESS' LIMIT 1;
-- Note the reference

EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = '<reference_from_above>';
-- Look at "Heap Fetches" — if > 0, the visibility map has stale entries
```

---

## 3. Measure (Baseline)

**Baseline:**
1. `n_dead_tup` on `payments`: record the current count — should be non-zero
   after any period of worker activity.
2. `leaf_fragmentation` on payment indexes: note the percentage — values above
   ~30% indicate measurable bloat.
3. `Heap Fetches` in the Index Only Scan: record whether it's 0 or > 0.

---

## 4. Optimize

### Step 1 — VACUUM to update the visibility map

```sql
-- Trigger VACUUM on payments to process dead tuples and update visibility map
VACUUM (VERBOSE, ANALYZE) payments;
```

After VACUUM:
- Dead tuples are cleaned from the heap
- The visibility map is updated — pages with only live tuples are marked
  all-visible
- `ANALYZE` updates statistics used by the planner

Re-check the Index Only Scan:

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = '<same_reference>';
-- Heap Fetches should now be 0 (or very close)
```

### Step 2 — REINDEX to rebuild a bloated index

`REINDEX` rebuilds the index from scratch, eliminating all dead entries and
fragmentation. There are two forms:

```sql
-- REINDEX (blocking — acquires ShareLock, blocks writes)
-- Use when the table is offline or during a maintenance window
REINDEX INDEX payments_one_success_per_order;

-- REINDEX CONCURRENTLY (PG12+, non-blocking — same idea as CREATE INDEX CONCURRENTLY)
-- Use during business hours on a live table
REINDEX INDEX CONCURRENTLY idx_payments_gateway_ref_covering;
```

After REINDEX, check the bloat again:

```sql
SELECT
    pi.indexname,
    pg_size_pretty(pg_relation_size(pi.indexname::regclass)) AS index_size,
    s.leaf_fragmentation
FROM pg_indexes pi
CROSS JOIN LATERAL pgstatindex(pi.indexname) s
WHERE pi.tablename = 'payments'
  AND pi.schemaname = 'orderflow'
ORDER BY pi.indexname;
```

### Step 3 — Verify autovacuum is keeping up

```sql
-- Check autovacuum_vacuum_scale_factor threshold
-- (default: 0.2 = vacuum when 20% of rows are dead)
SHOW autovacuum_vacuum_scale_factor;

-- For high-write tables, 20% is too high — 600k payments × 20% = 120k dead
-- tuples before autovacuum fires. Consider a per-table override:
ALTER TABLE payments SET (
    autovacuum_vacuum_scale_factor = 0.05,   -- vacuum at 5% dead
    autovacuum_vacuum_cost_delay   = 2       -- less aggressive throttling
);
```

---

## 5. Measure Again

```sql
-- Re-run the dead tuple / bloat check
SELECT relname, n_live_tup, n_dead_tup
FROM   pg_stat_user_tables
WHERE  relname = 'payments' AND schemaname = 'orderflow';

-- Re-run the Index Only Scan check
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount FROM payments
WHERE  gateway_reference = '<same_reference>';
```

**After VACUUM:** `n_dead_tup` drops toward 0; `Heap Fetches` in Index Only Scan drops to 0.
**After REINDEX:** `leaf_fragmentation` drops to near 0%; index may be slightly smaller.
**Delta:** Index Only Scan now performs zero heap fetches — all pages in the
visibility map are marked all-visible after VACUUM.

---

## 6. Explain

**Index bloat:** When a row is updated, PostgreSQL does not modify the existing
heap tuple — it writes a new version and marks the old one dead (MVCC). The old
tuple's index entries remain in every index until VACUUM removes them. Over
time, indexes accumulate dead entries (page space occupied by pointers to dead
heap rows) — this is **index bloat**. A bloated index is larger than necessary
and has more empty or sparsely-filled leaf pages.

**VACUUM and the visibility map:** VACUUM's primary job on the heap is to mark
dead tuples as reusable. As a side effect, it marks heap pages in the
**visibility map** as all-visible when every tuple on the page is live and
visible to all transactions. Index Only Scans consult the visibility map: if a
page is marked all-visible, no heap fetch is needed for visibility checking.
If the page is not marked, the heap must be read to verify tuple visibility —
hence `Heap Fetches > 0` after write-heavy periods when VACUUM hasn't run.

**REINDEX:** Unlike VACUUM, REINDEX rebuilds the index from scratch by scanning
the heap and creating a new, compact B-tree. It eliminates dead entries *and*
rebalances the tree, which VACUUM cannot do. `REINDEX CONCURRENTLY` (PG12+)
works like `CREATE INDEX CONCURRENTLY` — two passes, no write blocking.

**autovacuum tuning:** The default `autovacuum_vacuum_scale_factor = 0.2` means
"VACUUM when 20% of rows are dead." For a 600 000-row payments table, this
means waiting until 120 000 dead tuples accumulate — enough to cause measurable
Index Only Scan regressions. Lowering the threshold to 5% (`0.05`) for high-
write tables ensures more frequent, smaller autovacuum runs.

This connection between VACUUM, the visibility map, and Index Only Scan
performance is why Lab 05.06 (Covering Index) specifically flagged it with a
"forward pointer to the VACUUM lab."

---

## 7. Cleanup / Reset Note

The `autovacuum_vacuum_scale_factor` override on `payments` **persists** — it
is the correct production tuning for a high-write table and will be observed in
the monitoring lab (Lab 11).

All other changes in this lab (`VACUUM`, `REINDEX`) are one-time maintenance
operations, not persistent schema changes.

To review all indexes accumulated across Labs 04.01–04.15:

```sql
SELECT indexname, tablename,
       pg_size_pretty(pg_relation_size(indexname::regclass)) AS size,
       pg_get_indexdef(indexrelid) AS definition
FROM pg_indexes pi
JOIN pg_class c ON c.relname = pi.indexname
JOIN pg_index i ON i.indexrelid = c.oid
WHERE pi.schemaname = 'orderflow'
ORDER BY tablename, indexname;
```

**Persisting indexes (documented baseline changes):**

| Index | Table | Lab | Purpose |
|-------|-------|-----|---------|
| `payments_one_success_per_order` | payments | 04.01 | INV-05 enforcement |
| `idx_orders_customer_id` | orders | 04.02 | FK lookup index |
| `idx_order_items_order_product` | order_items | 04.04 | Composite access pattern |
| `idx_orders_active_status_created` | orders | 04.05 | Payment worker hot path |
| `idx_customers_email_lower` | customers | 04.06 | Case-insensitive email search |
| `idx_payments_gateway_ref_covering` | payments | 04.07 | Reconciliation Index Only Scan |
| `idx_payments_order_id` | payments | 04.07 | FK lookup index |
| `idx_products_metadata_gin` | products | 04.09 | JSONB attribute search |
| `idx_employees_metadata_gin` | employees | 04.09 | JSONB skill search |
| `idx_customers_name_trgm` | customers | 04.10 | Fuzzy name search |
| `idx_employees_name_trgm` | employees | 04.10 | Fuzzy name search |
| `idx_orders_created_at_brin` | orders | 04.13 | Date range scan |
| `idx_orders_warehouse_id` | orders | 04.14 | Warehouse fulfillment queries |

---

## Further Reading

- [PostgreSQL docs — VACUUM](https://www.postgresql.org/docs/current/sql-vacuum.html)
- [PostgreSQL docs — REINDEX](https://www.postgresql.org/docs/current/sql-reindex.html)
- [PostgreSQL docs — Visibility Map](https://www.postgresql.org/docs/current/storage-vm.html)
- [PostgreSQL docs — pgstatindex](https://www.postgresql.org/docs/current/pgstattuple.html)
