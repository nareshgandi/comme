# PostgreSQL Partitioning Quick Reference

# Demo Table

```sql
CREATE TABLE employees (
    emp_id BIGSERIAL PRIMARY KEY,
    employee_name TEXT,
    email TEXT,
    department_id INT,
    salary NUMERIC(10,2),
    hire_date DATE,
    skills TEXT[],
    profile JSONB,
    office_location POINT,
    description TEXT
);
```

---

# Why Partition?

When a table grows to hundreds of millions of rows:

- Faster scans (Partition Pruning)
- Easier archival
- Faster maintenance
- Smaller indexes
- Better vacuum performance
- Drop old data instantly

---

# Partition Types

| Type | Best For | Partition Key | Example |
|------|----------|---------------|---------|
| RANGE | Time-series | hire_date | Monthly/Yearly employees |
| LIST | Fixed categories | department_id | HR, Finance, IT |
| HASH | Even distribution | emp_id | OLTP workload |
| DEFAULT | Unknown values | Any | Catch unmatched rows |

---

# RANGE Partition

## Before

```sql
SELECT *
FROM employees
WHERE hire_date BETWEEN '2024-01-01' AND '2024-03-31';
```

Large table scans many pages.

## Create

```sql
CREATE TABLE employees_range
(
LIKE employees INCLUDING ALL
)
PARTITION BY RANGE(hire_date);

CREATE TABLE employees_2023
PARTITION OF employees_range
FOR VALUES FROM ('2023-01-01') TO ('2024-01-01');

CREATE TABLE employees_2024
PARTITION OF employees_range
FOR VALUES FROM ('2024-01-01') TO ('2025-01-01');

INSERT INTO employees_range
SELECT * FROM employees;
```

Expected

- Partition Pruning
- Only matching partition scanned

---

# LIST Partition

## Before

```sql
SELECT *
FROM employees
WHERE department_id=10;
```

## Create

```sql
CREATE TABLE employees_list
(
LIKE employees INCLUDING ALL
)
PARTITION BY LIST(department_id);

CREATE TABLE employees_dept10
PARTITION OF employees_list
FOR VALUES IN (10);

CREATE TABLE employees_dept20
PARTITION OF employees_list
FOR VALUES IN (20);

CREATE TABLE employees_other
PARTITION OF employees_list
DEFAULT;

INSERT INTO employees_list
SELECT * FROM employees;
```

Expected

- Only relevant department partition accessed.

---

# HASH Partition

Useful when queries don't have a natural date/category but you want balanced writes.

```sql
CREATE TABLE employees_hash
(
LIKE employees INCLUDING ALL
)
PARTITION BY HASH(emp_id);

CREATE TABLE employees_h0 PARTITION OF employees_hash
FOR VALUES WITH (MODULUS 4,REMAINDER 0);

CREATE TABLE employees_h1 PARTITION OF employees_hash
FOR VALUES WITH (MODULUS 4,REMAINDER 1);

CREATE TABLE employees_h2 PARTITION OF employees_hash
FOR VALUES WITH (MODULUS 4,REMAINDER 2);

CREATE TABLE employees_h3 PARTITION OF employees_hash
FOR VALUES WITH (MODULUS 4,REMAINDER 3);
```

Expected

- Even distribution
- Better concurrent write scalability

---

# DEFAULT Partition

```sql
CREATE TABLE employees_default
PARTITION OF employees_list
DEFAULT;
```

Stores rows that don't match existing LIST values.

---

# ATTACH Partition

```sql
ALTER TABLE employees_list
ATTACH PARTITION employees_dept30
FOR VALUES IN (30);
```

If rows already exist in DEFAULT for department 30, move them first.

---

# DETACH Partition

```sql
ALTER TABLE employees_list
DETACH PARTITION employees_dept30;
```

Useful for archival or maintenance.

---

# Local Index

```sql
CREATE INDEX
ON employees_range(hire_date);
```

PostgreSQL automatically creates indexes on partitions.

---

# Explain Comparison

## Before

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE hire_date BETWEEN '2024-01-01'
AND '2024-03-31';
```

Expected

- Seq Scan

## After

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees_range
WHERE hire_date BETWEEN '2024-01-01'
AND '2024-03-31';
```

Expected

- Partition Pruning
- Scan only employees_2024

---

# Online Migration Strategy

1. Create partitioned parent
2. Create partitions
3. Create indexes
4. Dual-write trigger
5. Backfill historical data
6. Validate counts
7. Rename tables
8. Drop old table later

Downtime: Metadata rename only (milliseconds).

---

# Comparison

## RANGE vs LIST vs HASH

| Feature | RANGE | LIST | HASH |
|---------|-------|------|------|
| Time-based | ✅ | ❌ | ❌ |
| Categories | ❌ | ✅ | ❌ |
| Even distribution | ❌ | ❌ | ✅ |
| Archiving | Excellent | Good | Poor |
| Reporting | Excellent | Good | Average |
| OLTP writes | Good | Good | Excellent |

---

## Partitioning vs Indexing

| Indexing | Partitioning |
|----------|--------------|
| Speeds row lookup | Reduces data scanned |
| Single table | Multiple child tables |
| Good for millions | Good for hundreds of millions/billions |
| Maintenance grows | Maintenance isolated per partition |

Use indexes first.
Partition only when table size, maintenance, or lifecycle justifies it.

---

# Best Practices

- Choose stable partition keys.
- Prefer RANGE for time-series.
- Always create DEFAULT partition for LIST.
- Don't over-partition.
- Keep partition count manageable.
- Create indexes only when required.
- Use EXPLAIN ANALYZE to verify Partition Pruning.
