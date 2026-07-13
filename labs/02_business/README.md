# Lab 02 — The OrderFlow Business Story

**Type:** Foundation write-up (no Business Problem / Optimize shape)
**Prerequisite:** Lab 01 complete; `business_rules.md` read.
**Time:** 20 minutes reading; 30 minutes exploring in psql.

This write-up is meant to be read *before* touching SQL. It answers the
question: what is actually happening in this database?

---

## The Business

OrderFlow is an online retailer with a warehouse-and-courier fulfillment model.
Customers browse a product catalog, place orders, and receive goods shipped from
one of four regional warehouses. The company employs warehouse staff, couriers,
finance staff, managers, and administrators.

This is not a novel business. It is deliberately generic — every person who has
ordered anything online already understands how it works. That familiarity is
intentional: when you see a slow query in Lab 05, you should immediately
understand *why* it matters to a real business, not just why it matters
academically.

---

## The Entities

### Customers

Customers have a name, contact details, a shipping address, and a loyalty tier
(`bronze`, `silver`, `gold`, `platinum`). Loyalty tier determines nothing in
the workers' logic — it exists as a useful low-cardinality column for the index
and statistics labs later.

The customer pool is large by design: the default bootstrap loads 100 000
customers. Realistic customer counts make query plans interesting — a Seq Scan
on a 100 k-row table takes noticeably longer than on a 1 000-row table, which
is exactly the point of Lab 05.

### Products

Products have a SKU (category-prefixed, e.g. `ELEC-0001`), a name, a category,
a unit price, an optional weight, and a `metadata` JSONB column. The JSONB
column is intentionally schema-flexible: Electronics might have `brand` and
`warranty_years`; Food might have `diet` and `allergens`. This makes it the
natural target for the GIN index and JSONB query labs within Lab 05, and for
the `pgvector` embedding lab in Lab 10.

### Warehouses

Four physical fulfillment centres, one per region:

| Code | Region | City |
|------|--------|------|
| WH-USE-01 | us-east | Newark, NJ |
| WH-USW-01 | us-west | Reno, NV |
| WH-USC-01 | us-central | Kansas City, MO |
| WH-EUC-01 | eu-central | Frankfurt, DE |

Every order is assigned a warehouse at creation time (drawn from
`is_active = TRUE` warehouses). The region codes match the foreign server names
used in the FDW lab (Lab 10), where each warehouse region becomes a separate
logical data source.

### Employees

Five roles with distinct department mappings:

| Role | Department | Salary range (USD/yr) |
|------|------------|----------------------|
| `warehouse_staff` | Fulfillment | 35 000 – 55 000 |
| `courier` | Logistics | 38 000 – 58 000 |
| `finance` | Finance | 60 000 – 95 000 |
| `manager` | Operations | 75 000 – 120 000 |
| `admin` | Administration | 70 000 – 110 000 |

Only `warehouse_staff` and `manager` roles are eligible to be assigned to
orders (INV-05 relates to the active warehouse constraint; employee assignment
is probabilistic and optional — `employee_id` is nullable on orders).

The `salary` column and the employee's personal data are the natural targets
for the Row-Level Security and `pgcrypto` encryption labs in Lab 09.

### Orders

Orders are the central table. Every other table either feeds into an order
(customer, products, warehouse, employee) or records what happened to it
(order_items, payments).

An order carries:
- Status (drives the lifecycle state machine)
- A shipping address (copied from the customer at order time — not a FK)
- Optional notes
- `total_amount` (kept in sync with `SUM(order_items.line_total)`)
- `shipped_at` and `delivered_at` timestamps (populated as the order progresses)

### Order Items

Each order has one or more line items. A line item records the product,
quantity, unit price (snapshot of `products.unit_price` at order time), and
an optional discount percentage. `line_total` is a stored generated column —
you cannot write to it.

Order items are **immutable after insert** (INV-07). Workers never update them.
This is not just a business rule — it is what makes `order_items` a natural
target for append-only partitioning strategies, and it is why VACUUM has
little work to do on this table (few or no dead tuples).

### Payments

Each order can have multiple payment rows: failed attempts (retries) plus the
single successful payment, and optionally a refund row. The rule is: exactly
one payment per order may have `status = 'SUCCESS'` (INV-05). A partial unique
index enforces this in Lab 05.

The `gateway_reference` column is the external payment processor's transaction
ID. `processed_at` records when the gateway responded. Both are auditable —
`pgaudit` in Lab 09 is configured specifically on this table.

---

## The Order Lifecycle

The complete state machine (from `business_rules.md`):

```
NEW
 │  payment attempt → SUCCESS
 ▼
PROCESSING
 │  warehouse packs items
 ▼
PACKED
 │  courier picks up
 ▼
SHIPPED
 │  delivery confirmed
 ▼
DELIVERED ──── customer initiates return ──► RETURNED ──► REFUNDED (terminal)
```

Each transition is driven by a separate Python worker. No transition is
instantaneous — workers apply configurable minimum-age checks between states
(e.g. a PROCESSING order waits at least 30 seconds before becoming PACKED).
This produces the realistic mixed-status distribution you see in the live data.

### The invariants that matter most

Ten business invariants are defined in `business_rules.md`. The four most
useful to keep in mind when querying:

- **INV-01/02:** `shipped_at` and `delivered_at` are non-null when status
  requires them. You can use this to validate data quality.
- **INV-04:** No order advances past PROCESSING without a `SUCCESS` payment.
  An order stuck in NEW with all payments FAILED is an "abandoned" order.
- **INV-06:** `orders.total_amount = SUM(order_items.line_total)` at all times.
  Useful for integrity checks after any migration.
- **INV-07:** `order_items` rows are never updated. This is the reason
  `order_items` has essentially zero VACUUM work — worth verifying in Lab 11.

---

## The Payment Flow

The payment worker operates a mini payment gateway simulation:

1. Pick up a `NEW` order (using `FOR UPDATE SKIP LOCKED` to avoid contention
   with other worker instances).
2. Simulate a gateway call — succeeds with probability
   `1 - payment_failure_rate` (default 92 %).
3. On success: mark payment `SUCCESS`, advance order to `PROCESSING`.
4. On failure: mark payment `FAILED`, order stays `NEW`. Retry up to
   `max_payment_retries` times (default 3). After exhausting retries, the
   order is abandoned in `NEW` with all payments `FAILED`.

This means the `payments` table has a natural mix: mostly `SUCCESS` rows, some
`FAILED` rows (retries), and a small fraction of `REFUNDED` rows. The
`pg_cron` lab in Lab 10 schedules a cleanup job for abandoned orders.

---

## Connecting the Workers to the Data

At any given moment, if you query `pg_stat_activity` you will see four
application names:

| `application_name` | What it is doing |
|--------------------|------------------|
| `order_generator` | INSERTing new orders + order_items in batches |
| `payment_processor` | Selecting NEW orders, INSERTing payments, updating status |
| `order_processor` | Advancing PROCESSING → PACKED → SHIPPED → DELIVERED → RETURNED |
| `employee_updates` | Random UPDATEs on the `employees` table |

This is observable right now: `SELECT pid, application_name, state, query FROM
pg_stat_activity WHERE application_name LIKE '%order%' OR application_name
LIKE '%payment%' OR application_name LIKE '%employee%';`

---

## What This Enables

| Later lab | What it needs from the business story |
|-----------|--------------------------------------|
| Lab 04 (PostgreSQL Core) | The lifecycle state machine is the perfect transaction demonstration — each transition is a `BEGIN … COMMIT` block |
| Lab 05 (Indexes) | Low-cardinality columns (`status`, `loyalty_tier`) vs. high-cardinality (`email`, `gateway_reference`) for index selectivity discussion |
| Lab 09 (Security) | `employees.salary` and `customers.email` as PII requiring RLS and encryption |
| Lab 10 (Extensions) | `payments` as the pg_cron cleanup target; `products.metadata` JSONB for pgvector |
| Lab 11 (Monitoring) | Understanding *why* `employee_updates.py` exists — it generates UPDATE-heavy WAL and dead tuples specifically to give autovacuum something to do |
