# Lab 05.01 — B-tree Index: The Default Index Type

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 04.01 complete; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

The finance team is building a customer account dashboard that shows every
order a customer has ever placed. The backend query is `SELECT * FROM orders
WHERE customer_id = $1`. With 500 000 orders in the database and no index on
`orders.customer_id`, this query performs a full sequential scan — reading
every row in the table to find the 5–20 orders belonging to one customer.
Dashboard page loads are timing out at 3–4 seconds. Foreign key columns in
PostgreSQL do **not** get an index automatically; the developer who wrote the
FK assumed PostgreSQL would handle it.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Pick a customer who has placed orders
SELECT customer_id, COUNT(*) AS order_count
FROM orders
GROUP BY customer_id
ORDER BY order_count DESC
LIMIT 1;
-- Note the customer_id returned (used as $1 below)

-- Observe the query plan — before any index
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, status, total_amount, created_at
FROM orders
WHERE customer_id = <customer_id_from_above>;
```

**Representative output on a default bootstrap dataset (~500 000 orders):**

```
Seq Scan on orders  (cost=0.00..15890.25 rows=5 width=60)
                    (actual time=0.218..412.847 rows=8 loops=1)
  Filter: (customer_id = 4721)
  Rows Removed by Filter: 499992
  Buffers: shared hit=312 read=4141
Planning Time: 0.112 ms
Execution Time: 413.031 ms
```

> Your numbers will differ based on cache state and live worker activity.
> Record YOUR baseline values in section 3 before continuing.

---

## 3. Measure (Baseline)

**Baseline:** ~413 ms  |  Seq Scan reading ~4 453 pages (shared hit + read)
|  499 992 rows scanned to find 8 results

---

## 4. Optimize

A B-tree index on the foreign key column allows PostgreSQL to jump directly to
the matching rows without reading the rest of the table.

```sql
CREATE INDEX idx_orders_customer_id ON orders (customer_id);
```

This index **persists as a permanent baseline change.** Every table whose FK
column is queried for lookups should have an index on that column — the
omission was a schema-level gap, not a deliberate teaching exercise.

---

## 5. Measure Again

```sql
-- Re-run the exact same EXPLAIN from section 2
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, status, total_amount, created_at
FROM orders
WHERE customer_id = <same_customer_id>;
```

**Representative output after index:**

```
Index Scan using idx_orders_customer_id on orders
                    (cost=0.42..22.15 rows=5 width=60)
                    (actual time=0.031..0.049 rows=8 loops=1)
  Index Cond: (customer_id = 4721)
  Buffers: shared hit=7
Planning Time: 0.201 ms
Execution Time: 0.071 ms
```

**After:** ~0.07 ms  |  7 buffer reads
**Delta:** ~5 800× faster; buffer reads dropped from ~4 453 to 7 (99.85% reduction)

---

## 6. Explain

A **B-tree** (balanced tree) index stores index entries in sorted order on
disk. The structure is a tree of pages: internal pages contain key values and
pointers to child pages; leaf pages contain the actual key values and heap
tuple identifiers (CTIDs) pointing to the physical row location.

Before the index, PostgreSQL had no choice but to read every heap page in
`orders` (a sequential scan) and evaluate the filter `customer_id = 4721`
against each row. With 500 000 rows across ~4 453 heap pages, that is ~4 453
8 KB disk reads.

After the index, the executor walks the B-tree from the root to the leaf level:
at each internal node it reads one page and follows the pointer to the child
that covers `customer_id = 4721`. This takes O(log N) page reads — roughly 3–4
pages for 500 000 rows. It then follows the CTIDs in the leaf pages to fetch
the matching heap rows directly. Total: ~7 page reads instead of ~4 453.

**Why PostgreSQL does not automatically index FK columns:** A FK constraint
enforces referential integrity — it checks the *referenced* column's PK index
(which must exist). It does not know whether the *referencing* column will
ever appear in a `WHERE` clause. Adding an index on every FK would create
unnecessary write overhead on tables that are only ever joined, not filtered.
PostgreSQL leaves the decision to the DBA.

---

## 7. Cleanup / Reset Note

`idx_orders_customer_id` **persists** — it is a legitimate production index,
not a demonstration artifact. No cleanup needed.

---

## Further Reading

- [PostgreSQL docs — B-Tree Indexes](https://www.postgresql.org/docs/current/indexes-types.html#INDEXES-TYPES-BTREE)
- [PostgreSQL docs — Index on Expressions](https://www.postgresql.org/docs/current/indexes-expressional.html)
