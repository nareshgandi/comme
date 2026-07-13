-- =============================================================================
-- OrderFlow: Initial Schema
-- Migration : 001_initial_schema.sql
-- Milestone : 1 — FROZEN after review approval
--
-- Tables    : employees, customers, products, warehouses,
--             orders, order_items, payments
--
-- Deliberately excluded
--             inventory, suppliers, logistics, invoices, coupons, reviews
--             None of these are required to demonstrate the targeted DBA
--             concepts; including them would dilute the schema without
--             adding teaching value.
--
-- Design contract
--             Every column, type, and constraint in this file must survive
--             unmodified through labs on indexes, partitioning, streaming and
--             logical replication, Patroni, pgBackRest/PITR, RLS, pgcrypto,
--             SSL/client certs, pgaudit, FDW, pg_stat_statements, pg_cron,
--             pg_partman, pg_repack, pgvector, and cloud migration.
--
--             If a later milestone cannot work with this schema as written,
--             STOP and open a schema amendment rather than silently patching.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Schema
-- ---------------------------------------------------------------------------
CREATE SCHEMA IF NOT EXISTS orderflow;

-- All subsequent objects live in the orderflow schema.
-- Applications and workers must connect with search_path = orderflow,public
-- or qualify every reference.
SET search_path TO orderflow, public;

-- ---------------------------------------------------------------------------
-- Utility: updated_at trigger function
-- Applied to every table that carries an updated_at column.
-- Defined once here; each table registers its own trigger below.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION orderflow.set_updated_at()
    RETURNS TRIGGER
    LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;


-- =============================================================================
-- TABLE: employees
--
-- Purpose in teaching
--   RLS         — role-based row visibility: warehouse_staff see only their
--                 own row; managers see the full table; admin see everything.
--                 RLS policies added in Lab 09.
--   pgcrypto    — the salary column is intentionally plain-text now so that
--                 Lab 09 can demonstrate column-level encryption in-place
--                 without altering the column definition.
--   pg_trgm/GIN — trigram index on (first_name || ' ' || last_name) for
--                 fuzzy employee-name search. Index added in Lab 05.
--   JSONB/GIN   — metadata column stores unstructured skill tags and
--                 certifications. GIN index added in Lab 05.
--
-- Normalization rationale
--   role is a VARCHAR CHECK list rather than a FK to a lookup table because
--   the role set is small (5 values) and stable. A lookup table would add a
--   join to every RLS policy evaluation with no meaningful benefit.
--   department is free-text VARCHAR for the same reason.
-- =============================================================================
CREATE TABLE orderflow.employees (
    employee_id BIGINT         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name  VARCHAR(100)   NOT NULL,
    last_name   VARCHAR(100)   NOT NULL,
    email       VARCHAR(255)   NOT NULL,
    phone       VARCHAR(30),
    role        VARCHAR(50)    NOT NULL
                    CHECK (role IN (
                        'warehouse_staff',
                        'courier',
                        'finance',
                        'manager',
                        'admin'
                    )),
    department  VARCHAR(100),
    hire_date   DATE           NOT NULL DEFAULT CURRENT_DATE,
    -- salary is stored as plain NUMERIC in M1.
    -- Lab 09 (pgcrypto) will encrypt this column without changing its type
    -- by storing pgp_sym_encrypt output in the same column and altering
    -- application queries — a deliberate teaching choice.
    salary      NUMERIC(12, 2) NOT NULL
                    CHECK (salary >= 0),
    is_active   BOOLEAN        NOT NULL DEFAULT TRUE,
    -- metadata: unstructured KV store — certifications, emergency contacts,
    -- skill tags.  Workers may populate it; the exact keys are not prescribed.
    -- GIN index added in Lab 05.
    metadata    JSONB,
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT employees_email_key UNIQUE (email)
);

CREATE TRIGGER trg_employees_updated_at
    BEFORE UPDATE ON orderflow.employees
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- TABLE: customers
--
-- Purpose in teaching
--   RLS         — customers may only read their own rows through the app role;
--                 finance and admin roles see all. Lab 09.
--   pgcrypto    — email and phone are PII candidates for column-level
--                 encryption in Lab 09.
--   pg_trgm/GIN — fuzzy name and email search. Lab 05.
--   JSONB/GIN   — metadata stores customer preferences, A/B test cohort,
--                 marketing flags. GIN index in Lab 05.
--   Partitioning — if the customer table grows very large, hash partitioning
--                  on customer_id is the natural strategy. Not implemented
--                  in M1; Lab 06 uses orders as the partitioning example.
--
-- Normalization rationale
--   Address columns represent the customer's default (billing) address.
--   Orders carry their own snapshot of the shipping address so historical
--   orders remain correct even when a customer moves.  This is an intentional
--   denormalization on orders — it is documented there as well.
-- =============================================================================
CREATE TABLE orderflow.customers (
    customer_id   BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name    VARCHAR(100) NOT NULL,
    last_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(255) NOT NULL,
    phone         VARCHAR(30),
    address_line1 VARCHAR(255),
    address_line2 VARCHAR(255),
    city          VARCHAR(100),
    state         VARCHAR(100),
    country       VARCHAR(100) NOT NULL DEFAULT 'US',
    postal_code   VARCHAR(20),
    loyalty_tier  VARCHAR(20)  NOT NULL DEFAULT 'bronze'
                      CHECK (loyalty_tier IN (
                          'bronze', 'silver', 'gold', 'platinum'
                      )),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    -- metadata: preferences, A/B test flags, custom attributes.
    -- GIN index added in Lab 05.
    metadata      JSONB,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT customers_email_key UNIQUE (email)
);

CREATE TRIGGER trg_customers_updated_at
    BEFORE UPDATE ON orderflow.customers
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- TABLE: products
--
-- Purpose in teaching
--   FTS / pg_trgm / GIN — full-text and fuzzy search on name and description.
--                          A tsvector generated column will be added in Lab 05.
--                          A GIN index on metadata for attribute filtering also
--                          comes in Lab 05.
--   pgvector             — a vector FLOAT4[] or vector(1536) embedding column
--                          for semantic product search is added in Lab 10.
--                          Not added here to avoid a dependency on the
--                          pgvector extension before it is installed.
--   FDW                  — external product catalog integration via
--                          postgres_fdw in Lab 10; the SKU column is the
--                          natural join key to the foreign table.
--   JSONB                — metadata stores variant attributes (size, color,
--                          material, specs) and supplier references.
--
-- Normalization rationale
--   unit_price is the current list price for the product.  It can change at
--   any time without corrupting historical data because order_items snapshots
--   the price at purchase time.  This design teaches the snapshot pattern and
--   is a realistic production commerce pattern.
-- =============================================================================
CREATE TABLE orderflow.products (
    product_id  BIGINT         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku         VARCHAR(100)   NOT NULL,
    name        VARCHAR(255)   NOT NULL,
    description TEXT,
    category    VARCHAR(100)   NOT NULL,
    subcategory VARCHAR(100),
    unit_price  NUMERIC(10, 2) NOT NULL
                    CHECK (unit_price >= 0),
    weight_kg   NUMERIC(8, 3)
                    CHECK (weight_kg >= 0),
    is_active   BOOLEAN        NOT NULL DEFAULT TRUE,
    -- metadata: variant attributes (size/color/specs), supplier references,
    -- any future extensible product properties.
    -- GIN index added in Lab 05.
    metadata    JSONB,
    created_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT products_sku_key UNIQUE (sku)
);

CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON orderflow.products
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- TABLE: warehouses
--
-- Purpose in teaching
--   FDW          — each region (e.g. 'us-east', 'us-west', 'eu-central') will
--                  become a separate foreign server in Lab 10 using
--                  postgres_fdw to simulate cross-region data federation.
--                  The region column is the key; its allowed values must
--                  match config.yaml so the FDW setup can be scripted.
--   Replication  — the warehouse topology mirrors the HA replication topology
--                  demonstrated in Lab 07 (primary in us-east, replica in
--                  us-west).  Same region names, same mental model.
--   Partitioning — warehouses.warehouse_id can serve as a list-partition key
--                  for orders if a per-warehouse partitioning strategy is
--                  explored as an alternative to time-based partitioning.
--                  Not implemented in M1.
--
-- Normalization rationale
--   warehouses has no reverse FK to orders; the relationship is
--   orders → warehouses (each order is fulfilled from one warehouse).
--   Keeping warehouses clean of order references allows the FDW lab to
--   federate the warehouses table independently.
-- =============================================================================
CREATE TABLE orderflow.warehouses (
    warehouse_id  BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code          VARCHAR(20)  NOT NULL,
    name          VARCHAR(255) NOT NULL,
    -- region values come from config.yaml (e.g. 'us-east', 'us-west',
    -- 'eu-central').  Workers must only assign orders to warehouses whose
    -- region appears in the active config.  FDW lab creates one foreign
    -- server per distinct region value found in this column.
    region        VARCHAR(50)  NOT NULL,
    address_line1 VARCHAR(255),
    city          VARCHAR(100),
    state         VARCHAR(100),
    country       VARCHAR(100) NOT NULL DEFAULT 'US',
    postal_code   VARCHAR(20),
    capacity_sqft INTEGER
                      CHECK (capacity_sqft > 0),
    is_active     BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    CONSTRAINT warehouses_code_key UNIQUE (code)
);

CREATE TRIGGER trg_warehouses_updated_at
    BEFORE UPDATE ON orderflow.warehouses
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- TABLE: orders
--
-- Purpose in teaching
--   RANGE partitioning — created_at is the natural partition key: orders
--                        arrive in time order, queries are almost always
--                        time-bounded, and BRIN indexes are most effective
--                        on monotonically increasing columns.
--                        The table is created as a plain (unpartitioned)
--                        table here.  Lab 06 covers the full conversion
--                        strategy: create a new partitioned table with
--                        pg_partman, attach the old table as a partition,
--                        then cut over — or use pg_repack to rewrite the
--                        heap in a partitioned form online.
--   BRIN index         — created_at is a BRIN candidate (Lab 05) because
--                        the physical heap insertion order matches the
--                        logical time order, making BRIN highly selective.
--   Logical replication — orders is the primary replication target in Lab 07;
--                         the status column enables publication filtering
--                         (e.g. replicate only SHIPPED/DELIVERED to a
--                         reporting replica).
--   pgBackRest PITR    — order insert volume drives WAL generation rate;
--                        Lab 08 uses orders to calibrate PITR RTO/RPO.
--   pgaudit            — status-change audit trail in Lab 09; all UPDATEs
--                        to orders.status are logged via pgaudit.
--
-- Normalization rationale
--   total_amount is intentionally denormalized (it can be computed from
--   SUM(order_items.line_total)).  The denormalization is deliberate:
--   (a) it is a realistic production pattern for high-read commerce systems,
--   (b) it teaches MVCC and transaction discipline — workers must update
--       total_amount in the same transaction as the order_items insert, and
--   (c) it creates a measurable consistency invariant (INV-06) that later
--       labs can audit with a query.
--
--   Shipping address columns are a snapshot of the customer's address at
--   order creation time.  This is the correct design: if a customer updates
--   their address, historical orders must remain unaffected.
-- =============================================================================
CREATE TABLE orderflow.orders (
    order_id               BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id            BIGINT        NOT NULL
                               REFERENCES orderflow.customers (customer_id),
    -- employee_id: the warehouse employee assigned to this order.
    -- Nullable — not every order has an assigned employee in the simulation.
    employee_id            BIGINT
                               REFERENCES orderflow.employees (employee_id),
    warehouse_id           BIGINT
                               REFERENCES orderflow.warehouses (warehouse_id),
    status                 VARCHAR(20)   NOT NULL DEFAULT 'NEW'
                               CHECK (status IN (
                                   'NEW',
                                   'PROCESSING',
                                   'PACKED',
                                   'SHIPPED',
                                   'DELIVERED',
                                   'RETURNED',
                                   'REFUNDED'
                               )),
    -- total_amount: denormalized SUM(order_items.line_total).
    -- Workers must update this in the same transaction as order_items inserts.
    total_amount           NUMERIC(12, 2) NOT NULL DEFAULT 0
                               CHECK (total_amount >= 0),
    -- Shipping address: snapshot from customers.address_* at creation time.
    shipping_address_line1 VARCHAR(255),
    shipping_address_line2 VARCHAR(255),
    shipping_city          VARCHAR(100),
    shipping_state         VARCHAR(100),
    shipping_country       VARCHAR(100),
    shipping_postal_code   VARCHAR(20),
    notes                  TEXT,
    -- Lifecycle timestamps: set by workers as the order advances.
    -- NULL is valid until the corresponding status is reached.
    shipped_at             TIMESTAMPTZ,
    delivered_at           TIMESTAMPTZ,
    -- created_at is the future RANGE partition key (Lab 06).
    -- Do not add a BRIN index here — that is Lab 05's work.
    created_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    -- The only temporal invariant enforceable without application context.
    -- Remaining invariants (INV-04, INV-09, INV-10) are enforced by workers.
    CONSTRAINT orders_shipped_before_delivered
        CHECK (
            shipped_at  IS NULL
            OR delivered_at IS NULL
            OR shipped_at <= delivered_at
        )
);

CREATE TRIGGER trg_orders_updated_at
    BEFORE UPDATE ON orderflow.orders
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- TABLE: order_items
--
-- Purpose in teaching
--   B-tree composite index — (order_id, product_id) is the primary access
--                            pattern; a covering index variant
--                            (order_id) INCLUDE (line_total, quantity) will
--                            be added in Lab 05 to eliminate heap fetches
--                            for aggregation queries.
--   FK integrity            — ON DELETE CASCADE on order_id demonstrates
--                             cascaded delete behaviour under concurrent
--                             transactions in Lab 04.
--   Join optimization       — order_items is the central join table in most
--                             analytical queries; Lab 05 uses it to contrast
--                             hash join vs. nested-loop plans.
--   Generated columns       — line_total is GENERATED ALWAYS AS STORED,
--                             teaching how PostgreSQL evaluates and stores
--                             computed values alongside row data (Lab 04).
--
-- Normalization rationale
--   unit_price is snapshotted at insert time — independent of
--   products.unit_price — so that product price changes never corrupt
--   historical line totals.  This is the same snapshot pattern used for
--   orders.shipping_address_*.
--
--   No updated_at column: order_items are immutable after insert
--   (business_rules.md INV-07).  Including updated_at would imply that
--   updates are valid, which they are not.
-- =============================================================================
CREATE TABLE orderflow.order_items (
    order_item_id BIGINT         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id      BIGINT         NOT NULL
                      REFERENCES orderflow.orders (order_id)
                      ON DELETE CASCADE,
    product_id    BIGINT         NOT NULL
                      REFERENCES orderflow.products (product_id),
    quantity      INTEGER        NOT NULL
                      CHECK (quantity > 0),
    -- unit_price: price at the moment this order was placed.
    -- Independent of products.unit_price after insert.
    unit_price    NUMERIC(10, 2) NOT NULL
                      CHECK (unit_price >= 0),
    discount_pct  NUMERIC(5, 2)  NOT NULL DEFAULT 0
                      CHECK (discount_pct BETWEEN 0 AND 100),
    -- line_total: always consistent with quantity, unit_price, discount_pct.
    -- STORED means PostgreSQL writes it to the heap — it is queryable and
    -- indexable like any regular column, but always derived.
    line_total    NUMERIC(12, 2) GENERATED ALWAYS AS (
                      ROUND(
                          quantity * unit_price * (1.0 - discount_pct / 100.0),
                          2
                      )
                  ) STORED,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);


-- =============================================================================
-- TABLE: payments
--
-- Purpose in teaching
--   pgaudit          — every INSERT and UPDATE on this table is logged in
--                      Lab 09 to satisfy a PCI-DSS-style financial audit trail.
--   MVCC / isolation  — concurrent payment attempts on the same order_id
--                       demonstrate SERIALIZABLE isolation failures and
--                       application-level retry logic in Lab 04.
--   pgcrypto         — gateway_reference (card token / processor txn ID)
--                      is a candidate for pgp_sym_encrypt in Lab 09.
--                      It is plain VARCHAR here to keep M1 schema-only.
--   pg_cron          — a scheduled job to retry FAILED payments is added
--                      in Lab 10; the job queries payments WHERE status='FAILED'
--                      AND created_at > NOW() - INTERVAL '1 hour'.
--   Rollback / savepoint — partial payment failure rollback within a
--                           multi-step transaction is demonstrated in Lab 04.
--
-- Normalization rationale
--   One order can have multiple payment rows: each retry creates a new row
--   (all FAILED rows are retained for audit), and a REFUNDED order gets an
--   additional row with status='REFUNDED'.  The uniqueness constraint — only
--   one SUCCESS payment per order — is enforced by workers now.  Lab 05 will
--   add a partial unique index on (order_id) WHERE status = 'SUCCESS' to make
--   this a database-enforced invariant.  It is deliberately left out of M1
--   so Lab 05 can add it as a teaching exercise.
--
--   gateway_reference is VARCHAR(255) to accommodate multiple payment
--   processors with different ID formats.  Encryption is added in Lab 09.
-- =============================================================================
CREATE TABLE orderflow.payments (
    payment_id        BIGINT         GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id          BIGINT         NOT NULL
                          REFERENCES orderflow.orders (order_id),
    amount            NUMERIC(12, 2) NOT NULL
                          CHECK (amount > 0),
    method            VARCHAR(30)    NOT NULL
                          CHECK (method IN (
                              'credit_card',
                              'debit_card',
                              'paypal',
                              'bank_transfer',
                              'wallet'
                          )),
    status            VARCHAR(20)    NOT NULL DEFAULT 'PENDING'
                          CHECK (status IN (
                              'PENDING',
                              'SUCCESS',
                              'FAILED',
                              'REFUNDED'
                          )),
    -- gateway_reference: external payment processor transaction ID.
    -- Plain VARCHAR in M1; pgcrypto lab will add column-level encryption.
    gateway_reference VARCHAR(255),
    failure_reason    VARCHAR(255),
    processed_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE TRIGGER trg_payments_updated_at
    BEFORE UPDATE ON orderflow.payments
    FOR EACH ROW EXECUTE FUNCTION orderflow.set_updated_at();


-- =============================================================================
-- End of migration 001_initial_schema.sql
-- Schema frozen at Milestone 1 approval.
-- Next migration file: 002_* (to be created in a future milestone only when
-- a schema amendment is formally approved).
-- =============================================================================
