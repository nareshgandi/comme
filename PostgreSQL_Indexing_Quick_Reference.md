# PostgreSQL Indexing Quick Reference

# Demo Tables

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

CREATE TABLE location (
    location_id BIGSERIAL PRIMARY KEY,
    city TEXT,
    area POLYGON,
    created_at TIMESTAMP
);
```

---

# Index Summary

| Index | Use Case | Example Query | Create Index | Expected Change |
|-------|----------|---------------|--------------|-----------------|
| B-Tree | Equality, Range, ORDER BY | `department_id=10` | `CREATE INDEX idx_dept ON employees(department_id);` | Seq Scan → Index Scan |
| Multi-column | Filter on multiple columns | `department_id=10 AND salary>70000` | `CREATE INDEX idx_dept_sal ON employees(department_id,salary);` | Single index satisfies both predicates |
| Covering (INCLUDE) | Projection queries | `SELECT employee_name,salary WHERE department_id=10` | `CREATE INDEX idx_cover ON employees(department_id) INCLUDE(employee_name,salary);` | Index Only Scan possible |
| Functional | Functions in WHERE | `lower(email)=...` | `CREATE INDEX idx_lower_email ON employees(lower(email));` | Function becomes indexable |
| Partial | Frequently filtered subset | `status='ACTIVE'` | `CREATE INDEX idx_active ON t(status) WHERE status='ACTIVE';` | Smaller & faster index |
| GIN | Arrays, JSONB | `skills @> ARRAY['PostgreSQL']` | `CREATE INDEX idx_skills ON employees USING GIN(skills);` | Seq Scan → Bitmap Index Scan |
| GIN(JSONB) | JSON containment | `profile @> '{"city":"Delhi"}'` | `CREATE INDEX idx_profile ON employees USING GIN(profile);` | Bitmap Index Scan |
| pg_trgm | LIKE / ILIKE | `employee_name ILIKE '%john%'` | `CREATE EXTENSION pg_trgm; CREATE INDEX idx_trgm ON employees USING GIN(employee_name gin_trgm_ops);` | Seq Scan → Bitmap Index Scan |
| GiST | Geometry, spatial | `office_location <@ box(...)` | `CREATE INDEX idx_gist ON employees USING GiST(office_location);` | Bitmap Index Scan |
| SP-GiST | Sparse spatial, tries | Large sparse point datasets | `CREATE INDEX idx_spgist ON employees USING SPGIST(office_location);` | Faster for sparse distributions |
| BRIN | Huge append-only tables | Date range | `CREATE INDEX idx_brin ON orders USING BRIN(order_date);` | Reads only relevant page ranges |
| HypoPG | Test hypothetical index | EXPLAIN only | `SELECT * FROM hypopg_create_index('CREATE INDEX ...');` | Planner changes without creating index |

---

# Before / After Examples

## B-Tree

Before

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE department_id=10;
```

```sql
CREATE INDEX idx_dept
ON employees(department_id);
```

After

Expected

- Seq Scan → Index Scan
- Lower execution time
- Fewer buffers

---

## Multi-column

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE department_id=10
AND salary>70000;
```

```sql
CREATE INDEX idx_dept_salary
ON employees(department_id,salary);
```

Expected

- Single composite index lookup
- Better than filtering salary after department lookup

---

## Covering Index

```sql
EXPLAIN ANALYZE
SELECT employee_name,salary
FROM employees
WHERE department_id=10;
```

```sql
CREATE INDEX idx_cover
ON employees(department_id)
INCLUDE(employee_name,salary);
```

Expected

- Index Only Scan
- Heap access avoided (visibility map permitting)

---

## Functional Index

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE lower(email)='abc@gmail.com';
```

```sql
CREATE INDEX idx_lower_email
ON employees(lower(email));
```

---

## GIN (Array)

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE skills @> ARRAY['PostgreSQL'];
```

```sql
CREATE INDEX idx_skills
ON employees
USING GIN(skills);
```

Observed:
- Parallel Seq Scan → Bitmap Heap Scan + Bitmap Index Scan. (Matches your demo.)

---

## GIN (JSONB)

```sql
EXPLAIN ANALYZE
SELECT *
FROM employees
WHERE profile @> '{"city":"Delhi"}';
```

```sql
CREATE INDEX idx_profile
ON employees
USING GIN(profile);
```

Observed:
- Seq Scan → Bitmap Heap Scan.

---

## pg_trgm

```sql
CREATE EXTENSION pg_trgm;

CREATE INDEX idx_trgm
ON employees
USING GIN(employee_name gin_trgm_ops);
```

```sql
SELECT *
FROM employees
WHERE employee_name ILIKE '%john%';
```

Observed:
- Analyze required after index creation.
- Parallel Seq Scan → Bitmap Index Scan.

---

## GiST

```sql
CREATE INDEX idx_gist
ON employees
USING GiST(office_location);
```

```sql
SELECT *
FROM employees
WHERE office_location <@ box(point(10,10),point(40,40));
```

Observed:
- Seq Scan → Bitmap Heap Scan.

---

## BRIN

```sql
CREATE INDEX idx_orders_date
ON orders
USING BRIN(order_date);
```

Use only for:
- Huge tables
- Append-only data
- Naturally ordered columns

---

## HypoPG

```sql
CREATE EXTENSION hypopg;

SELECT *
FROM hypopg_create_index(
'CREATE INDEX ON employee(department_id,salary)'
);

EXPLAIN
SELECT *
FROM employee
WHERE department_id=10
AND salary>70000;
```

Planner uses hypothetical index without actually creating it.

---

# Comparison

## Multi-column vs Covering

| Feature | Multi-column | Covering |
|----------|--------------|-----------|
| Filters | Multiple | Usually one |
| Projection | No | Yes (INCLUDE) |
| Heap Access | Usually Yes | Often No |
| Index Only Scan | Rare | Common |

---

## GiST vs SP-GiST

| GiST | SP-GiST |
|------|---------|
| Balanced tree | Space partitioned |
| General purpose | Sparse datasets |
| PostGIS | Points, IP, text tries |

---

## GIN vs pg_trgm

| GIN(JSON/Array) | pg_trgm |
|-----------------|----------|
| Arrays | LIKE |
| JSONB | ILIKE |
| Full text | Fuzzy search |

---

## B-Tree vs BRIN

| B-Tree | BRIN |
|--------|------|
| Precise lookup | Block summaries |
| Larger index | Tiny index |
| Random access | Append-only |
| OLTP | Data warehouse |

---

# Extension Summary

| Extension | Purpose |
|-----------|---------|
| pg_trgm | LIKE / similarity |
| btree_gin | B-tree operator classes for GIN |
| btree_gist | B-tree operator classes for GiST |
| hypopg | Hypothetical indexes |

---

# Best Practices

- Analyze after bulk load.
- Benchmark before/after using EXPLAIN ANALYZE.
- Check Buffers and Execution Time.
- Prefer B-tree unless workload requires otherwise.
- Use BRIN only on naturally ordered large tables.
- Test with HypoPG before building expensive indexes.
