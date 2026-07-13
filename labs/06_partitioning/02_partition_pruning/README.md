# Lab 06.02 — Partition Pruning: When the Planner Skips Partitions (and When It Doesn't)

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Lab 06.01 complete; `orders` is now RANGE-partitioned; workers running.
**Estimated time:** 45 min

---

## 1. Business Problem

A senior developer hears that orders is now partitioned and assumes every query
on it is now automatically fast. They write a month-over-month revenue
comparison that should only read two partitions — but `EXPLAIN ANALYZE` shows
all partitions are scanned. The DBA needs to explain *when* pruning fires and
*when* it silently doesn't, and how to rewrite queries to re-enable it.

---

## 2. Observe

Count how many partitions exist:

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- List all partitions
SELECT c.relname              AS partition,
       pg_size_pretty(pg_relation_size(c.oid)) AS size
FROM   pg_inherits  i
JOIN   pg_class     p ON p.oid = i.inhparent
JOIN   pg_class     c ON c.oid = i.inhchild
JOIN   pg_namespace n ON n.oid = p.relnamespace
WHERE  p.relname = 'orders' AND n.nspname = 'orderflow'
ORDER  BY c.relname;
```

Establish the pruning baseline with a constant-predicate query:

```sql
-- CASE A: constant date predicate — planner sees the literal, prunes at plan time
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= '2024-10-01'
  AND  created_at <  '2024-11-01';
-- Expected: Append node lists ONLY orders_2024_10
```

---

## 3. Measure (Baseline)

**Baseline — pruned case:** Append node shows exactly 1 partition. All other
partitions are absent from the plan. Record the number of partitions listed
under the Append node: **1**.

---

## 4. Optimize — Understanding and Fixing Each Pruning Failure

### Case B: Function wrapping the partition key — pruning DEFEATED

```sql
-- DATE_TRUNC wraps created_at; PostgreSQL cannot infer which partition
-- contains the result without evaluating the function for every partition.
EXPLAIN
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  DATE_TRUNC('month', created_at) = '2024-10-01';
```

The Append node now lists ALL partitions. The planner cannot invert
`DATE_TRUNC('month', created_at) = '2024-10-01'` into a simple range on
`created_at` — so it scans everything.

**Fix:** Write the predicate on the raw column:

```sql
EXPLAIN
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= '2024-10-01'
  AND  created_at <  '2024-11-01';
-- Pruning restored: 1 partition.
```

### Case C: EXTRACT / date functions — sometimes defeated

```sql
-- EXTRACT returns a float, not a timestamptz — planner cannot prune
EXPLAIN
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  EXTRACT(year FROM created_at) = 2024;
-- All partitions scanned.
```

**Fix:** Replace with a range predicate:

```sql
EXPLAIN
SELECT COUNT(*), SUM(total_amount)
FROM   orders
WHERE  created_at >= '2024-01-01'
  AND  created_at <  '2025-01-01';
-- Only 2024 partitions appear in the Append node.
```

### Case D: Runtime parameter (PREPARE) — dynamic pruning

When the predicate value is a query parameter (not a literal), the planner
cannot prune at planning time. PostgreSQL performs **runtime pruning** (PG12+)
instead — partitions are eliminated during execution when the parameter value
is known.

```sql
PREPARE revenue_by_range(timestamptz, timestamptz) AS
    SELECT COUNT(*), SUM(total_amount)
    FROM   orders
    WHERE  created_at >= $1 AND created_at < $2;

EXPLAIN (ANALYZE, BUFFERS)
EXECUTE revenue_by_range('2024-10-01', '2024-11-01');
-- Look for "Subplans Removed" in the Append node — runtime pruning in action.
```

### Case E: Pruning vs. the DEFAULT partition

The DEFAULT partition (`orders_default`) is always included in any scan that
cannot exclude it. A query with `WHERE created_at >= '2024-10-01'` prunes all
fixed-range partitions that don't overlap, but the DEFAULT partition always
appears in the plan — it could theoretically contain any row.

```sql
EXPLAIN
SELECT COUNT(*) FROM orders WHERE created_at >= '2024-10-01';
-- Note: orders_default appears in the Append node even if it is empty.
-- This is expected and unavoidable — DEFAULT must be checked.
```

### Toggle pruning to confirm its effect:

```sql
-- Disable partition pruning (never do this in production — diagnostic only)
SET enable_partition_pruning = off;
EXPLAIN SELECT COUNT(*) FROM orders WHERE created_at >= '2024-10-01' AND created_at < '2024-11-01';
-- All partitions appear in Append node.

SET enable_partition_pruning = on;
EXPLAIN SELECT COUNT(*) FROM orders WHERE created_at >= '2024-10-01' AND created_at < '2024-11-01';
-- Only the matching partition appears.
```

---

## 5. Measure Again

| Case | Partitions scanned | Pruning? |
|------|--------------------|----------|
| A: constant range predicate | 1 (+ DEFAULT) | ✓ Planning-time |
| B: DATE_TRUNC on column | All | ✗ Defeated |
| C: EXTRACT on column | All | ✗ Defeated |
| D: PREPARE with parameters | 1 (+ DEFAULT) | ✓ Runtime |
| E: any predicate + DEFAULT | 1 + DEFAULT | Partial |

---

## 6. Explain

**Planning-time pruning** happens when the predicate contains a literal value
that the planner can compare against partition bounds during query planning. The
planner produces an `Append` node whose child list omits any partition with
non-overlapping bounds. The pruned partitions are not touched at all.

**Runtime pruning** (PG12+) handles parameterized queries. The partition
elimination happens at the start of execution, when the parameter values are
bound. The Explain output shows `"Subplans Removed: N"` on the Append node.

**Why functions defeat pruning:** PostgreSQL can only prune a partition if it
can statically determine that the partition key value cannot satisfy the
predicate. For a predicate like `DATE_TRUNC('month', created_at) = '2024-10-01'`,
the planner cannot invert the function to derive bounds on `created_at` without
evaluating it for every partition. The planner opts to scan all rather than
risk missing rows.

**The rule:** write predicates directly on the partition key column, in a form
the planner can compare against literal partition boundaries. A range predicate
(`created_at >= X AND created_at < Y`) is always the safe form.

---

## 7. Cleanup / Reset Note

No schema changes in this lab. All `SET` changes (`enable_partition_pruning`)
are session-local and reset automatically on disconnect.

---

## Further Reading

- [PostgreSQL docs — Partition Pruning](https://www.postgresql.org/docs/current/ddl-partitioning.html#DDL-PARTITION-PRUNING)
- [PostgreSQL docs — EXPLAIN](https://www.postgresql.org/docs/current/sql-explain.html)
