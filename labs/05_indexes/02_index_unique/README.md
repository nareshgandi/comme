# Lab 05.02 — Unique Index: Constraint vs. Index, and Deferability

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.01 complete; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

Lab 04.01 added `payments_one_success_per_order` as a partial unique index to
enforce INV-05. A new engineer asks: "Why did you use `CREATE UNIQUE INDEX`
instead of `UNIQUE` in the table definition? They look the same." They are not
the same. This lab explores the difference, demonstrates where a unique
*index* beats a unique *constraint*, and shows one critical scenario — bulk
data migration — where a non-deferrable unique constraint would fail but a
deferrable one succeeds.

A second real problem: `products.sku` must be globally unique, but the current
application wants to rename a batch of SKUs (e.g. `ELEC-0001` → `ELEC-N-0001`)
in a single transaction by swapping values through a temporary intermediate.
A non-deferrable unique constraint blocks this; a deferrable one does not.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Verify what indexes currently back the unique constraints
SELECT
    i.relname  AS index_name,
    ix.indisunique,
    ix.indisprimary,
    ix.indisvalid,
    pg_get_indexdef(ix.indexrelid) AS definition
FROM pg_index ix
JOIN pg_class i ON i.oid = ix.indexrelid
JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'orderflow'
  AND t.relname = 'products'
ORDER BY i.relname;
```

Now attempt the SKU swap without a deferrable constraint:

```sql
-- Simulate a batch rename: ELEC-0001 → ELEC-N-0001
-- First, see the current SKU
SELECT product_id, sku FROM products WHERE sku LIKE 'ELEC-%' LIMIT 2;
-- note: product_id 1 = 'ELEC-0001', product_id 2 = 'ELEC-0002'

-- Try to swap two SKUs in a single transaction
BEGIN;
  UPDATE products SET sku = 'ELEC-0002' WHERE product_id = 1;  -- would collide!
-- ERROR: duplicate key value violates unique constraint "products_sku_key"
ROLLBACK;
```

**Observation:** The non-deferrable UNIQUE constraint fires *immediately* on
each `UPDATE` statement, before the transaction commits. It sees the
intermediate state where both rows temporarily share a value.

---

## 3. Measure (Baseline)

**Baseline:** The SKU swap transaction above errors at the first UPDATE with a
unique constraint violation. The batch rename cannot be completed in a single
transaction; the application must use a temporary SKU (e.g. `TEMP-9999`) as an
intermediate value, adding complexity and error-prone application code.

---

## 4. Optimize

### Part A — Unique Index vs. Unique Constraint (same B-tree, different metadata)

```sql
-- A UNIQUE CONSTRAINT (implicit index, named by constraint name):
ALTER TABLE products
    ADD CONSTRAINT products_sku_key UNIQUE (sku);
-- Note: this already exists in the schema. We're just inspecting it.

-- A UNIQUE INDEX (explicit, full control):
CREATE UNIQUE INDEX CONCURRENTLY products_sku_unique_idx
    ON products (sku);
-- This creates a second, redundant unique index on the same column.
-- Don't leave both in place; drop the demonstration one after observing it.
```

The underlying structure is identical: both create a B-tree index. The
differences:
- **Naming:** A constraint lets you `DROP CONSTRAINT`; an index requires
  `DROP INDEX`.
- **Deferability:** Only a `UNIQUE CONSTRAINT` declared `DEFERRABLE` (not a
  bare `CREATE UNIQUE INDEX`) can be deferred within a transaction.
- **Partial unique:** Only `CREATE UNIQUE INDEX … WHERE` supports a predicate.
  `ADD CONSTRAINT … UNIQUE` cannot be partial. (This is why `payments_one_success_per_order`
  from Lab 04.01 was built as an index, not a constraint.)

```sql
-- Drop the redundant demonstration index immediately
DROP INDEX products_sku_unique_idx;
```

### Part B — Deferrable unique constraint for batch SKU rename

```sql
-- Drop the existing non-deferrable unique constraint on products.sku
-- and replace it with a deferrable one.

-- This requires superuser or the table owner:
ALTER TABLE products DROP CONSTRAINT products_sku_key;

ALTER TABLE products
    ADD CONSTRAINT products_sku_key
    UNIQUE (sku)
    DEFERRABLE INITIALLY IMMEDIATE;
```

Now the SKU swap works:

```sql
-- Find two products to swap
SELECT product_id, sku FROM products WHERE sku LIKE 'ELEC-%' ORDER BY product_id LIMIT 2;

BEGIN;
  -- Defer uniqueness check to end of transaction
  SET CONSTRAINTS products_sku_key DEFERRED;

  -- Swap SKUs through a temporary value approach — OR simply update both
  -- in a single transaction, which now works
  UPDATE products SET sku = 'ELEC-TEMP-9999' WHERE product_id = 1;
  UPDATE products SET sku = 'ELEC-0001'      WHERE product_id = 2;
  UPDATE products SET sku = 'ELEC-0002'      WHERE product_id = 1;
COMMIT;
-- Succeeds! The unique check runs at COMMIT time, not after each UPDATE.
-- Restore the original SKUs:
BEGIN;
  SET CONSTRAINTS products_sku_key DEFERRED;
  UPDATE products SET sku = 'ELEC-0001' WHERE product_id = 1;
  UPDATE products SET sku = 'ELEC-0002' WHERE product_id = 2;
COMMIT;
```

---

## 5. Measure Again

```sql
-- Same SKU swap attempt, now with DEFERRABLE constraint
BEGIN;
  SET CONSTRAINTS products_sku_key DEFERRED;
  UPDATE products SET sku = 'ELEC-TEMP-X' WHERE product_id = 1;
  UPDATE products SET sku = 'ELEC-0001'   WHERE product_id = 2;
  UPDATE products SET sku = 'ELEC-0002'   WHERE product_id = 1;
COMMIT;
```

**After:** Transaction commits successfully.
**Delta:** Batch SKU rename completes in a single, clean transaction with no
temporary workaround values — zero error-handling complexity removed from the
application layer.

```sql
-- Restore SKUs to original state before cleanup
BEGIN;
  SET CONSTRAINTS products_sku_key DEFERRED;
  UPDATE products SET sku = 'ELEC-0001' WHERE product_id = 1;
  UPDATE products SET sku = 'ELEC-0002' WHERE product_id = 2;
COMMIT;
```

---

## 6. Explain

**INITIALLY IMMEDIATE vs. INITIALLY DEFERRED:** A constraint declared
`DEFERRABLE INITIALLY IMMEDIATE` runs after every statement by default (same as
a non-deferrable constraint) but can be switched to deferred mode within a
transaction via `SET CONSTRAINTS … DEFERRED`. When deferred, PostgreSQL
postpones the constraint check to transaction commit time. If the constraint is
violated at commit time, the entire transaction rolls back.

This works because the B-tree index the constraint uses is shared transactional
state. PostgreSQL can write intermediate states into the index within a
transaction without making them visible to other transactions (MVCC), and then
enforce uniqueness at commit time by checking whether the final committed state
violates the constraint.

**Why `CREATE UNIQUE INDEX … WHERE` cannot be deferrable:** Partial index
predicates create a fundamentally different check path — the constraint is not
on a complete key, so the deferred evaluation path is not implemented for
partial unique indexes in PostgreSQL.

---

## 7. Cleanup / Reset Note

The `products_sku_key` constraint has been **modified** (from non-deferrable to
deferrable). This change persists intentionally — it demonstrates the correct
production pattern for tables whose natural keys occasionally need batch renames.

Verify the final state:

```sql
SELECT conname, condeferrable, condeferred
FROM pg_constraint
WHERE conrelid = 'products'::regclass
  AND conname = 'products_sku_key';
-- condeferrable should be TRUE, condeferred FALSE (INITIALLY IMMEDIATE)
```

---

## Further Reading

- [PostgreSQL docs — Unique Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-UNIQUE-CONSTRAINTS)
- [PostgreSQL docs — SET CONSTRAINTS](https://www.postgresql.org/docs/current/sql-set-constraints.html)
