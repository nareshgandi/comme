# OrderFlow — Business Rules (The Bible)

This document is the **single source of truth** for all Python workers, all
lab exercises, and all schema decisions. Workers must satisfy these rules.
Labs must not contradict them. If a later milestone reveals a conflict with
this document, **stop and flag the conflict** — do not quietly work around it.

---

## 1. Order Lifecycle

Every order moves forward through a linear state machine. The only backward
transitions are RETURNED and REFUNDED, which are terminal states.

```
                    ┌─────────────────────┐
                    │         NEW         │◄── order created
                    └──────────┬──────────┘
                               │  payment attempt → SUCCESS
                               ▼
                    ┌─────────────────────┐
                    │     PROCESSING      │  payment confirmed
                    └──────────┬──────────┘
                               │  warehouse packs items
                               ▼
                    ┌─────────────────────┐
                    │       PACKED        │  ready for pickup
                    └──────────┬──────────┘
                               │  courier picks up
                               ▼
                    ┌─────────────────────┐
                    │      SHIPPED        │  in transit
                    └──────────┬──────────┘
                               │  delivery confirmed
                               ▼
                    ┌─────────────────────┐
                    │     DELIVERED       │──── customer initiates return
                    └─────────────────────┘               │
                                                          ▼
                                               ┌──────────────────┐
                                               │    RETURNED      │
                                               └────────┬─────────┘
                                                        │  refund approved
                                                        ▼
                                               ┌──────────────────┐
                                               │    REFUNDED      │  terminal
                                               └──────────────────┘
```

### 1.1 Invariants (never violated — INV codes referenced in labs)

| ID | Rule |
|----|------|
| INV-01 | `status = 'SHIPPED'` requires `shipped_at IS NOT NULL` |
| INV-02 | `status IN ('DELIVERED','RETURNED','REFUNDED')` requires `delivered_at IS NOT NULL` |
| INV-03 | `shipped_at <= delivered_at` (enforced by DB CHECK constraint) |
| INV-04 | An order cannot advance past PROCESSING if no payment has `status = 'SUCCESS'` |
| INV-05 | Exactly **one** payment row per order may have `status = 'SUCCESS'` |
| INV-06 | `orders.total_amount` must equal `SUM(order_items.line_total)` at all times |
| INV-07 | `order_items` rows are **immutable** after insert — no updates, ever |
| INV-08 | A `status = 'REFUNDED'` order must have a payment row with `status = 'REFUNDED'` |
| INV-09 | `warehouse_id` on an order must reference an `is_active = TRUE` warehouse |
| INV-10 | An order must have at least one `order_items` row before leaving `NEW` status |

INV-03 is enforced at the database level via a CHECK constraint.
INV-04, INV-05, INV-09, INV-10 are enforced by workers. A future RLS / trigger
lab may harden selected invariants into the database layer — document the
change as a schema amendment rather than quietly patching the migration.

---

## 2. Payment Rules

### 2.1 Attempt Flow

The NEW → PROCESSING transition always requires a payment attempt:

1. Worker opens a transaction.
2. Worker inserts a `payments` row with `status = 'PENDING'`.
3. Worker simulates the gateway call using a configurable success probability
   (`config.payment_success_rate`, range 0.0–1.0).
4. **On success:**
   - Update `payments.status` → `'SUCCESS'`, set `payments.processed_at = NOW()`.
   - Update `orders.status` → `'PROCESSING'`.
   - Commit.
5. **On failure:**
   - Update `payments.status` → `'FAILED'`, set `payments.failure_reason`.
   - Order remains `'NEW'`.
   - Commit.
6. Failed orders are retried up to `config.max_payment_retries` times (each
   retry creates a new `payments` row — the failed rows are kept for audit).
7. After exhausting retries, the order is abandoned: it remains in `'NEW'`
   with all payment attempts recorded as `'FAILED'`. Workers will not pick it
   up again. (A future pg_cron lab will schedule cleanup of abandoned orders.)

### 2.2 Refund Flow

1. Only `status = 'RETURNED'` orders are eligible for a refund.
2. Worker inserts a new `payments` row with `status = 'REFUNDED'` for the
   same amount as the original SUCCESS payment.
3. Worker updates `orders.status` → `'REFUNDED'`.
4. Both changes occur in a single transaction.
5. Partial refunds are **not supported** in v1.

### 2.3 Constraints Summary

- `payments.amount` must be > 0 (enforced by DB CHECK).
- `payments.method` must be one of: `credit_card`, `debit_card`, `paypal`,
  `bank_transfer`, `wallet` (enforced by DB CHECK).
- Multiple payment rows per order are allowed (retries + refund). Only one
  may have `status = 'SUCCESS'` (enforced by worker; a partial unique index
  on `(order_id) WHERE status = 'SUCCESS'` will be added in Lab 05).

---

## 3. Return Rules

### 3.1 Eligibility

- Only `status = 'DELIVERED'` orders are eligible for return.
- Minimum delivery age before a return can be initiated: configurable via
  `config.return_min_days_after_delivery` (simulates customer use period).
- Return probability: configurable via `config.return_probability` (range
  0.0–1.0). Applied by the worker each cycle when scanning DELIVERED orders.

### 3.2 Flow

1. Worker selects DELIVERED orders whose `delivered_at` is older than
   `return_min_days_after_delivery`.
2. Worker applies `return_probability` to decide whether to initiate a return.
3. Worker updates `orders.status` → `'RETURNED'`.
4. The order is now eligible for the refund flow (§2.2).

---

## 4. Warehouse Assignment

- Every order is assigned a warehouse at creation time.
- Assignment rule: randomly select from warehouses where `is_active = TRUE`.
- The set of warehouse regions (and their weights) is configurable in
  `config.yaml` (`warehouses` section).
- If no active warehouse exists, the worker must raise an error rather than
  inserting a NULL `warehouse_id`.

---

## 5. Employee Assignment

- `orders.employee_id` is optional (nullable).
- When assigned, the employee must have `is_active = TRUE` and role in
  `('warehouse_staff', 'manager')`.
- Assignment probability and rules are configurable in `config.yaml`.

---

## 6. Data Entry Principles

- **No SQL seed scripts.** All rows enter the database via Python factories
  (Milestone 3) and workers (Milestone 5). No exceptions.
- Factories use the `Faker` library for realistic names, emails, and addresses.
- All counts (customer count, product count, warehouse count, employee count)
  are read from `config.yaml` — never hardcoded.

---

## 7. Correct Order of Operations

### Seeding (one-time, on bootstrap)

```
employees → warehouses → customers → products
```

These four tables have no inter-dependencies and could be seeded in parallel,
but the order above is the canonical sequence for deterministic seeding logs.

### Order creation (continuous simulation)

```
BEGIN;
  INSERT orders          (status='NEW')
  INSERT order_items     (one or more rows)
  UPDATE orders          (set total_amount = SUM of line_total)
COMMIT;
```

### Payment attempt (immediately after order creation)

```
BEGIN;
  INSERT payments        (status='PENDING')
  -- simulate gateway --
  UPDATE payments        (status='SUCCESS' or 'FAILED')
  UPDATE orders          (status='PROCESSING' if SUCCESS)
COMMIT;
```

### Fulfillment (after PROCESSING)

```
UPDATE orders  status='PACKED'     (warehouse packs)
UPDATE orders  status='SHIPPED'    (set shipped_at)
UPDATE orders  status='DELIVERED'  (set delivered_at)
```

Each transition is a single UPDATE in its own transaction.

---

## 8. What This Document Does Not Govern

The following are **implementation details** decided in later milestones and
must not be anticipated here:

- Python class structure, worker concurrency model, sleep intervals
- PostgreSQL index types or partitioning strategies
- Connection pooling, retry back-off, or worker orchestration
- Monitoring thresholds or alerting rules
