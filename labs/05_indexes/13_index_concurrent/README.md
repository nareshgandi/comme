# Lab 05.13 — CREATE INDEX CONCURRENTLY: Building Indexes Without Blocking Writers

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.12 complete; all four workers running (critical — this lab requires live write traffic).
**Estimated time:** 1 hr

> **This lab only makes sense with live workers.** The entire point is to
> observe what happens to concurrent writers during a regular vs. concurrent
> index build. Run `python bootstrap.py --status` before starting and confirm
> all four workers show RUNNING.

---

## 1. Business Problem

The on-call DBA needs to add a new index to `orders.warehouse_id` — the ops
team discovered that the order-processor worker's fulfillment queries are doing
a Seq Scan when filtering by warehouse. Adding an index during business hours
requires a decision: `CREATE INDEX` acquires a `ShareLock` on the table that
**blocks all concurrent writes** for the entire duration of the build (minutes
on a 500 000-row table). `CREATE INDEX CONCURRENTLY` avoids that lock at the
cost of a longer build time and two passes over the table. In a live system
where `order_generator.py` is inserting 2.5 orders/second, the choice matters.

---

## 2. Observe

First verify the workers are actively writing:

```bash
psql -U postgres -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Confirm live writes are happening
SELECT pid, application_name, state, LEFT(query, 60) AS query_snippet
FROM   pg_stat_activity
WHERE  datname = 'orderflow'
  AND  application_name IN (
    'order_generator', 'payment_processor',
    'order_processor', 'employee_updates'
  );
```

Now observe what happens during a **regular** `CREATE INDEX` — watch `pg_stat_activity`
in a second terminal while the index builds:

```sql
-- TERMINAL 1: Watch for blocked queries while index builds
-- Run this continuously (e.g. with \watch 1) in a second psql session:
SELECT pid, wait_event_type, wait_event, state, application_name,
       LEFT(query, 80) AS query
FROM   pg_stat_activity
WHERE  datname = 'orderflow'
  AND  wait_event IS NOT NULL
ORDER BY wait_event_type;
```

```sql
-- TERMINAL 2: Build a regular (blocking) index
-- Note: on a live system, this WILL block the workers briefly.
-- Keep the observation window (60 seconds) short to minimize disruption.
\timing on
CREATE INDEX idx_orders_warehouse_id_blocking
    ON orders (warehouse_id);
\timing off
-- Record the build time
```

---

## 3. Measure (Baseline)

**Baseline observations:**
1. During the blocking `CREATE INDEX`, `pg_stat_activity` shows workers in
   `wait_event = 'relation'` (waiting for the ShareLock to release). They cannot
   write new rows until the index build finishes.
2. Build time for a blocking index on `orders.warehouse_id` (~500 000 rows):
   **Record your actual timing** — typically 3–8 seconds on a standard VM.

```sql
-- Drop the blocking index — we will rebuild it correctly below
DROP INDEX idx_orders_warehouse_id_blocking;
```

**Baseline metric:** Index build time = X seconds; workers blocked for X seconds

---

## 4. Optimize

```sql
-- Build the same index CONCURRENTLY — no ShareLock; workers continue unimpeded
\timing on
CREATE INDEX CONCURRENTLY idx_orders_warehouse_id
    ON orders (warehouse_id);
\timing off
-- Record the build time (expect ~2–3× longer than the blocking version)
```

While the concurrent build runs, check the lock in `pg_stat_activity`:

```sql
-- In a second terminal, watch lock behavior during CONCURRENTLY build
SELECT pid, wait_event_type, wait_event, state, application_name
FROM   pg_stat_activity
WHERE  datname = 'orderflow';
-- Workers should show state='active', NOT wait_event='relation'
```

Also verify the index built correctly:

```sql
SELECT indexname, indisvalid
FROM   pg_indexes pi
JOIN   pg_class c ON c.relname = pi.indexname
JOIN   pg_index i ON i.indexrelid = c.oid
WHERE  pi.tablename = 'orders'
  AND  pi.indexname = 'idx_orders_warehouse_id';
-- indisvalid must be TRUE before the index can be used by queries
```

---

## 5. Measure Again

**After `CREATE INDEX CONCURRENTLY`:**
- Workers show no `wait_event = 'relation'` during the build — inserts proceed
  at the normal ~2.5 orders/second rate
- Build time: ~2–3× longer than blocking build (extra pass over the table +
  waiting for any in-flight transactions to drain between passes)
- Final index is identical in structure and query performance to the blocking build

**Delta:** Zero write blocking vs. full write block for X seconds — at 2.5
orders/sec, the blocking build would have prevented ~15–20 inserts on a typical
VM. On a busier production system (1 000 inserts/sec), the same blocking build
blocks 3 000–8 000 writes.

---

## 6. Explain

`CREATE INDEX` acquires a `ShareLock` (lock mode 5) that is compatible with
readers but **incompatible with writes**. All `INSERT`, `UPDATE`, and `DELETE`
statements wait until the lock is released. On a large table, the build can
take minutes — minutes of write outage on a live system.

`CREATE INDEX CONCURRENTLY` avoids the ShareLock by splitting the build into
three phases:

1. **Phase 1:** Acquire a `ShareUpdateExclusiveLock` (compatible with writes).
   Scan the heap once to build an initial index snapshot. New writes during
   this phase are NOT in the index yet.
2. **Phase 2:** Wait for all transactions that started before Phase 1 to
   finish (they might hold references to old heap versions). Scan the heap
   again to catch any rows added in Phase 1 that the index missed.
3. **Phase 3:** Mark the index as valid (`indisvalid = TRUE`). The index is
   now visible to the query planner.

During all three phases, `INSERT`/`UPDATE`/`DELETE` continue without waiting.
The trade-off: two full heap scans instead of one, plus waiting for in-flight
transactions to drain between phases — roughly 2–3× longer total build time.

**Important edge cases:**
- If `CREATE INDEX CONCURRENTLY` is interrupted (e.g. system crash, Ctrl-C),
  the index is left in an **invalid** state (`indisvalid = FALSE`). An invalid
  index is not used by the planner but still consumes write overhead. It must
  be explicitly dropped with `DROP INDEX CONCURRENTLY`.
- A concurrent build cannot run inside a transaction block (`BEGIN … CREATE
  INDEX CONCURRENTLY … COMMIT` fails).

---

## 7. Cleanup / Reset Note

`idx_orders_warehouse_id` **persists** — the order-processor worker does filter
by `warehouse_id` for fulfillment queries, and this index is the correct fix
for the Seq Scan that motivated this lab.

Verify the final index is valid:

```sql
SELECT indexname,
       pg_size_pretty(pg_relation_size(indexname::regclass)) AS size,
       indisvalid
FROM pg_indexes pi
JOIN pg_class c ON c.relname = pi.indexname
JOIN pg_index i ON i.indexrelid = c.oid
WHERE pi.tablename = 'orders'
ORDER BY indexname;
```

---

## Further Reading

- [PostgreSQL docs — CREATE INDEX](https://www.postgresql.org/docs/current/sql-createindex.html)
- [PostgreSQL docs — Locking and Indexes](https://www.postgresql.org/docs/current/explicit-locking.html#LOCKING-TABLES)
