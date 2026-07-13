# Lab 05.05 — Expression Index: Index a Transformation, Not a Column

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.04 complete; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

The customer support portal lets agents search for customers by email address.
The portal normalises search input to lowercase before querying, but the
`customers.email` column was populated by factories using mixed-case Faker
data. An agent searching for `ALICE.JOHNSON@GMAIL.COM` finds nothing, even
though the row exists as `alice.johnson@gmail.com`. Using `lower(email) =
lower($1)` in the query correctly handles the mismatch — but without an index
on the expression `lower(email)`, the query falls back to a sequential scan
on 100 000 customers. The existing `customers_email_key` unique index on
`email` is not used when the query filters on `lower(email)`.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Confirm the UNIQUE index on email exists (from schema)
\d customers
-- Shows: customers_email_key UNIQUE, btree (email)

-- Now observe a case-insensitive lookup
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name, email, loyalty_tier
FROM   customers
WHERE  lower(email) = lower('Alice.Johnson@gmail.com');
```

**Representative output (~100 000 customers, no expression index):**

```
Seq Scan on customers  (cost=0.00..3124.00 rows=500 width=88)
                       (actual time=0.048..147.231 rows=1 loops=1)
  Filter: (lower((email)::text) = 'alice.johnson@gmail.com')
  Rows Removed by Filter: 99999
  Buffers: shared hit=124 read=1751
Planning Time: 0.089 ms
Execution Time: 147.312 ms
```

The existing `customers_email_key` index on `email` is **not used** because
the query filters on `lower(email)`, not `email`. To the planner, `email` and
`lower(email)` are different indexed expressions.

---

## 3. Measure (Baseline)

**Baseline:** ~147 ms  |  Seq Scan on customers reading ~1 875 pages  |
99 999 rows examined to return 1 result

---

## 4. Optimize

```sql
-- Index the transformation, not the raw column value.
-- Any query using WHERE lower(email) = $1 will now use this index.
CREATE INDEX idx_customers_email_lower
    ON customers (lower(email));
```

The index stores the result of `lower(email)` for every row — not the original
value. PostgreSQL evaluates `lower(email)` once at insert/update time and
stores the normalised value in the index leaf pages.

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name, email, loyalty_tier
FROM   customers
WHERE  lower(email) = lower('Alice.Johnson@gmail.com');
```

**Representative output after expression index:**

```
Index Scan using idx_customers_email_lower on customers
                    (cost=0.42..8.44 rows=1 width=88)
                    (actual time=0.032..0.036 rows=1 loops=1)
  Index Cond: (lower((email)::text) = 'alice.johnson@gmail.com')
  Buffers: shared hit=4
Planning Time: 0.201 ms
Execution Time: 0.058 ms
```

**After:** ~0.06 ms  |  4 buffer reads
**Delta:** ~2 455× faster; buffer reads dropped from ~1 875 to 4 (99.8% reduction)

---

## 6. Explain

A regular B-tree index on `email` stores the raw column values in sorted
order. The planner matches query predicates to index keys by comparing the
predicate expression against the index definition. `lower(email) = $1` does
not match `email` — the expressions are structurally different — so the plain
index is bypassed.

An **expression index** (also called a functional index) applies a function or
expression to each column value at index-build time and stores the result as
the index key. Here, `lower(email)` is evaluated once per row; the normalised
value is stored in the leaf page instead of the original mixed-case value.
When a query uses `WHERE lower(email) = ...`, the planner finds an index whose
definition exactly matches the expression and uses it.

**Write overhead:** Expression indexes have a slightly higher write cost than
column indexes because PostgreSQL must evaluate `lower(email)` on every INSERT
and UPDATE that touches `email`. For `lower()` on a short string, this overhead
is negligible. For expensive expressions (e.g. `to_tsvector()` on a large TEXT
column), the overhead is significant — and in that case a generated column may
be preferable.

**The original `customers_email_key` is still needed:** It enforces exact-case
uniqueness for the `UNIQUE` constraint. The new expression index handles lookup
performance only. They serve different purposes and coexist.

---

## 7. Cleanup / Reset Note

`idx_customers_email_lower` **persists.** The customer support portal
case-insensitive email lookup is a real, ongoing query and this index is the
correct solution. No cleanup needed.

---

## Further Reading

- [PostgreSQL docs — Indexes on Expressions](https://www.postgresql.org/docs/current/indexes-expressional.html)
