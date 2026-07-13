# Lab 04.01 — Constraints: Database-Level Data Integrity

**Phase:** Business Simulation
**Prerequisites:** Labs 01–03 complete; all four OrderFlow workers running (`python bootstrap.py --status`).
**Estimated time:** 1 hr

---

## 1. Business Problem

A data quality audit finds that `business_rules.md` defines 10 invariants for
OrderFlow, but several are enforced only by the Python workers — not by the
database itself. Worker bugs, direct `psql` access by an engineer, or a future
tool that bypasses the workers can all violate these invariants silently. The
audit asks: what happens if `payment_processor.py` has a race-condition bug and
records two `SUCCESS` payments for the same order? The database will accept
both. This lab inventories the constraints already in place, demonstrates the
gap, and closes it.

---

## 2. Observe

Connect to the database and inspect what the schema already protects:

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- List every constraint on every orderflow table
SELECT
    tc.table_name,
    tc.constraint_name,
    tc.constraint_type,
    kcu.column_name,
    cc.check_clause
FROM information_schema.table_constraints tc
LEFT JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
   AND tc.table_schema    = kcu.table_schema
LEFT JOIN information_schema.check_constraints cc
    ON tc.constraint_name = cc.constraint_name
   AND tc.constraint_schema = cc.constraint_schema
WHERE tc.table_schema = 'orderflow'
ORDER BY tc.table_name, tc.constraint_type, tc.constraint_name;
```

Then demonstrate the INV-05 gap. In a second `psql` session (or after noting
the first session is not in a transaction), run:

```sql
-- Simulate a worker bug: two SUCCESS payments on the same order
-- First, find a real order that already has a SUCCESS payment
SELECT order_id, status FROM orders WHERE status = 'PROCESSING' LIMIT 1;

-- Note the order_id returned (e.g. 42), then:
INSERT INTO payments (order_id, amount, method, status, gateway_reference)
VALUES (42, 99.99, 'credit_card', 'SUCCESS', 'FAKE-DUPLICATE-001');

-- Does the second INSERT succeed? It should — there is no DB-level constraint
-- yet. Check:
SELECT order_id, status, gateway_reference
FROM payments
WHERE order_id = 42 AND status = 'SUCCESS';
```

---

## 3. Measure (Baseline)

**Baseline — INV-05 gap:** The `INSERT` above succeeds. `SELECT COUNT(*) FROM payments WHERE order_id = 42 AND status = 'SUCCESS'` returns `2`. The database silently accepted a duplicate SUCCESS payment, violating INV-05.

To confirm the current count of "corrupt" orders (should be 0 or 1 from our
test above):

```sql
SELECT order_id, COUNT(*) AS success_count
FROM payments
WHERE status = 'SUCCESS'
GROUP BY order_id
HAVING COUNT(*) > 1;
```

---

## 4. Optimize

This section demonstrates each constraint type against a real OrderFlow
scenario, then closes the INV-05 gap.

### 4a — NOT NULL: required fields

`NOT NULL` is already present on every column that must have a value. Attempt
to violate one:

```sql
-- This fails immediately — NOT NULL is enforced by the database
INSERT INTO employees (first_name, last_name, email, role, salary)
VALUES ('Alice', NULL, 'alice@test.com', 'admin', 80000);
-- ERROR: null value in column "last_name" of relation "employees" violates not-null constraint
```

`NOT NULL` catches data entry bugs at the boundary — no `WHERE x IS NOT NULL`
defensive queries needed in application code.

### 4b — CHECK: domain validation

The schema uses CHECK constraints for domain integrity. Attempt a violation:

```sql
-- This fails — salary CHECK (salary >= 0) rejects it
INSERT INTO employees (first_name, last_name, email, role, salary)
VALUES ('Bob', 'Smith', 'bob@test.com', 'courier', -5000);
-- ERROR: new row for relation "employees" violates check constraint "employees_salary_check"

-- This also fails — role must be in the allowed set
INSERT INTO employees (first_name, last_name, email, role, salary)
VALUES ('Carol', 'Jones', 'carol@test.com', 'intern', 25000);
-- ERROR: new row for relation "employees" violates check constraint "employees_role_check"

-- The temporal invariant INV-03 is also a CHECK:
UPDATE orders SET delivered_at = '2024-01-01', shipped_at = '2024-01-05'
WHERE order_id = 1;
-- ERROR: new row for relation "orders" violates check constraint
--        "orders_shipped_before_delivered"
```

### 4c — UNIQUE: business-key uniqueness

The schema already enforces uniqueness on natural business keys:

```sql
-- employees and customers each have UNIQUE (email)
INSERT INTO customers (first_name, last_name, email, country)
VALUES ('Duplicate', 'Customer', 'alice@test.com', 'US');
-- (runs fine if 'alice@test.com' is not in customers)

-- Now try inserting the same email twice
INSERT INTO customers (first_name, last_name, email, country)
VALUES ('Duplicate2', 'Customer', 'alice@test.com', 'US');
-- ERROR: duplicate key value violates unique constraint "customers_email_key"
```

A UNIQUE constraint creates a B-tree index implicitly. You can also create a
`UNIQUE INDEX` directly for more control (name, deferability, partial scope) —
Lab 05.02 covers this distinction.

### 4d — PRIMARY KEY: identity and uniqueness

Every table uses `BIGINT GENERATED ALWAYS AS IDENTITY`. The PK constraint
combines NOT NULL + UNIQUE and is the anchor for every foreign key:

```sql
-- Attempt to INSERT with an explicit PK value — GENERATED ALWAYS prevents it
INSERT INTO orders
    (order_id, customer_id, warehouse_id, status, total_amount, shipping_country)
VALUES (99999, 1, 1, 'NEW', 0, 'US');
-- ERROR: cannot insert into column "order_id"
-- DETAIL: Column "order_id" is an identity column defined as GENERATED ALWAYS.

-- The correct pattern: omit the PK column entirely
INSERT INTO orders (customer_id, warehouse_id, status, total_amount, shipping_country)
VALUES (1, 1, 'NEW', 0, 'US')
RETURNING order_id;
-- Returns the database-assigned order_id
```

### 4e — FOREIGN KEY: referential integrity

Foreign keys prevent orphaned records. Observe cascading behavior:

```sql
-- Create a test order to experiment with
INSERT INTO orders (customer_id, warehouse_id, status, total_amount, shipping_country)
SELECT customer_id, 1, 'NEW', 50.00, 'US'
FROM customers
LIMIT 1
RETURNING order_id;
-- Note the returned order_id (e.g. 987654)

-- Add an order_item to it
INSERT INTO order_items (order_id, product_id, quantity, unit_price)
SELECT 987654, product_id, 1, unit_price
FROM products LIMIT 1;

-- ON DELETE CASCADE: deleting the order cascades to its order_items
DELETE FROM orders WHERE order_id = 987654;

-- Confirm the order_items are gone too (no orphans)
SELECT COUNT(*) FROM order_items WHERE order_id = 987654;  -- should return 0

-- ON DELETE RESTRICT: try to delete a customer who has orders
SELECT customer_id FROM orders LIMIT 1;  -- find a customer with an order
DELETE FROM customers WHERE customer_id = <that_id>;
-- ERROR: update or delete on table "customers" violates foreign key constraint
--        "orders_customer_id_fkey" on table "orders"
```

### 4f — Exclusion Constraints (GiST)

**Schema gap — honest assessment:** The current OrderFlow schema contains no
date-range, geometric, or interval columns that would naturally demonstrate an
exclusion constraint. No column will be added to the frozen schema just to
force this demonstration.

An exclusion constraint using GiST is the right tool when you need to prevent
*overlapping* values, not just *equal* values. The canonical example in a
fulfillment context would be warehouse staff shift scheduling:

```sql
-- SYNTHETIC EXAMPLE — not run against the live OrderFlow database.
-- Illustrates what exclusion constraints look like; not part of the M1 schema.

-- If OrderFlow had a shifts table:
CREATE TABLE orderflow.shifts (
    shift_id    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES orderflow.employees(employee_id),
    shift_period tsrange NOT NULL,
    CONSTRAINT no_overlapping_shifts
        EXCLUDE USING gist (
            employee_id WITH =,     -- same employee
            shift_period WITH &&    -- overlapping time range (&&  = "overlaps")
        )
);

-- This INSERT would succeed:
INSERT INTO shifts (employee_id, shift_period)
VALUES (1, '[2024-01-15 08:00, 2024-01-15 16:00)');

-- This INSERT would fail — same employee, overlapping shift:
INSERT INTO shifts (employee_id, shift_period)
VALUES (1, '[2024-01-15 14:00, 2024-01-15 22:00)');
-- ERROR: conflicting key value violates exclusion constraint "no_overlapping_shifts"
```

`=` on `employee_id` uses the `btree_gist` extension's GiST opclass for
integers. `&&` uses the built-in `tsrange` GiST opclass. A regular UNIQUE
constraint cannot express this — uniqueness alone does not catch partial
overlaps.

GiST is covered in depth in Lab 05.10.

### 4g — Close the INV-05 gap

First, clean up the duplicate SUCCESS payment we inserted during the Observe
step:

```sql
-- Remove the test duplicate
DELETE FROM payments
WHERE gateway_reference = 'FAKE-DUPLICATE-001';
```

Now add the partial unique index that makes INV-05 a database-level guarantee.
This index **persists as a permanent baseline change** — it enforces a core
business invariant and is referenced in later labs.

```sql
CREATE UNIQUE INDEX payments_one_success_per_order
    ON payments (order_id)
    WHERE status = 'SUCCESS';
```

Test it:

```sql
-- This must now fail at the DB level
INSERT INTO payments (order_id, amount, method, status, gateway_reference)
VALUES (42, 99.99, 'credit_card', 'SUCCESS', 'FAKE-DUPLICATE-002');
-- ERROR: duplicate key value violates unique constraint
--        "payments_one_success_per_order"
```

---

## 5. Measure Again

```sql
-- Re-run the duplicate test from section 2
INSERT INTO payments (order_id, amount, method, status, gateway_reference)
VALUES (42, 99.99, 'credit_card', 'SUCCESS', 'FAKE-DUPLICATE-003');
```

**After:** `ERROR: duplicate key value violates unique constraint "payments_one_success_per_order"`

**Delta:** The database now rejects duplicate SUCCESS payments unconditionally —
INV-05 is enforced even if `payment_processor.py` has a bug, a direct `psql`
session bypasses the worker, or a future tool fails to check. Zero code changes
needed in the application layer.

---

## 6. Explain

**CHECK and NOT NULL** are evaluated at row write time by the executor, before
the row reaches the heap. They add no storage overhead — they are predicates
evaluated inline.

**UNIQUE and PRIMARY KEY** work through a B-tree index. When a row is inserted
or updated, PostgreSQL checks the index for an existing entry with the same key.
The check is O(log n) against the index, not O(n) against the heap. A UNIQUE
constraint and a `CREATE UNIQUE INDEX` create the same B-tree structure
internally — the constraint is just declarative syntax on top.

**FOREIGN KEY** integrity is enforced by checking the referenced table's PK
index on every INSERT/UPDATE of the referencing column, and by checking for
dependent rows on DELETE. The `ON DELETE CASCADE` on `order_items.order_id`
causes the executor to emit DELETE operations for child rows as part of the
same transaction — no additional application code needed.

**Exclusion constraints** use a GiST index instead of a B-tree. GiST supports
arbitrary operator comparisons (overlap, containment, distance), not just
equality. The index stores a bounding structure that can answer "does any
existing value conflict with this new value under operator X?" — something a
B-tree cannot express for non-equality operators like `&&`.

**Partial unique index (INV-05):** A partial index only indexes rows matching
its `WHERE` predicate. The `payments_one_success_per_order` index contains one
entry per `order_id` that has a SUCCESS payment. On an INSERT with
`status = 'SUCCESS'`, PostgreSQL checks this index for the incoming `order_id`.
On an INSERT with any other status, the partial index is not checked at all —
it is invisible to those writes.

---

## 7. Cleanup / Reset Note

**`payments_one_success_per_order` persists.** This partial unique index closes
a real invariant gap (INV-05) and is the correct long-term baseline for the
OrderFlow schema. It is referenced in Lab 05.02 (Unique Index) and will be
analysed in Lab 05.12 (BRIN) and later.

All test rows inserted during this lab:
```sql
-- Remove the test order and its cascade (if not already gone)
DELETE FROM orders WHERE customer_id = 1 AND status = 'NEW' AND total_amount = 50.00;

-- Verify no orphan test data remains
SELECT COUNT(*) FROM payments WHERE gateway_reference LIKE 'FAKE-%';
-- Should return 0
```

---

## Further Reading

- [PostgreSQL docs — Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html)
- [PostgreSQL docs — Exclusion Constraints](https://www.postgresql.org/docs/current/ddl-constraints.html#DDL-CONSTRAINTS-EXCLUSION)
