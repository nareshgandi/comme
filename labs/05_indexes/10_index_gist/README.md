# Lab 05.10 — GiST Index: Generalized Search Trees and Exclusion Constraints

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.09 complete; workers running. Superuser required for `CREATE EXTENSION btree_gist`.
**Estimated time:** 1 hr

> **Schema fit — honest assessment:** The current OrderFlow schema contains no
> geometric columns, IP address columns, or native range (`tsrange`, `daterange`)
> columns. GiST's primary use cases (spatial queries, nearest-neighbor, range
> overlap) have no natural home in the frozen M1 schema. Rather than add a
> fake column, this lab demonstrates GiST in the two contexts where it IS
> relevant to OrderFlow:
>
> 1. **As the index type required by exclusion constraints** — which Lab 04.01
>    introduced conceptually. Here we make it concrete with the synthetic
>    warehouse shift scheduling example, clearly labeled as synthetic.
>
> 2. **As the backing structure for the trigram index operator** — GiST can
>    also back trigram searches (as an alternative to GIN), providing a direct
>    comparison point with Lab 05.09.
>
> If the schema ever gains a `tsrange` or PostGIS geometry column (a possible
> future milestone), this lab is the right place to revisit.

---

## 1. Business Problem

**Part A (synthetic, clearly labeled):** The warehouse scheduling team wants to
prevent double-booking of dock workers — the same employee assigned to two
overlapping shifts at the same warehouse. A UNIQUE constraint only catches
identical values; it cannot detect partial overlap. An exclusion constraint with
GiST is the database-level solution, but it requires the `btree_gist` extension
to create GiST opclasses for scalar types like `BIGINT`.

**Part B (live OrderFlow schema):** The trigram GIN index from Lab 05.09 is
fast for large result sets. GiST can also back trigram searches (using
`gist_trgm_ops`) and is faster for similarity lookups that return a *small*
result set. Comparing the two shows where each index structure wins.

---

## 2. Observe

```bash
psql -U postgres -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Check if btree_gist is installed
SELECT * FROM pg_extension WHERE extname = 'btree_gist';
-- Likely empty — not installed yet
```

For Part B, observe the existing GIN trigram index behavior:

```sql
-- Similarity search — GIN trigram baseline
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name,
       similarity(first_name || ' ' || last_name, 'johnson') AS sim
FROM   customers
WHERE  (first_name || ' ' || last_name) % 'johnson'
ORDER BY sim DESC
LIMIT 5;
```

---

## 3. Measure (Baseline)

**Part A:** Without `btree_gist`, the exclusion constraint on a scalar `employee_id`
column cannot be created — `CREATE EXTENSION btree_gist` is a prerequisite.

**Part B:** GIN trigram similarity search returns results in ~3–5 ms for a
similarity threshold of 0.3 (from Lab 05.09).

---

## 4. Optimize

### Part A — Exclusion constraint via GiST (synthetic demonstration)

```sql
-- Install btree_gist to enable GiST opclasses on scalar types
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- Create a demonstration table for warehouse shift scheduling
-- SYNTHETIC: this table is not part of the frozen M1 schema.
-- It is created here to demonstrate GiST exclusion constraints
-- and will be dropped in the Cleanup section.
CREATE TABLE orderflow.demo_shifts (
    shift_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employee_id BIGINT     NOT NULL
                    REFERENCES orderflow.employees (employee_id),
    warehouse_id BIGINT    NOT NULL
                    REFERENCES orderflow.warehouses (warehouse_id),
    shift_period tsrange   NOT NULL,
    CONSTRAINT demo_no_overlapping_shifts
        EXCLUDE USING gist (
            employee_id  WITH =,       -- same employee
            shift_period WITH &&       -- AND overlapping time range
        )
);

-- Insert a valid shift
INSERT INTO demo_shifts (employee_id, warehouse_id, shift_period)
SELECT employee_id, 1, '[2024-06-01 08:00, 2024-06-01 16:00)'
FROM employees WHERE is_active LIMIT 1;

-- Insert a non-overlapping shift for the same employee — succeeds
INSERT INTO demo_shifts (employee_id, warehouse_id, shift_period)
SELECT employee_id, 1, '[2024-06-01 17:00, 2024-06-02 01:00)'
FROM employees WHERE is_active LIMIT 1;

-- Insert an overlapping shift — fails with exclusion violation
INSERT INTO demo_shifts (employee_id, warehouse_id, shift_period)
SELECT employee_id, 1, '[2024-06-01 14:00, 2024-06-01 22:00)'
FROM employees WHERE is_active LIMIT 1;
-- ERROR: conflicting key value violates exclusion constraint "demo_no_overlapping_shifts"
```

Inspect the GiST index PostgreSQL created automatically for the exclusion:

```sql
\d demo_shifts
-- Shows: demo_no_overlapping_shifts EXCLUDE USING gist (employee_id WITH =, shift_period WITH &&)
```

### Part B — GiST trigram index (comparison with Lab 05.09's GIN)

```sql
-- Create a GiST-backed trigram index on customers.name
-- (GIN already exists from Lab 05.09 — this is a comparison)
CREATE INDEX idx_customers_name_gist_trgm
    ON customers
    USING gist ((first_name || ' ' || last_name) gist_trgm_ops);
```

```sql
-- Compare: GiST vs GIN for a similarity search with a small result set
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name,
       similarity(first_name || ' ' || last_name, 'johnson') AS sim
FROM   customers
WHERE  (first_name || ' ' || last_name) % 'johnson'
ORDER BY sim DESC
LIMIT 5;
```

---

## 5. Measure Again

**Part A:** The exclusion constraint fires synchronously on conflicting INSERT,
returning an error before any row is written. The protection is 100 % — no
application-level lock required.

**Part B representative output (GiST trigram, `LIMIT 5`):**

```
Index Scan using idx_customers_name_gist_trgm on customers
                    (cost=0.41..52.30 rows=5 width=108)
                    (actual time=0.312..1.234 rows=5 loops=1)
  Index Cond: (... % 'johnson')
  Buffers: shared hit=12
Execution Time: 1.312 ms
```

**GiST vs. GIN — trigram use case:**

| | GiST trigram | GIN trigram |
|---|---|---|
| Small result set (LIMIT 5) | ~1.3 ms | ~4.2 ms |
| Large result set (all matches) | slower | faster |
| Supports `ORDER BY similarity` using index | ✓ | ✗ |
| Index size | smaller | larger |
| Write overhead | lower | higher |

GiST wins for nearest-neighbor similarity (`ORDER BY sim DESC LIMIT N`) and
small result sets. GIN wins for containment and large result sets.

---

## 6. Explain

GiST (Generalized Search Tree) is a framework, not a specific index structure.
It lets PostgreSQL extensions define their own index key types and search
predicates by implementing a fixed set of methods (consistent, union, penalty,
picksplit, compress, decompress, distance). The tree is always balanced, and
every node stores a *bounding key* — a lossless or lossy summary of all keys
in that subtree.

**For exclusion constraints:** GiST can represent non-equality operators (`&&`
for range overlap) because its `consistent` method can evaluate any supported
operator. A UNIQUE constraint uses B-tree, which only supports `=`. An
exclusion constraint routes through GiST, which can answer "does any existing
key conflict with the new key under operator X?"

**`btree_gist`** extends GiST to support scalar types (integers, dates, text)
that normally use B-tree. It implements `consistent`, `union`, etc. for these
types so they can participate in GiST exclusion constraints alongside range
types.

**For trigram similarity:** The GiST trigram node stores a signature (a bloom
filter of trigrams) rather than the full trigram set. This is lossy —
false positives require a recheck — but the tree structure supports nearest-
neighbor search via the `<->` (distance) operator and ordered scans by
similarity score. GIN stores exact inverted lists and excels at set
intersection (finding all rows with all query trigrams) but cannot do ordered
nearest-neighbor scans natively.

---

## 7. Cleanup / Reset Note

```sql
-- Drop the synthetic demonstration table (not part of the M1 schema)
DROP TABLE orderflow.demo_shifts;

-- Drop the GiST trigram index (GIN from Lab 05.09 is the production choice
-- for large result set containment queries)
DROP INDEX idx_customers_name_gist_trgm;

-- btree_gist extension PERSISTS — it is required by the exclusion constraint
-- pattern and may be needed by future labs.
```

`btree_gist` extension persists. All other changes from this lab are cleaned up.

---

## Further Reading

- [PostgreSQL docs — GiST Indexes](https://www.postgresql.org/docs/current/gist.html)
- [PostgreSQL docs — btree_gist](https://www.postgresql.org/docs/current/btree-gist.html)
- [PostgreSQL docs — Exclusion Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-EXCLUSION)
