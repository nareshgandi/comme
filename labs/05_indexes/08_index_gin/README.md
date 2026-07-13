# Lab 05.08 — GIN Index: JSONB and Multi-Value Attributes

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.07 complete; workers running.
**Estimated time:** 1 hr

---

## 1. Business Problem

The product catalog team wants to filter products by attributes stored in
`products.metadata` — for example, "find all Electronics products where
`color = 'Black'`" or "find all Clothing products with `size = 'M'`". The
`metadata` column is JSONB, and the factory populates it with category-specific
keys (`color`, `size`, `brand`, `material`, `diet`, etc.). With 1 000 products
and no index on `metadata`, every attribute query requires a sequential scan.
More urgently, the schema comment already anticipated this: "GIN index on
metadata — added in Lab 05." This is that lab.

A second real scenario: `employees.metadata` stores unstructured key-value
pairs (skill tags, certifications). HR needs to find all employees with a
specific certification. Same problem; same solution.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Inspect the metadata structure for a product category
SELECT metadata
FROM   products
WHERE  category = 'Electronics'
LIMIT  3;

-- The JSONB query: find all Electronics products with color = 'Black'
EXPLAIN (ANALYZE, BUFFERS)
SELECT product_id, sku, name, metadata->>'color' AS color
FROM   products
WHERE  category = 'Electronics'
  AND  metadata @> '{"color": "Black"}';
```

**Representative output (~1 000 products, no GIN index):**

```
Seq Scan on products  (cost=0.00..24.50 rows=1 width=88)
                      (actual time=0.021..1.847 rows=45 loops=1)
  Filter: ((category = 'Electronics') AND (metadata @> '{"color": "Black"}'::jsonb))
  Rows Removed by Filter: 955
  Buffers: shared hit=12
Planning Time: 0.089 ms
Execution Time: 1.912 ms
```

With 1 000 products the sequential scan is fast — but as the catalog grows and
metadata queries get more complex (nested keys, array containment), the lack of
a GIN index becomes a serious bottleneck. More importantly, *JSONB containment
operators cannot use a B-tree index at all* — GIN is the only option.

Test an employee metadata query too:

```sql
-- Find employees with a specific skill tag (if populated by workers)
EXPLAIN (ANALYZE, BUFFERS)
SELECT employee_id, first_name, last_name, metadata
FROM   employees
WHERE  metadata @> '{"certifications": ["OSHA-30"]}';
```

---

## 3. Measure (Baseline)

**Baseline (products):** ~1.9 ms on 1 000 rows  |  Seq Scan, 12 pages
**Projected at 1 M products:** ~1 900 ms — linear growth, no index option
except GIN

Note: on 1 000 rows the Seq Scan is fast enough that the planner may choose it
over the GIN index even after adding it (small tables are faster to scan than
to do index lookups). The lab adds the index correctly for future scale and to
demonstrate the operator support.

---

## 4. Optimize

### Two GIN operator classes — understand the trade-off first

| Operator class | Supports | Index size | Use when |
|----------------|----------|------------|----------|
| `jsonb_ops` (default) | `@>`, `?`, `?|`, `?&`, `@?`, `@@` | Larger | Need key existence checks (`?`) |
| `jsonb_path_ops` | `@>`, `@?`, `@@` only | Smaller | Containment and path queries only |

```sql
-- Add GIN index on products.metadata (default jsonb_ops)
CREATE INDEX idx_products_metadata_gin
    ON products USING gin (metadata);

-- For employees, use jsonb_path_ops (containment only, smaller index)
CREATE INDEX idx_employees_metadata_gin
    ON employees USING gin (metadata jsonb_path_ops);
```

Both indexes **persist as permanent baseline changes** — anticipated in the
schema comments from Milestone 1.

```sql
-- Re-run with GIN index
EXPLAIN (ANALYZE, BUFFERS)
SELECT product_id, sku, name, metadata->>'color' AS color
FROM   products
WHERE  category = 'Electronics'
  AND  metadata @> '{"color": "Black"}';
```

**Representative output (GIN index, small table — planner may still seq scan):**

```
-- On 1 000 rows, the planner may choose Seq Scan due to small table size.
-- Force the demonstration:
SET enable_seqscan = off;

Bitmap Heap Scan on products  (cost=8.00..24.50 rows=1 width=88)
                               (actual time=0.312..0.891 rows=45 loops=1)
  Recheck Cond: (metadata @> '{"color": "Black"}'::jsonb)
  Filter: (category = 'Electronics')
  Buffers: shared hit=8
Execution Time: 0.934 ms

RESET enable_seqscan;
```

**Additional GIN queries that now use the index:**

```sql
-- Key existence check (requires jsonb_ops, not jsonb_path_ops)
SELECT product_id, sku FROM products WHERE metadata ? 'brand';

-- Any of these keys exist
SELECT product_id, sku FROM products WHERE metadata ?| ARRAY['color', 'size'];

-- Nested containment
SELECT product_id, sku FROM products WHERE metadata @> '{"size": ["M", "L"]}';
```

---

## 5. Measure Again

```sql
-- On a small table, force the planner to show the GIN path:
SET enable_seqscan = off;
EXPLAIN (ANALYZE, BUFFERS)
SELECT product_id, sku, name
FROM   products
WHERE  metadata @> '{"color": "Black"}';
RESET enable_seqscan;
```

**After:** Bitmap Heap Scan, ~0.9 ms  |  8 buffer reads
**Key insight:** On 1 000 rows, both paths are fast. The critical difference is
*operator support*: without GIN, the containment operator `@>` has no index
path at all — it always seq scans. At 100 000 products (e.g. after real catalog
growth), `SET enable_seqscan = off` is unnecessary — the GIN index wins clearly.

---

## 6. Explain

A GIN (Generalized Inverted Index) is an inverted index: instead of mapping row
IDs to key values, it maps key values (or JSONB paths/elements) to the list of
row IDs containing that key.

For JSONB, PostgreSQL decomposes each document into its constituent keys and
values and creates index entries for each one. A `products.metadata` entry of
`{"color": "Black", "brand": "Sony"}` creates index entries for `color:Black`
and `brand:Sony`. A containment query `metadata @> '{"color": "Black"}'` asks:
"which rows have an entry for `color:Black`?" — the GIN index answers in O(1)
posting-list lookup per key.

A B-tree cannot answer containment queries: it stores entire column values as
sorted keys, so it can only compare `metadata = '...'` (exact match) or scan
for range — neither useful for containment or key existence.

**GIN and write amplification:** GIN indexes have higher write overhead than
B-tree. Each INSERT into `products` decomposes the metadata document and inserts
one GIN entry per key-value pair. PostgreSQL mitigates this with a "pending
list" (a small B-tree of new entries) that is merged into the main GIN structure
by autovacuum. During heavy writes, lookups against the pending list add a small
linear scan; after a merge, lookups are O(1) again.

---

## 7. Cleanup / Reset Note

`idx_products_metadata_gin` and `idx_employees_metadata_gin` **persist** as
permanent baseline changes. Both are anticipated in the M1 schema comments and
will be observed in the monitoring lab (bloat, autovacuum on GIN pending lists).

---

## Further Reading

- [PostgreSQL docs — GIN Indexes](https://www.postgresql.org/docs/current/gin.html)
- [PostgreSQL docs — JSONB Operators and Functions](https://www.postgresql.org/docs/current/functions-json.html)
