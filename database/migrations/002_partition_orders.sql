-- =============================================================================
-- OrderFlow: Migration 002 — Convert orders to declarative RANGE partitioning
-- Migration : 002_partition_orders.sql
-- Milestone : 8 — Storage Engineering
--
-- Design decision (approved before this migration was written):
--   labs/06_partitioning/00_design_decision.md
--
-- Summary
--   Converts orderflow.orders from a monolithic heap table to a declarative
--   RANGE-partitioned table on created_at, with monthly child partitions.
--
-- Approved trade-off
--   PostgreSQL requires the partition key (created_at) in every PRIMARY KEY /
--   UNIQUE constraint on a partitioned table.  The FK constraints from
--   order_items.order_id and payments.order_id to orders(order_id) are therefore
--   dropped: orders(order_id) alone is no longer a globally-unique key.
--   Referential integrity is maintained by application invariants INV-11 and
--   INV-12.  See 00_design_decision.md for full reasoning.
--
-- Workers (unmodified)
--   order_generator, order_processor, and payment_processor continue to run
--   without any code changes.  created_at DEFAULT NOW() routes new inserts to
--   the correct monthly partition automatically.
--
-- Apply
--   psql -U postgres -d orderflow -v ON_ERROR_STOP=1 -f 002_partition_orders.sql
--
-- The entire migration runs inside one transaction.  Any failure rolls the
-- database back to the pre-migration state.
-- =============================================================================

BEGIN;

SET search_path TO orderflow, public;

-- ---------------------------------------------------------------------------
-- Guard: abort immediately if orders is already partitioned
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM   pg_class     c
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  c.relname  = 'orders'
          AND  n.nspname  = 'orderflow'
          AND  c.relkind  = 'p'          -- 'p' = partitioned table
    ) THEN
        RAISE EXCEPTION
            'Migration 002 has already been applied: orders is already a '
            'partitioned table.  Aborting.';
    END IF;
END;
$$;


-- ---------------------------------------------------------------------------
-- Step 1 — Preserve the existing data: rename monolithic table to orders_old
-- ---------------------------------------------------------------------------
ALTER TABLE orderflow.orders RENAME TO orders_old;

-- The trg_orders_updated_at trigger moves with the rename and stays on
-- orders_old.  A new trigger will be created on the new partitioned table.


-- ---------------------------------------------------------------------------
-- Step 2 — Drop FK constraints whose referenced column is no longer
--           globally unique after partitioning
-- ---------------------------------------------------------------------------

-- order_items: ON DELETE CASCADE is also lost here.  This only protected
-- against direct DELETE FROM orders, which no worker ever performs.
-- Orders reach terminal states via status transitions, not deletes.
ALTER TABLE orderflow.order_items
    DROP CONSTRAINT order_items_order_id_fkey;

-- payments: simple FK to orders(order_id)
ALTER TABLE orderflow.payments
    DROP CONSTRAINT payments_order_id_fkey;

-- Both columns remain NOT NULL.  Application-level checks (INV-11, INV-12)
-- replace DB-level enforcement.  See post-migration verification section.


-- ---------------------------------------------------------------------------
-- Step 3 — Create the new partitioned orders table
--
-- order_id is BIGINT NOT NULL (no identity yet — added in Step 7 after the
-- data copy, to avoid identity sequence naming conflicts during the rename).
-- ---------------------------------------------------------------------------
CREATE TABLE orderflow.orders (
    order_id               BIGINT         NOT NULL,
    customer_id            BIGINT         NOT NULL
                               REFERENCES orderflow.customers  (customer_id),
    employee_id            BIGINT
                               REFERENCES orderflow.employees  (employee_id),
    warehouse_id           BIGINT
                               REFERENCES orderflow.warehouses (warehouse_id),
    status                 VARCHAR(20)    NOT NULL DEFAULT 'NEW'
                               CHECK (status IN (
                                   'NEW',
                                   'PROCESSING',
                                   'PACKED',
                                   'SHIPPED',
                                   'DELIVERED',
                                   'RETURNED',
                                   'REFUNDED'
                               )),
    total_amount           NUMERIC(12, 2) NOT NULL DEFAULT 0
                               CHECK (total_amount >= 0),
    shipping_address_line1 VARCHAR(255),
    shipping_address_line2 VARCHAR(255),
    shipping_city          VARCHAR(100),
    shipping_state         VARCHAR(100),
    shipping_country       VARCHAR(100),
    shipping_postal_code   VARCHAR(20),
    notes                  TEXT,
    shipped_at             TIMESTAMPTZ,
    delivered_at           TIMESTAMPTZ,
    -- created_at is the RANGE partition key.
    -- DEFAULT NOW() ensures that any INSERT that omits created_at (all three
    -- workers) is routed to the correct monthly partition automatically.
    created_at             TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT orders_shipped_before_delivered
        CHECK (
            shipped_at  IS NULL
            OR delivered_at IS NULL
            OR shipped_at <= delivered_at
        ),
    -- Composite PRIMARY KEY: PostgreSQL requires the partition key column
    -- (created_at) in every unique/primary key constraint on a partitioned
    -- table.  This is the approved trade-off documented in 00_design_decision.md.
    PRIMARY KEY (order_id, created_at)
) PARTITION BY RANGE (created_at);

-- Recreate the updated_at trigger.  On PostgreSQL 13+, triggers on a
-- partitioned table are automatically inherited by all partitions.
CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orderflow.orders
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- ---------------------------------------------------------------------------
-- Step 4 — Create monthly RANGE child partitions
--
-- Range: earliest month in orders_old through current month + 2 (forward
-- provision so the workers never hit a partition gap).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    start_month DATE;
    end_month   DATE;
    part_start  DATE;
    part_end    DATE;
    part_name   TEXT;
    part_count  INT  := 0;
BEGIN
    -- Earliest month present in the data
    SELECT DATE_TRUNC('month', MIN(created_at))::DATE
    INTO   start_month
    FROM   orderflow.orders_old;

    IF start_month IS NULL THEN
        RAISE EXCEPTION
            'orders_old is empty — run history_loader.py before applying '
            'migration 002.';
    END IF;

    -- Provision two months ahead of today
    end_month := (DATE_TRUNC('month', NOW()) + INTERVAL '2 months')::DATE;

    RAISE NOTICE 'Provisioning monthly partitions from % through %.',
        start_month, end_month;

    part_start := start_month;
    WHILE part_start <= end_month LOOP
        part_end  := (part_start + INTERVAL '1 month')::DATE;
        part_name := 'orders_' || TO_CHAR(part_start, 'YYYY_MM');

        EXECUTE format(
            'CREATE TABLE orderflow.%I '
            'PARTITION OF orderflow.orders '
            'FOR VALUES FROM (%L::timestamptz) TO (%L::timestamptz)',
            part_name,
            part_start::TEXT,
            part_end::TEXT
        );

        part_start := part_end;
        part_count := part_count + 1;
    END LOOP;

    RAISE NOTICE 'Created % monthly partitions.', part_count;
END;
$$;

-- DEFAULT partition: catches any row whose created_at falls outside the
-- provisioned range.  Workers would produce rows here only if the monthly
-- provisioning script falls behind.  Detected by: SELECT COUNT(*) FROM
-- orders_default; — a non-zero result means provisioning is overdue.
CREATE TABLE orderflow.orders_default
    PARTITION OF orderflow.orders DEFAULT;


-- ---------------------------------------------------------------------------
-- Step 5 — Bulk-copy data from orders_old into the new partitioned table
--
-- Column list is explicit to guard against column-order differences.
-- No OVERRIDING SYSTEM VALUE needed because order_id is plain BIGINT NOT NULL
-- at this point (identity is added in Step 7, after the data copy).
-- ---------------------------------------------------------------------------
INSERT INTO orderflow.orders (
    order_id, customer_id, employee_id, warehouse_id,
    status, total_amount,
    shipping_address_line1, shipping_address_line2,
    shipping_city, shipping_state, shipping_country, shipping_postal_code,
    notes, shipped_at, delivered_at, created_at, updated_at
)
SELECT
    order_id, customer_id, employee_id, warehouse_id,
    status, total_amount,
    shipping_address_line1, shipping_address_line2,
    shipping_city, shipping_state, shipping_country, shipping_postal_code,
    notes, shipped_at, delivered_at, created_at, updated_at
FROM orderflow.orders_old;


-- ---------------------------------------------------------------------------
-- Step 6 — Verify row counts match before destroying the source
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    old_count BIGINT;
    new_count BIGINT;
BEGIN
    SELECT COUNT(*) INTO old_count FROM orderflow.orders_old;
    SELECT COUNT(*) INTO new_count FROM orderflow.orders;

    IF old_count <> new_count THEN
        RAISE EXCEPTION
            'Row count mismatch: orders_old=% orders=%.  Aborting.',
            old_count, new_count;
    END IF;

    RAISE NOTICE 'Row count verified: % rows migrated.', new_count;
END;
$$;


-- ---------------------------------------------------------------------------
-- Step 7 — Add GENERATED ALWAYS AS IDENTITY to order_id
--
-- Done after the copy so that inserting historical rows (Step 5) does not
-- require OVERRIDING SYSTEM VALUE and creates no sequence naming conflicts
-- with the sequence still owned by orders_old.
--
-- START WITH is set dynamically to MAX(order_id) + 1 so that new INSERTs
-- from the workers continue the sequence without collision.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    max_id BIGINT;
BEGIN
    SELECT MAX(order_id) INTO max_id FROM orderflow.orders;
    max_id := COALESCE(max_id, 0);

    EXECUTE format(
        'ALTER TABLE orderflow.orders '
        'ALTER COLUMN order_id '
        'ADD GENERATED ALWAYS AS IDENTITY (START WITH %s)',
        max_id + 1
    );

    RAISE NOTICE 'order_id identity sequence starts at %.', max_id + 1;
END;
$$;


-- ---------------------------------------------------------------------------
-- Step 8 — Drop the old monolithic table (data is verified in the new table)
-- ---------------------------------------------------------------------------
DROP TABLE orderflow.orders_old;


-- ---------------------------------------------------------------------------
-- Step 9 — Recreate Milestone 7 indexes on the partitioned table
--
-- Indexes created on a partitioned table are automatically propagated to all
-- current and future child partitions.
-- ---------------------------------------------------------------------------

-- Lab 05.01: B-tree on customer_id (order history dashboard)
CREATE INDEX idx_orders_customer_id
    ON orderflow.orders (customer_id);

-- Lab 05.04: Partial index on active statuses (payment worker hot path)
CREATE INDEX idx_orders_active_status_created
    ON orderflow.orders (status, created_at DESC)
    WHERE status IN ('NEW', 'PROCESSING');

-- Lab 05.12: BRIN on created_at (date-range revenue reports)
-- Each monthly partition has near-perfect physical correlation (all rows in a
-- given month arrive in time order).  Lab 06.03 demonstrates that per-partition
-- BRIN is substantially more effective than the old whole-table BRIN.
CREATE INDEX idx_orders_created_at_brin
    ON orderflow.orders USING brin (created_at)
    WITH (pages_per_range = 128);

-- Lab 05.13: B-tree on warehouse_id (fulfillment queries)
CREATE INDEX idx_orders_warehouse_id
    ON orderflow.orders (warehouse_id);


-- ---------------------------------------------------------------------------
-- Step 10 — Final structural assertion
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    partition_count INT;
BEGIN
    SELECT COUNT(*) INTO partition_count
    FROM   pg_inherits  i
    JOIN   pg_class     p ON p.oid = i.inhparent
    JOIN   pg_namespace n ON n.oid = p.relnamespace
    WHERE  p.relname  = 'orders'
      AND  n.nspname  = 'orderflow';

    ASSERT (
        SELECT relkind = 'p'
        FROM   pg_class     c
        JOIN   pg_namespace n ON n.oid = c.relnamespace
        WHERE  c.relname  = 'orders'
          AND  n.nspname  = 'orderflow'
    ), 'ASSERT failed: orders.relkind is not ''p''';

    RAISE NOTICE 'Migration 002 complete.  orders is now a RANGE-partitioned '
        'table with % child partitions (including default).',
        partition_count;
END;
$$;

COMMIT;


-- =============================================================================
-- Post-migration verification — run manually after applying this migration
-- =============================================================================

-- 1. Confirm partition layout
--    SELECT tableoid::regclass AS partition, COUNT(*) AS rows
--    FROM   orderflow.orders
--    GROUP  BY 1
--    ORDER  BY 1;

-- 2. Confirm workers route new inserts to the correct partition
--    (run this a few seconds after bootstrap.py --status shows workers running)
--    SELECT tableoid::regclass AS partition, MAX(created_at) AS latest_row
--    FROM   orderflow.orders
--    GROUP  BY 1
--    ORDER  BY 2 DESC
--    LIMIT  3;

-- 3. Verify INV-11 — no orphan order_items
--    SELECT COUNT(*) AS orphan_items
--    FROM   orderflow.order_items oi
--    WHERE  NOT EXISTS (
--        SELECT 1 FROM orderflow.orders o WHERE o.order_id = oi.order_id
--    );

-- 4. Verify INV-12 — no orphan payments
--    SELECT COUNT(*) AS orphan_payments
--    FROM   orderflow.payments p
--    WHERE  NOT EXISTS (
--        SELECT 1 FROM orderflow.orders o WHERE o.order_id = p.order_id
--    );

-- 5. Confirm partition pruning on a date-range query
--    EXPLAIN (ANALYZE, BUFFERS)
--    SELECT COUNT(*) FROM orderflow.orders
--    WHERE  created_at >= NOW() - INTERVAL '30 days';
--    -- Expected: only 1-2 partitions scanned, not all.

-- 6. Confirm default partition is empty (if it has rows, run the provisioning
--    script: scripts/provision_monthly_partition.py)
--    SELECT COUNT(*) FROM orderflow.orders_default;
-- =============================================================================
