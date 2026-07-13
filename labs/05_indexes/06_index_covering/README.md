# Lab 05.06 — Covering Index (INCLUDE): Eliminating Heap Fetches

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.05 complete; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

The payment reconciliation service sends thousands of requests per minute that
look up a payment by its `gateway_reference` token (the external processor's
transaction ID) and retrieve the `order_id` and `amount` to cross-check against
the bank's settlement file. Adding an index on `gateway_reference` makes the
lookup fast, but the planner performs an **Index Scan** — it finds the row
using the index, then fetches the full heap row to retrieve `order_id` and
`amount`. That second heap fetch is unnecessary: `order_id` and `amount` could
be stored in the index leaf page alongside `gateway_reference`, eliminating the
round-trip to the heap entirely. This lab demonstrates the difference between
an Index Scan and an Index Only Scan, and the `INCLUDE` clause that enables it.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Pick a real gateway_reference
SELECT gateway_reference FROM payments WHERE status = 'SUCCESS' LIMIT 1;
-- Note the value returned (e.g. 'CC-A3F2819D4B7E1C0F')

-- Baseline: no index on gateway_reference yet
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = 'CC-A3F2819D4B7E1C0F';
```

**Representative output (~600 000 payments, no index):**

```
Seq Scan on payments  (cost=0.00..12380.50 rows=1 width=16)
                      (actual time=0.088..312.841 rows=1 loops=1)
  Filter: ((gateway_reference)::text = 'CC-A3F2819D4B7E1C0F')
  Rows Removed by Filter: 599999
  Buffers: shared hit=245 read=3063
Planning Time: 0.089 ms
Execution Time: 312.941 ms
```

---

## 3. Measure (Baseline)

**Baseline:** ~313 ms  |  Seq Scan reading ~3 308 pages  |  599 999 rows
examined to return 1 result

---

## 4. Optimize

### Step 1 — Plain index (Index Scan — still hits the heap)

```sql
CREATE INDEX idx_payments_gateway_ref
    ON payments (gateway_reference);

EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = 'CC-A3F2819D4B7E1C0F';
```

**Representative output (plain index — Index Scan):**

```
Index Scan using idx_payments_gateway_ref on payments
                    (cost=0.42..8.44 rows=1 width=16)
                    (actual time=0.032..0.036 rows=1 loops=1)
  Index Cond: ((gateway_reference)::text = 'CC-A3F2819D4B7E1C0F')
  Buffers: shared hit=5
Execution Time: 0.048 ms
```

Faster — but note `Buffers: shared hit=5`. The planner still makes a heap
fetch: the index points to the tuple; PostgreSQL reads one more page to get
`order_id` and `amount`.

### Step 2 — Covering index (Index Only Scan — heap skipped)

```sql
DROP INDEX idx_payments_gateway_ref;

CREATE INDEX idx_payments_gateway_ref_covering
    ON payments (gateway_reference)
    INCLUDE (order_id, amount);
```

The `INCLUDE` columns are stored in the leaf pages of the index but are NOT
part of the sort key — they cannot be used in `WHERE` clauses or `ORDER BY`.
They exist solely to make the index self-contained for queries that need those
columns.

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = 'CC-A3F2819D4B7E1C0F';
```

**Representative output (covering index — Index Only Scan):**

```
Index Only Scan using idx_payments_gateway_ref_covering on payments
                    (cost=0.42..4.44 rows=1 width=16)
                    (actual time=0.021..0.023 rows=1 loops=1)
  Index Cond: ((gateway_reference)::text = 'CC-A3F2819D4B7E1C0F')
  Heap Fetches: 0
  Buffers: shared hit=3
Planning Time: 0.189 ms
Execution Time: 0.035 ms
```

`Heap Fetches: 0` confirms no heap access. The index contained everything the
query needed.

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, amount
FROM   payments
WHERE  gateway_reference = 'CC-A3F2819D4B7E1C0F';
```

**After (covering index):** ~0.035 ms  |  3 buffer reads  |  Heap Fetches: 0
**Delta:** vs. Seq Scan: ~8 940× faster; buffer reads 3 308 → 3

---

## 6. Explain

**Index Scan vs. Index Only Scan:**
An Index Scan uses the index to find matching row identifiers (CTIDs) and then
visits the heap page for each CTID to retrieve the requested columns. Even
fetching a single row requires at least one heap page access.

An **Index Only Scan** happens when the index contains ALL columns that the
query projects — there is nothing left to fetch from the heap. PostgreSQL reads
the leaf page of the index and returns the data directly. `Heap Fetches: 0`
confirms the heap was never accessed.

**The visibility map dependency (forward pointer to Lab 05.14 — VACUUM):**
Index Only Scans are not always 100 % heap-free. PostgreSQL must confirm that
each index entry is visible to the current transaction. To avoid reading the
heap page for this check, it consults the **visibility map** — a bitmap that
records which heap pages contain only live, visible tuples. If a heap page has
not been vacuumed recently and is not marked in the visibility map, PostgreSQL
must visit it to verify visibility, even for a covered query.

This means `Heap Fetches` can be non-zero immediately after a table has heavy
write activity. After `VACUUM` runs and marks pages as all-visible, subsequent
Index Only Scans see `Heap Fetches: 0`. Lab 05.14 (VACUUM and Index Maintenance)
demonstrates this relationship directly.

**`INCLUDE` vs. adding to the key:** Adding `order_id` and `amount` to the key
would also allow an Index Only Scan, but it would change the sort order of the
index and allow using those columns in range queries or `ORDER BY` — which is
not needed here and would make the index slightly larger. `INCLUDE` is the
correct choice when you want the data available in the leaf page without making
it a sort key.

---

## 7. Cleanup / Reset Note

`idx_payments_gateway_ref_covering` **persists.** The reconciliation service
query is a real hot path, and Index Only Scans require zero heap fetches.
Referenced in Lab 05.14 (VACUUM) to demonstrate the visibility map connection.

Also add the FK lookup index on `payments.order_id`:

```sql
CREATE INDEX idx_payments_order_id ON payments (order_id);
```

This index **persists** — FK columns on high-write tables need indexes for
join performance and cascading delete performance.

---

## Further Reading

- [PostgreSQL docs — Index-Only Scans and Covering Indexes](https://www.postgresql.org/docs/current/indexes-index-only-scans.html)
- [PostgreSQL docs — Visibility Map](https://www.postgresql.org/docs/current/storage-vm.html)
