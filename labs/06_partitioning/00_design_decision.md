# Partitioning Design Decision — `orders` Table

**Status: AWAITING SIGN-OFF — do not proceed to migration until approved.**

---

## The Requirement

Milestone 8 converts `orders` to declarative RANGE partitioning on
`created_at`. This is the natural, correct choice for OrderFlow: orders arrive
monotonically in time, queries are almost always time-bounded, and BRIN indexes
(Lab 05.12) are most effective on correlated data. The schema comment for
`orders.created_at` in `001_initial_schema.sql` explicitly anticipates this:
*"created_at is the future RANGE partition key (Lab 06)."*

---

## The Hard Constraint

PostgreSQL requires that **every UNIQUE and PRIMARY KEY constraint on a
partitioned table must include the partition key column(s)**.

This is a hard engine requirement, not a style preference. PostgreSQL enforces
global uniqueness across partitions by routing each INSERT through the correct
child partition; without the partition key in the constraint, it cannot guarantee
cross-partition uniqueness.

**Current schema (`001_initial_schema.sql`):**

```sql
orders.order_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
order_items.order_id BIGINT NOT NULL REFERENCES orderflow.orders(order_id) ON DELETE CASCADE
payments.order_id    BIGINT NOT NULL REFERENCES orderflow.orders(order_id)
```

After partitioning `orders` by `created_at`:

- `PRIMARY KEY (order_id)` alone is **not allowed** on the partitioned table.
  It must become `PRIMARY KEY (order_id, created_at)`.
- A FOREIGN KEY on `order_items.order_id` that references `orders(order_id)`
  **cannot reference a non-unique column** on the partitioned table.
  The referenced column(s) must form a unique/primary key — which now requires
  `(order_id, created_at)` to be the referenced columns.
- Same constraint applies to `payments.order_id`.

---

## The Options

### Option A — Composite FK: carry `order_created_at` on child tables

Add `order_created_at TIMESTAMPTZ NOT NULL` to both `order_items` and
`payments`. Change both FKs to reference `(order_id, order_created_at)`.

```sql
-- order_items
order_created_at TIMESTAMPTZ NOT NULL,
FOREIGN KEY (order_id, order_created_at) REFERENCES orders(order_id, created_at)

-- payments
order_created_at TIMESTAMPTZ NOT NULL,
FOREIGN KEY (order_id, order_created_at) REFERENCES orders(order_id, created_at)
```

**Upside:** Database-enforced FK integrity is fully preserved.

**Fatal downside for this project:** The workers must change.

- `order_generator.py`: inserts order_items in the same transaction as the
  order, so it has `NOW()` — but it would need to propagate the actual
  `orders.created_at` value returned from the order INSERT. Currently it uses
  a psycopg COPY stream for order_items that does not carry a created_at.
  The worker code must be restructured.
- `payment_processor.py`: processes payments in a separate transaction, long
  after the order was created. It would need to `SELECT orders.created_at`
  before every `INSERT INTO payments` to obtain the partition-routing column.
  That is a new query per payment — a behavioral change, and a worker code
  change.
- `order_processor.py`: inserts refund payments (RETURNED → REFUNDED path).
  Same lookup requirement as payment_processor.

The M8 design constraint is explicit: *"Workers must run unmodified against
the partitioned table — if you find yourself wanting to edit
`python/workers/`, stop and reconsider the partitioning design instead."*

**Option A is disqualified** by this constraint.

---

### Option B — Drop the enforced FKs; rely on application-level integrity

Remove `REFERENCES orderflow.orders(order_id)` from `order_items.order_id`
and `payments.order_id`. The `ON DELETE CASCADE` on `order_items.order_id`
is also dropped as part of this.

`orders.order_id` remains the sole primary key column; the partition key
`created_at` is added to the constraint to form `PRIMARY KEY (order_id, created_at)`.
`order_id` retains a per-partition UNIQUE constraint on `(order_id, created_at)`
and the planner can still use it for routing.

The uniqueness of `order_id` across all partitions is preserved by the
sequence (`GENERATED ALWAYS AS IDENTITY`), not by the partitioned PK alone.
PostgreSQL does not enforce global uniqueness of a single column across
partitions unless a global index exists — but since `order_id` is a monotonic
identity column, collisions are structurally impossible. This is not a
hidden assumption; it is a documented property of `GENERATED ALWAYS AS
IDENTITY` backed by a single sequence.

**Upside:**
- Workers run completely unmodified. All three workers reference `order_id`
  as a lookup key; none of them depend on the FK constraint itself. The
  INSERT and UPDATE patterns are unchanged.
- Pedagogically honest: real production systems regularly trade away enforced
  FK integrity for partition flexibility, especially at scale. Teaching this
  trade-off explicitly is more valuable than hiding it.

**Downside:**
- Database can no longer catch a bug that inserts a `payment` or `order_item`
  with a non-existent `order_id`. In this system, the only writers are the
  Python workers, which never generate orphan rows. But a future tool, a
  direct `psql` session, or a worker bug could silently insert orphans.
- `ON DELETE CASCADE` on `order_items.order_id` is lost. This only mattered
  for direct `DELETE FROM orders` operations, which workers never perform.
  Orders reach terminal states (DELIVERED, REFUNDED) via status transitions,
  not deletes. The teaching value of `ON DELETE CASCADE` was demonstrated in
  Lab 04.01 (Constraints) before this migration, which is correct sequencing.

**Mitigation — application-level orphan check:**

A consistency verification query is documented in the migration and in the
lab:

```sql
-- INV-11 (application-level, post-partitioning): no orphan order_items
SELECT COUNT(*) FROM order_items oi
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = oi.order_id);

-- INV-12 (application-level, post-partitioning): no orphan payments
SELECT COUNT(*) FROM payments p
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_id = p.order_id);
```

These queries can be run as a sanity check in Lab 11 (Monitoring) and can be
scheduled with `pg_cron` in Milestone 13.

---

## Recommendation

**Proceed with Option B.**

The workers cannot change — that constraint is absolute for this milestone.
The identity sequence makes orphan `order_id`s structurally impossible from
the workers. The FK was providing meaningful protection only against
out-of-band writes (direct `psql`, future tooling), and that gap is
acceptable in a teaching system with documented compensating queries.

The pedagogically correct framing is: *this is a real production trade-off
that DBAs encounter every time they partition a large table with
many-to-one child tables. Knowing the options and their costs — not
pretending the problem doesn't exist — is the skill.*

---

## What the Migration Does

If this decision is approved, `database/migrations/002_partition_orders.sql`
will:

1. Rename the existing `orderflow.orders` to `orderflow.orders_old`.
2. Create `orderflow.orders` as a new declarative RANGE-partitioned table
   with `PRIMARY KEY (order_id, created_at)`.
3. Drop the FK constraints on `order_items.order_id` and `payments.order_id`.
4. Copy all rows from `orders_old` into the new partitioned `orders` using
   a bulk INSERT (or `COPY` export/import for large tables).
5. Drop `orders_old`.
6. Recreate all indexes from M7 (BRIN on `created_at`, B-tree on `customer_id`,
   active-status partial index, warehouse B-tree) on the partitioned table.
7. Create monthly child partitions from the earliest month in the data
   through the current month + 2 (forward provision).
8. Add a DEFAULT partition to catch any out-of-range future rows if the
   monthly provisioning script falls behind.

`001_initial_schema.sql` is **not touched**. The M1 migration remains
historically accurate — it shows the schema as originally designed. Migration
`002` is the evolution, same as any production system.

---

**⛔ STOP — awaiting explicit sign-off before writing the migration.**

Reply with "approved" (or any approval) to proceed to writing
`database/migrations/002_partition_orders.sql` and the six partitioning labs.
