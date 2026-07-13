# Lab 03 — The Python Workload: Factories & Workers

**Type:** Foundation write-up (no Business Problem / Optimize shape)
**Prerequisite:** Lab 02 complete; workers running.
**Time:** 30 minutes reading; 20 minutes code walkthrough.

This write-up explains *why* the data looks the way it does — the Python layer
that generates it. Understanding the workers lets you interpret what you see in
later labs: why `order_items` has no dead tuples, why `employees` is bloated,
why the payments table has a specific ratio of SUCCESS to FAILED rows.

No code changes. Read and understand.

---

## The Core Principle: All Data Through Python

One of the founding design decisions (Principle 3 in `architecture.md`) is:

> **Application-only data entry.** All rows enter the database through Python
> factories and workers. No `INSERT` scripts, ever.

This is not an aesthetic choice. It is what makes the dataset realistic.
A hand-written SQL seed script produces clean, uniform data — every row looks
like it was inserted by a `for i in range(100000)` loop. Factory-generated
data with seeded randomness produces the statistical distributions that make
real query optimization interesting: non-uniform cardinality, realistic
timestamp gaps, rare NULL values in the right columns, duplicate product names
in different categories.

---

## Milestone 2: The Factories

Five factories live in `python/factories/`. Each takes a seeded `random.Random`
instance so that test output is deterministic.

| Factory | Produces | Key behaviors |
|---------|----------|---------------|
| `EmployeeFactory` | `Employee` dataclass | Role + department mapping; salary drawn from per-role ranges |
| `CustomerFactory` | `Customer` dataclass | US-weighted country distribution; loyalty tier with realistic skew (60 % bronze) |
| `ProductFactory` | `Product` dataclass | Category-prefixed SKUs (ELEC-0001, CLTH-0002); JSONB metadata keys vary by category |
| `OrderFactory` | `(Order, [OrderItem])` pairs | 1–6 items per order; optional discount; optional employee assignment; optional notes |
| `PaymentFactory` | `Payment` dataclass | Method weighted by real-world share (45 % credit card); failure reasons for gateway simulation |

All factory configuration — roles, salary ranges, loyalty tier weights, country
distribution, discount probabilities — comes from `python/config/config.yaml`
via `DEFAULT_CONFIG`. Tests call factories without a config file; workers call
`load_config()` at startup and pass the result in.

### What the factories do NOT do

Factories never touch the database. They produce Python dataclasses with
`None` primary keys. Workers are responsible for `INSERT … RETURNING` to get
the database-assigned IDs back. This separation means factory tests run
without a PostgreSQL connection.

---

## Milestone 3: The Workers

Five scripts live in `python/workers/`. One is a one-shot loader; four are
continuous processes.

### `history_loader.py` — one-shot

Populates ~500 000 historical orders with realistic backdated timestamps. Uses
PostgreSQL's `COPY` protocol for bulk loading (10–50× faster than `executemany`
for reference tables and `order_items`/`payments`). Uses `INSERT … RETURNING`
for orders, because the generated `order_id` is needed immediately to write
`order_items`.

After `history_loader.py` completes, the database looks like a system that has
been running for a year: a mix of DELIVERED, SHIPPED, RETURNED, REFUNDED
orders, historical payment rows, and an employee roster.

`history_loader.py --force` truncates all tables and reloads from scratch.
Bootstrap never calls `--force` — if data already exists, it skips the loader.

### `order_generator.py` — continuous

Creates new `NEW`-status orders at approximately 2.5 per second (10 orders per
batch, 4-second sleep). Each iteration:

1. Calls `OrderFactory.create_orders(batch_size)` to produce order + item pairs.
2. INSERTs each order, gets the `order_id` back.
3. COPYs the order_items batch in one round-trip.
4. Commits.

This is the primary source of WAL volume. 2.5 orders/sec × ~3 items/order ×
typical row sizes ≈ 200 MB of WAL per day. This rate makes replication lag
measurable in Lab 07 and makes `pg_stat_statements` interesting in Lab 11.

### `payment_processor.py` — continuous

Processes `NEW` orders that have not yet been paid. Uses `FOR UPDATE SKIP LOCKED`
to safely pick up orders without competing with other worker instances:

```sql
SELECT order_id FROM orders
WHERE status = 'NEW'
ORDER BY created_at
LIMIT 50
FOR UPDATE SKIP LOCKED
```

For each order: simulate gateway result, insert payment row, update order status,
commit. This produces the payment mix: ~92 % SUCCESS, ~8 % FAILED (configurable).
Orders that exhaust `max_payment_retries` are abandoned in NEW.

### `order_processor.py` — continuous

Advances orders through the fulfillment lifecycle:
`PROCESSING → PACKED → SHIPPED → DELIVERED → (RETURNED → REFUNDED)`

Each transition is a separate `UPDATE` in its own transaction. Minimum-age
checks prevent unrealistically fast transitions (e.g. a PROCESSING order needs
at least 30 seconds before becoming PACKED). This produces a realistic age
distribution across statuses at any point in time.

The return logic: each cycle, DELIVERED orders older than a configurable
minimum are sampled; a fraction (`return_probability`, default 5 %) become
RETURNED, then immediately REFUNDED.

### `employee_updates.py` — continuous

Randomly mutates a small sample of employees each cycle (default 20 employees
per 10-second cycle):

- With `deactivate_prob` (2 %) probability: set `is_active = FALSE`
- With `dept_change_prob` (10 %) probability: reassign department
- Always: adjust salary by a random fraction (5–15 %)

**Why this worker exists:** `UPDATE` on a heap table creates dead tuples. The
`employees` table accumulates dead tuples continuously because of these random
salary and department updates. This is the data source for the autovacuum
tuning and bloat analysis lab (Lab 11). Without `employee_updates.py`, the
VACUUM lab would need an artificial setup to produce dead tuples — with it,
the bloat is already there when you arrive.

---

## Milestone 4: Configuration

All tunable parameters live in `python/config/config.yaml`. The loader
(`python/config/loader.py`) reads it at worker startup and produces a typed
`Config` dataclass — no dict key lookups at runtime, no YAML in business logic.

`DEFAULT_CONFIG` in `loader.py` mirrors all the values from `config.yaml.example`.
This is what factories use in tests (no YAML file required on disk).

The database password is the only parameter that never appears in YAML. It
comes exclusively from `ORDERFLOW_DB_PASSWORD` env var. `load_config()` raises
`EnvironmentError` immediately if it is unset.

---

## Milestone 5: Bootstrap

`bootstrap.py` ties everything together in six ordered steps:
preflight → DB provisioning → schema → historical load → worker launch → summary.

Re-running `bootstrap.py` on an already-running system is safe: each step
detects existing state and skips. `--status` and `--stop` provide process
control without restarting the full setup sequence.

---

## The Connection Contract

Every worker opens exactly one psycopg v3 connection at startup with:

- `application_name` = the worker's script name (without `.py`)
- `options="-c search_path=orderflow,public"`
- `autocommit=False` (explicit `conn.commit()` / `conn.rollback()`)

This is not accidental. `application_name` is what makes `pg_stat_activity`
readable in Lab 11 — each worker is identifiable at a glance. The explicit
`search_path` means every query uses unqualified table names, matching what
you see in `EXPLAIN` output.

---

## What This Enables

| Later lab | Specifically depends on |
|-----------|------------------------|
| Lab 04 (PostgreSQL Core) | `FOR UPDATE SKIP LOCKED` in `payment_processor.py` — perfect concurrency demonstration |
| Lab 05 (Indexes) | Live writes from all 4 workers producing realistic query plans; `payments` needs its partial unique index |
| Lab 07 (Replication) | `order_generator.py` WAL volume makes replication lag measurable; `application_name` on connections shows traffic on standby |
| Lab 09 (Security) | `employee_updates.py` writes to `salary` — the column being encrypted and RLS-protected |
| Lab 10 (Extensions) | `payment_processor.py` abandoned orders are the cleanup target for the `pg_cron` job |
| **Lab 11 (Monitoring)** | **`employee_updates.py` is the sole source of dead tuples on `employees`.** Without it running, the VACUUM / bloat lab has nothing to observe. Verify it is alive before starting Lab 11. |
