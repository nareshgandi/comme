# Lab 05.09 — GIN + pg_trgm: Fuzzy Text Search

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.08 complete; workers running. Superuser required for `CREATE EXTENSION`.
**Estimated time:** 1 hr

> **Extension exception — documented:** `pg_trgm` is installed in this lab,
> ahead of the general "extensions wait for Milestone 13 / Lab 10" policy.
> This exception is justified because the M1 schema comment on `employees`
> explicitly named trigram search as a design intent: *"pg_trgm/GIN — trigram
> index on (first_name || ' ' || last_name) for fuzzy employee-name search.
> Index added in Lab 05."* Installing `pg_trgm` here fulfils the schema's
> stated design intent and is recorded in `roadmap.md` as a documented
> exception.

---

## 1. Business Problem

The HR portal lets managers search for employees by name to assign them to
orders. Managers type partial names or make typos: "Smth" instead of "Smith",
"alice" instead of "Alice". The existing `employees.email` lookup (Lab 05.05)
handles exact email; it does not help for name searches. A `LIKE '%smith%'`
query on 200 employees is fast today but the HR team is asking for the same
search across *all customers* — 100 000 rows — for a new "customer lookup by
partial name" feature. Without a trigram index, `LIKE '%smith%'` forces a full
sequential scan on every keystroke.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Baseline: partial name search on customers (100 000 rows) with no trigram index
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name, email
FROM   customers
WHERE  first_name ILIKE '%smith%'
   OR  last_name  ILIKE '%smith%';
```

**Representative output (~100 000 customers, no trigram index):**

```
Seq Scan on customers  (cost=0.00..3374.00 rows=20 width=88)
                       (actual time=0.041..189.234 rows=18 loops=1)
  Filter: (((first_name)::text ILIKE '%smith%') OR
           ((last_name)::text ILIKE '%smith%'))
  Rows Removed by Filter: 99982
  Buffers: shared hit=124 read=1751
Planning Time: 0.078 ms
Execution Time: 189.312 ms
```

`ILIKE '%...%'` (or `LIKE '%...%'`) with a leading wildcard cannot use a
B-tree index — the index is sorted by prefix, and a mid-string pattern has no
useful entry point.

---

## 3. Measure (Baseline)

**Baseline:** ~189 ms  |  Seq Scan on customers (~1 875 pages)  |  99 982 rows
examined for 18 matches

---

## 4. Optimize

### Step 1 — Install pg_trgm (superuser required)

```bash
# Connect as superuser (e.g. postgres) to install the extension
psql -U postgres -d orderflow
```

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

`pg_trgm` ships with PostgreSQL — no OS-level package install is needed.

### Step 2 — Create the trigram GIN indexes

```sql
-- Switch back to the orderflow role (or stay as postgres)
SET search_path = orderflow, public;

-- Customer full-name trigram search
-- Concatenate first and last name into one searchable string
CREATE INDEX idx_customers_name_trgm
    ON customers
    USING gin ((first_name || ' ' || last_name) gin_trgm_ops);

-- Employee name trigram search (schema design intent from M1)
CREATE INDEX idx_employees_name_trgm
    ON employees
    USING gin ((first_name || ' ' || last_name) gin_trgm_ops);
```

Both indexes **persist as permanent baseline changes** — they fulfil the
design intent stated in the M1 schema.

### Step 3 — Rewrite the query to use trigram similarity

```sql
-- Trigram similarity search — works with GIN trigram index
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name, email
FROM   customers
WHERE  (first_name || ' ' || last_name) ILIKE '%smith%';
```

For fuzzy (similarity) matching, use the `%` operator (similarity threshold):

```sql
-- Set similarity threshold (default 0.3 — adjust to taste)
SET pg_trgm.similarity_threshold = 0.3;

-- Similarity search: finds "Smyth", "Smithe", "Smtih"
SELECT customer_id, first_name, last_name,
       similarity(first_name || ' ' || last_name, 'smith') AS sim
FROM   customers
WHERE  (first_name || ' ' || last_name) % 'smith'
ORDER BY sim DESC
LIMIT  10;
```

---

## 5. Measure Again

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT customer_id, first_name, last_name, email
FROM   customers
WHERE  (first_name || ' ' || last_name) ILIKE '%smith%';
```

**Representative output after GIN trigram index:**

```
Bitmap Heap Scan on customers  (cost=76.00..312.45 rows=20 width=88)
                                (actual time=2.341..3.812 rows=18 loops=1)
  Recheck Cond: (((first_name || ' ' || last_name)) ILIKE '%smith%')
  Rows Removed by Recheck: 0
  Buffers: shared hit=45
Planning Time: 0.312 ms
Execution Time: 3.912 ms
```

**After:** ~3.9 ms  |  45 buffer reads
**Delta:** ~48× faster; buffer reads 1 875 → 45 (97.6% reduction)

---

## 6. Explain

A trigram is a sequence of three consecutive characters. The `pg_trgm`
extension decomposes each string into its set of trigrams: "Smith" becomes
`{" Sm", "Smi", "mit", "ith", "th "}`. The GIN index stores an inverted map
from each trigram to the list of rows containing it.

A query for `LIKE '%smith%'` is decomposed by the planner into its trigrams
(`{" sm", "smi", "mit", "ith", "th "}`), and the GIN index returns the
intersection of row-ID lists for all trigrams. PostgreSQL then performs a
Bitmap Heap Scan on those rows and applies the full `ILIKE` as a recheck
condition (to handle false positives — GIN results are candidate matches, not
guaranteed matches).

For **fuzzy similarity** (`%` operator), the trigram intersection size relative
to the union determines the Jaccard similarity score. Rows with a score above
`pg_trgm.similarity_threshold` are returned, even if they don't contain the
exact string — enabling typo tolerance.

**Why a GIN over a B-tree for this:** A B-tree on `last_name` can answer
`last_name = 'Smith'` or `last_name LIKE 'Sm%'` (prefix — the leading
characters are sorted). It cannot answer `last_name LIKE '%mith'` (suffix) or
`last_name LIKE '%mit%'` (infix) because there is no useful entry point in a
sorted tree for a mid-string pattern. The GIN trigram index has no sort order —
it is an inverted index that maps trigrams to rows, so any substring is a valid
query regardless of position.

---

## 7. Cleanup / Reset Note

`idx_customers_name_trgm` and `idx_employees_name_trgm` **persist** as
permanent baseline changes. The `pg_trgm` extension also persists — it is a
superuser-installed extension and cannot be removed without `DROP EXTENSION`.

These indexes are referenced in the monitoring lab (Lab 05.14 — VACUUM and
index maintenance) and in Lab 10 (Extensions), where `pg_trgm` is explored
further.

---

## Further Reading

- [PostgreSQL docs — pg_trgm](https://www.postgresql.org/docs/current/pgtrgm.html)
- [PostgreSQL docs — GIN Indexes](https://www.postgresql.org/docs/current/gin.html)
