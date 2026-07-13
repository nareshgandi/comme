# Lab 06.04 — Storage Compression: TOAST, LZ4, and Honest Scope

**Phase:** Storage Engineering (Partitioning)
**Prerequisites:** Lab 06.01 complete; `orders` is partitioned.
**Estimated time:** 45 min

> **Scope note:** This lab covers what vanilla PostgreSQL offers: TOAST (inline
> compression of wide columns) and the choice between PGLZ and LZ4 compression
> algorithms. Columnar storage, `cstore_fdw`, and Zstandard compression are out
> of scope and deferred to Milestone 13 (Extensions) if pursued at all. Claims
> about compression ratios vary heavily by data; the numbers here are illustrative.

---

## 1. Business Problem

The infrastructure team observes that the `orders` table is larger than
expected for its row count. A DBA reviews the table layout and notices two
sources of uncompressed data:

1. `orders.notes` — a `TEXT` column storing free-form shipping notes, populated
   by `order_generator.py` with Faker-generated sentences. These strings are
   often longer than the TOAST threshold and could benefit from compression.
2. `products.metadata` — a `JSONB` column storing variant attributes. Wide JSONB
   values are TOASTed and compressed by default, but the compression algorithm
   can be tuned.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Check TOAST storage settings for orders columns
SELECT attname, attstorage, atttypid::regtype AS column_type
FROM   pg_attribute
WHERE  attrelid = 'orderflow.orders'::regclass
  AND  attnum > 0
  AND  NOT attisdropped
ORDER  BY attnum;
-- attstorage values:
--   'p' = plain      (never TOASTed, inline only)
--   'e' = external   (always out-of-line, no compression)
--   'm' = main       (compress inline; move out-of-line if needed — the default for text/jsonb)
--   'x' = extended   (try compress; move out-of-line if still oversized — default for text/jsonb)
```

Check actual storage sizes for notes and the products metadata column:

```sql
-- Average and max size of the notes column
SELECT
    COUNT(*)                               AS total_rows,
    COUNT(notes)                           AS rows_with_notes,
    ROUND(AVG(pg_column_size(notes)))      AS avg_notes_bytes,
    MAX(pg_column_size(notes))             AS max_notes_bytes,
    pg_size_pretty(SUM(pg_column_size(notes))) AS total_notes_size
FROM orders;

-- Average JSONB metadata size per product
SELECT
    ROUND(AVG(pg_column_size(metadata))) AS avg_metadata_bytes,
    MAX(pg_column_size(metadata))         AS max_metadata_bytes
FROM products;
```

Check which compression algorithm is currently configured (PostgreSQL 14+):

```sql
SELECT current_setting('default_toast_compression') AS toast_compression;
-- 'pglz' = traditional LZ77-based (default before PG16)
-- 'lz4'  = faster compression/decompression, similar ratio (available PG14+ if compiled in)
```

Confirm LZ4 support in this PostgreSQL build:

```sql
SELECT name, setting FROM pg_settings WHERE name = 'default_toast_compression';
-- If 'lz4' is not a valid option, this instance was not compiled with --with-lz4.
-- lz4 availability: PostgreSQL 14+ with --with-lz4 compilation flag.
```

---

## 3. Measure (Baseline)

Record:
1. Current compression algorithm: `default_toast_compression = ___`
2. Average `notes` column size: ___ bytes
3. Total `orders` relation size: `pg_size_pretty(pg_total_relation_size(...))`
4. `toast_tuple_target` setting (currently default):

```sql
SELECT reloptions FROM pg_class
WHERE  relname = 'orders_2024_12'   -- check the current month's partition
  AND  relnamespace = (SELECT oid FROM pg_namespace WHERE nspname = 'orderflow');
-- NULL means defaults apply; no per-table storage overrides set.
```

---

## 4. Optimize

### 4a — Understand TOAST thresholds

PostgreSQL attempts to inline a row in a heap page (8 KB default). If a row
with its TOAST-eligible columns would exceed `toast_tuple_target` bytes (default
2 kB), PostgreSQL compresses and/or moves out-of-line the columns marked
`'x'` or `'m'` in order of size (largest first) until the inline row fits.

The `toast_tuple_target` storage parameter controls when TOAST triggers:

```sql
-- Raise the threshold to reduce TOAST overhead for orders
-- (useful when most notes are < 4 KB and you prefer fewer TOAST accesses)
ALTER TABLE orders SET (toast_tuple_target = 4096);
-- This is a per-table storage parameter. It takes effect for future INSERTs
-- and UPDATEs; existing rows are not re-compressed.
```

### 4b — Switch compression algorithm (if LZ4 is available)

```sql
-- Check current algorithm
SHOW default_toast_compression;

-- If lz4 is available, switch a column to lz4 compression:
ALTER TABLE products ALTER COLUMN metadata SET COMPRESSION lz4;

-- Verify
SELECT attname, attcompression
FROM   pg_attribute
WHERE  attrelid = 'orderflow.products'::regclass
  AND  attname  = 'metadata';
-- attcompression 'l' = lz4, 'p' = pglz, '' = default
```

**LZ4 vs PGLZ trade-off:**

| | PGLZ | LZ4 |
|---|---|---|
| Compression ratio | Slightly better | Slightly worse |
| Compress speed | Slower | ~3–5× faster |
| Decompress speed | Slower | ~5–10× faster |
| Write-heavy workloads | Adds CPU overhead | Lower CPU overhead |
| Read-heavy workloads | Extra decompression latency | Lower decompression latency |

For the `orders.notes` column in OrderFlow (high insert rate, occasional reads),
LZ4 is the better choice if available.

```sql
-- Switch notes compression on the partitioned table
-- This applies to the parent; partitions inherit the setting
ALTER TABLE orders ALTER COLUMN notes SET COMPRESSION lz4;
```

### 4c — Measure size impact

```sql
-- Compare current vs compressed size for existing notes rows
SELECT
    pg_size_pretty(SUM(pg_column_size(notes)))   AS raw_size,
    pg_size_pretty(pg_relation_size(
        (SELECT oid FROM pg_class c
         JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relname LIKE 'orders_2024%'
           AND n.nspname = 'orderflow'
         LIMIT 1)
    )) AS partition_heap_size
FROM orders;
```

---

## 5. Measure Again

For existing rows: `pg_column_size()` shows the stored (compressed) byte count,
which may already reflect PGLZ compression on wide `notes` values. New rows
inserted after `ALTER TABLE ... SET COMPRESSION lz4` will use LZ4.

The compression ratio depends heavily on the data: Faker-generated English text
typically compresses to 40–60 % of its original size regardless of algorithm.
The real difference is CPU time: LZ4 uses ~3× less CPU per byte compared to
PGLZ for this kind of text data.

---

## 6. Explain

**TOAST (The Oversized-Attribute Storage Technique):** PostgreSQL stores rows
on 8 KB heap pages. A row with large `TEXT` or `JSONB` columns may not fit.
TOAST handles this by: (1) compressing the column in place (if the result is
≤ ~2 KB) or (2) moving it out-of-line to a separate TOAST table. The main heap
stores a pointer; the TOAST table stores the data in chunks.

Each table has exactly one TOAST table (`pg_toast.pg_toast_<oid>`). Reads that
touch a TOASTed column automatically follow the pointer.

**`toast_tuple_target`:** controls the threshold at which PostgreSQL decides to
TOAST. The default is 2048 bytes. Setting it higher means PostgreSQL tries
harder to keep rows in-line (fewer TOAST accesses on reads) at the cost of
potentially more wasted space per heap page. For the `notes` column in OrderFlow
— which is always written once and rarely updated — a higher threshold reduces
TOAST pointer indirection.

**What this lab does NOT cover:**
- Columnar compression (cstore_fdw, Hydra, DuckDB FDW) — OLAP-oriented,
  requires an extension. Deferred to Milestone 13.
- Zstandard (zstd) compression — not available in vanilla PostgreSQL as of PG16.
- Page-level compression (transparent compression at the OS/filesystem level
  via `wal_compression` or filesystem-level zfs/btrfs compression) — not a
  PostgreSQL feature per se.

---

## 7. Cleanup / Reset Note

`ALTER TABLE orders SET (toast_tuple_target = 4096)` **persists** — it is a
reasonable production tuning for a write-heavy table with moderate TEXT columns.

`ALTER TABLE orders ALTER COLUMN notes SET COMPRESSION lz4` **persists** if LZ4
was available. This only affects future rows; existing rows are not re-compressed
unless UPDATEd.

`ALTER TABLE products ALTER COLUMN metadata SET COMPRESSION lz4` **persists**.

To revert all compression changes:

```sql
ALTER TABLE orders   ALTER COLUMN notes     SET COMPRESSION pglz;
ALTER TABLE products ALTER COLUMN metadata  SET COMPRESSION pglz;
ALTER TABLE orders RESET (toast_tuple_target);
```

---

## Further Reading

- [PostgreSQL docs — TOAST](https://www.postgresql.org/docs/current/storage-toast.html)
- [PostgreSQL docs — CREATE TABLE storage parameters](https://www.postgresql.org/docs/current/sql-createtable.html#SQL-CREATETABLE-STORAGE-PARAMETERS)
- [PostgreSQL docs — LZ4 compression (PG14+)](https://www.postgresql.org/docs/current/runtime-config-client.html#GUC-DEFAULT-TOAST-COMPRESSION)
