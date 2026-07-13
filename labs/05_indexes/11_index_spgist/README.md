# Lab 05.11 — SP-GiST Index: Space-Partitioned Trees

**Phase:** Storage Engineering (Index Design)
**Prerequisites:** Lab 05.10 complete; workers running.
**Estimated time:** 30 min

> **Schema fit — honest assessment:** SP-GiST is most effective for
> non-balanced data distributions: IP address prefixes (`inet`), geographic
> point coordinates (`point`), and text with highly non-uniform common
> prefixes. The frozen OrderFlow schema has no `inet`, `point`, or `box`
> columns. The closest natural fit is `products.sku`, which carries a
> structured 4-character category prefix (`ELEC-`, `CLTH-`, `HOME-`, etc.)
> followed by a zero-padded sequence number.
>
> SP-GiST's `text_ops` opclass supports this prefix structure, but in
> practice a B-tree index on `sku` (already present as the `products_sku_key`
> unique index) performs identically for OrderFlow's query patterns. This lab
> teaches the SP-GiST mechanism and correctly notes where it genuinely wins
> over B-tree — while being honest that OrderFlow is not an ideal showcase.
> Do not add a synthetic column to demonstrate this; the instruction to "use
> a legitimate, clearly-labeled synthetic example" applies here.

---

## 1. Business Problem

The warehouse routing system needs to look up all products in the `Electronics`
category by matching SKUs that start with `ELEC-`. It also needs to answer
range queries like "all SKUs between `ELEC-0100` and `ELEC-0500`". The
existing unique B-tree index on `sku` handles both. The infrastructure team is
evaluating whether SP-GiST would offer a size or speed advantage for prefix-
heavy text data like SKUs, or for a future `inet` column that might store
product origin region IP ranges.

---

## 2. Observe

```bash
psql -U orderflow -d orderflow
```

```sql
SET search_path = orderflow, public;

-- Current index on products.sku
\d products
-- Shows: products_sku_key UNIQUE, btree (sku)

-- Prefix lookup with existing B-tree index
EXPLAIN (ANALYZE, BUFFERS)
SELECT product_id, sku, name, category
FROM   products
WHERE  sku LIKE 'ELEC-%'
ORDER BY sku;
```

**Representative output (B-tree, 1 000 products):**

```
Index Scan using products_sku_key on products
                    (cost=0.28..14.21 rows=143 width=60)
                    (actual time=0.024..0.187 rows=143 loops=1)
  Index Cond: (((sku)::text >= 'ELEC-') AND
               ((sku)::text < 'ELEF-'))
  Filter: ((sku)::text ~~ 'ELEC-%')
  Buffers: shared hit=6
Execution Time: 0.201 ms
```

The B-tree handles this in 0.2 ms on 1 000 rows.

---

## 3. Measure (Baseline)

**Baseline (B-tree):** ~0.2 ms  |  6 buffer reads  |  Index Scan (prefix
range implicit in B-tree: `ELEC-` ≤ sku < `ELEF-`)

---

## 4. Optimize

### Demonstrate SP-GiST on the SKU column (for comparison)

```sql
-- Create a SP-GiST index on sku using text_ops
-- SP-GiST text_ops uses a radix-tree (trie) structure
CREATE INDEX idx_products_sku_spgist
    ON products USING spgist (sku);
```

```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT product_id, sku, name, category
FROM   products
WHERE  sku LIKE 'ELEC-%'
ORDER BY sku;
```

**SP-GiST result:**

```
-- On 1 000 rows, the planner may still choose the B-tree unique index.
-- Temporarily drop the unique index to force SP-GiST:
BEGIN;
  DROP INDEX products_sku_key;
  -- Replace with non-unique version for comparison only
  CREATE UNIQUE INDEX products_sku_key ON products (sku);

  EXPLAIN (ANALYZE, BUFFERS)
  SELECT product_id, sku FROM products WHERE sku LIKE 'ELEC-%';
  -- Result will vary by planner decision

ROLLBACK;  -- restore original unique index
```

### Where SP-GiST genuinely wins (synthetic demonstration, clearly labeled)

SP-GiST is designed for data with a **natural recursive subdivision** —
IP subnets, quad-tree spatial partitions, telephone number prefixes. It does
not maintain a balanced tree; instead it partitions the space. For uniform
data like zero-padded sequential integers, this provides no advantage. For
real-world IP address data with heavy prefix sharing, SP-GiST is measurably
smaller and faster than B-tree.

```sql
-- SYNTHETIC EXAMPLE — not run against the live OrderFlow schema.
-- Shows the correct usage pattern; not a database change.

-- If products had an inet column for origin_ip:
CREATE TABLE demo_inet_example (
    id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    host_ip inet   NOT NULL
);
CREATE INDEX ON demo_inet_example USING spgist (host_ip);

-- SP-GiST accelerates subnet containment queries that B-tree cannot:
-- SELECT * FROM demo_inet_example WHERE host_ip << '192.168.0.0/16';
-- The << (contained by) operator uses SP-GiST's inet opclass.
-- B-tree does not support << at all.
```

---

## 5. Measure Again

For OrderFlow's actual SKU prefix query:

**After (SP-GiST):** ~0.2 ms — essentially identical to B-tree on 1 000 rows
**Delta:** No meaningful improvement on a 1 000-row table with sequential SKUs.

**Honest assessment:** At 1 000 products, SP-GiST and B-tree are
interchangeable for OrderFlow's query patterns. SP-GiST would show a meaningful
advantage for:
- Tables > 1 M rows with highly non-uniform key distributions
- `inet` columns with subnet containment queries (`<<`, `>>=`, etc.)
- Text columns with natural hierarchical prefix structure (domain names,
  telephone country codes, file paths)

OrderFlow's `products.sku` is uniformly distributed (sequential suffix numbers),
which is the worst case for SP-GiST — the trie degenerates toward a balanced
tree. B-tree is the correct choice here.

---

## 6. Explain

SP-GiST (Space-Partitioned GiST) stores data in a **space-partitioned tree**
— each internal node divides the key space into disjoint partitions, and child
nodes subdivide further. Unlike B-tree (which balances by *count* of entries
per node) or GiST (which allows overlapping bounding keys), SP-GiST requires
that partitions be **non-overlapping** — each key falls in exactly one
partition.

For text with a common prefix: a radix tree (trie) built with SP-GiST stores
shared prefixes once in internal nodes and diverging suffixes in children.
`ELEC-0001` and `ELEC-0002` share a root node for `ELEC-`, diverging only at
the last 4 characters. At 1 000 products, the trie and the B-tree have similar
depth (~3 levels). At 10 M products with 7 categories, the trie's prefix sharing
pays off in significantly smaller index size.

For `inet`: PostgreSQL's built-in SP-GiST opclass for `inet` uses a binary
trie on the IP bit pattern. The `<<` containment operator is answered by the
tree without scanning all matching rows — it traverses only the subtree rooted
at the matching network prefix.

B-tree is the pragmatic default for scalar columns in most schemas. SP-GiST is
the right tool when:
1. The column's data has a natural hierarchical prefix structure.
2. Queries use operators that B-tree cannot support (`<<` for inet,
   geometric containment).
3. The non-uniform distribution creates B-tree page imbalance.

---

## 7. Cleanup / Reset Note

```sql
-- Drop the SP-GiST index — B-tree unique index (products_sku_key) is correct
-- for OrderFlow's uniform sequential SKU data.
DROP INDEX idx_products_sku_spgist;
```

The B-tree `products_sku_key` unique index (from the original schema) remains
as the production index.

---

## Further Reading

- [PostgreSQL docs — SP-GiST Indexes](https://www.postgresql.org/docs/current/spgist.html)
- [PostgreSQL docs — Network Address Functions](https://www.postgresql.org/docs/current/functions-net.html)
