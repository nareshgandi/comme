# Lab 05.07 — Hash Index: Pure Equality Lookups

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.06 complete; workers running.
**Estimated time:** 30 min

---

## 1. Business Problem

The fraud detection service performs exact-match lookups of payment sessions
by a 64-character SHA-256 session token stored in `payments.gateway_reference`.
The lookups are always pure equality (`WHERE gateway_reference = $1`); there
are no range queries, no `LIKE` prefix searches, and no `ORDER BY` on this
column. The question from the infrastructure team: the existing B-tree covering
index on `gateway_reference` is correct, but is a Hash index faster for pure
equality — and if so, under what conditions? This lab gives an honest answer.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Confirm the existing index
\d payments
-- Shows: idx_payments_gateway_ref_covering btree (gateway_reference) INCLUDE (order_id, amount)

-- Baseline: pure equality lookup with the existing B-tree covering index
SELECT gateway_reference FROM payments WHERE status = 'SUCCESS' LIMIT 1;
-- Note the token returned

EXPLAIN (ANALYZE, BUFFERS)
SELECT payment_id, order_id, amount
FROM   payments
WHERE  gateway_reference = '<token_from_above>';
```

**Representative output (existing B-tree covering index):**

```
Index Only Scan using idx_payments_gateway_ref_covering on payments
                    (cost=0.42..4.44 rows=1 width=24)
                    (actual time=0.021..0.023 rows=1 loops=1)
  Index Cond: (gateway_reference = '...')
  Heap Fetches: 0
  Buffers: shared hit=3
Execution Time: 0.035 ms
```

---

## 3. Measure (Baseline)

**Baseline (B-tree covering index):** ~0.035 ms  |  3 buffer reads  |  Heap
Fetches: 0

---

## 4. Optimize

Create a Hash index on the same column and compare:

```sql
CREATE INDEX idx_payments_gateway_hash
    ON payments USING hash (gateway_reference);
```

```sql
-- Force the planner to use the hash index by temporarily dropping the btree one
-- (In production you would not do this — we are comparing, not switching.)
BEGIN;
  DROP INDEX idx_payments_gateway_ref_covering;

  EXPLAIN (ANALYZE, BUFFERS)
  SELECT payment_id, order_id, amount
  FROM   payments
  WHERE  gateway_reference = '<same_token>';

ROLLBACK;  -- restore the covering index
```

**Representative output (Hash index, no covering):**

```
Bitmap Heap Scan on payments  (cost=4.02..8.04 rows=1 width=24)
                               (actual time=0.022..0.025 rows=1 loops=1)
  Recheck Cond: (gateway_reference = '...')
  Heap Fetches: 1
  Buffers: shared hit=5
Execution Time: 0.038 ms
```

---

## 5. Measure Again

**Hash index result:** ~0.038 ms  |  5 buffer reads  |  1 heap fetch

**Delta vs. B-tree covering index:** Hash index is marginally **slower** in
this case, because the hash index has no `INCLUDE` capability — it always
requires a heap fetch for the additional columns. The B-tree covering index
wins on this specific query.

---

## 6. Explain

A Hash index computes a hash of each indexed value and stores hash buckets
with lists of matching heap tuple IDs. For an equality lookup, PostgreSQL
computes the hash of the search value, finds the bucket, and retrieves the
matching CTIDs — O(1) expected time vs. O(log N) for B-tree.

**When Hash indexes win over B-tree:**

| Scenario | B-tree | Hash |
|----------|--------|------|
| Pure equality, no extra columns needed | O(log N) | O(1) |
| Equality + projected columns (INCLUDE) | O(log N) + 0 heap fetch | O(1) + 1 heap fetch |
| Range queries (`>`, `<`, `BETWEEN`) | ✓ | ✗ |
| `ORDER BY` using index | ✓ | ✗ |
| `LIKE 'prefix%'` | ✓ | ✗ |
| Very long key values (>~2700 bytes) | ✓ | O(1) |

In practice, a Hash index is smaller than an equivalent B-tree index (no
internal tree pages, just buckets), and for **very large tables with pure
equality-only access** the O(1) vs O(log N) difference becomes measurable.
For a 600 000-row `payments` table, O(log₂ 600 000) ≈ 20 comparisons, which
is negligible — the B-tree covering index wins on the full query.

**Historical note:** Prior to PostgreSQL 10, Hash indexes were **not
WAL-logged** — they could not be replicated and were lost after a crash
without a full rebuild. Since PostgreSQL 10 they are fully crash-safe and
WAL-logged. However, the recommendation for most workloads remains: use B-tree
unless you have evidence that Hash provides a measurable benefit on your
specific query and data.

---

## 7. Cleanup / Reset Note

```sql
-- The Hash index was for demonstration only. Drop it.
DROP INDEX idx_payments_gateway_hash;
-- The B-tree covering index (idx_payments_gateway_ref_covering) remains.
```

---

## Further Reading

- [PostgreSQL docs — Hash Indexes](https://www.postgresql.org/docs/current/indexes-types.html#INDEXES-TYPES-HASH)
