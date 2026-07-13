# OrderFlow — Roadmap

The milestone discipline: **one milestone at a time**. Once a milestone is
approved it is frozen. A later milestone that requires changing an earlier
deliverable must stop, document the conflict, and get explicit sign-off.

---

## Build Milestones

| # | Milestone | Description | Status |
|---|-----------|-------------|--------|
| M1 | **Architecture & Data Model** | Repo skeleton, architecture docs, business rules, 7-table schema DDL, ER diagram | **Approved** |
| M2 | **Python Factories** | `factories/` module — dataclasses, 5 factories, reference_data, tests, requirements.txt | **Approved** |
| M3 | **Worker Architecture** | `workers/` module — db.py, history_loader, order_generator, order_processor, payment_processor, employee_updates | **Approved** |
| M4 | **Configuration System** | `config.yaml` schema + Python config loader; all tunable parameters extracted; validated on load | **Approved** |
| M5 | **Bootstrap & GitHub Quality** | `bootstrap.py` — preflight, DB provisioning, schema, historical load, worker launch; `--status`/`--stop`; pinned requirements; README rewrite | **Approved** |
| M6 | **Lab Methodology & Golden Rule** | `labs/LAB_TEMPLATE.md` — 8-section template; `labs/README.md` golden rule; foundation write-ups for labs 01/02/03 | **Approved** |
| M7 | **PostgreSQL Core** (Labs 04 + 05) | 1 constraints lab (`04_postgresql/01_constraints`) + 14 index labs (`05_indexes/01`–`14`); 13 persistent baseline indexes; pg_trgm exception documented | **Approved** |
| M8 | **Storage Engineering** (Lab 06) | Step 0 structural fix; `002_partition_orders.sql` migration; 6 partitioning sub-labs (range, pruning, BRIN, compression, maintenance, detach/attach); `scripts/provision_monthly_partition.py` | **In Review** |
| M9 | **High Availability** (Lab 07) | Streaming replication, logical replication, Patroni failover | Not Started |

---

## Lab Milestones

Each lab milestone reuses the M1 schema and the M5 live workload without
modification. Labs add *observation* (queries, EXPLAIN plans) or *controlled
change* (indexes, partitions, config) — they do not alter the base tables.

| # | Lab | Topic | Status |
|---|-----|-------|--------|
| L01 | Lab 01 | Repository orientation & architecture walkthrough | Not Started |
| L02 | Lab 02 | Business rules deep-dive: trace a full order lifecycle in psql | Not Started |
| L03 | Lab 03 | Python factory patterns: read the code, understand data generation | Not Started |
| L04 | Lab 04 | PostgreSQL core: transactions, MVCC, generated columns, triggers, FK cascade | Not Started |
| L05 | Lab 05 | Indexes: B-tree, BRIN, GIN, trigram; EXPLAIN ANALYZE; covering indexes; partial unique index on payments | Not Started |
| L06 | Lab 06 | Partitioning: RANGE partition orders on created_at with pg_partman; pg_repack for online reorganization | Not Started |
| L07 | Lab 07 | Replication: streaming replication setup; logical replication with publications/subscriptions; Patroni failover | Not Started |
| L08 | Lab 08 | Backup & PITR: pgBackRest full/incremental backup; WAL archiving; point-in-time recovery; RTO/RPO measurement | Not Started |
| L09 | Lab 09 | Security: RLS policies on employees/customers; pgcrypto column encryption; pgaudit on payments; SSL/client certs | Not Started |
| L10 | Lab 10 | Extensions: pg_cron (payment retry job); FDW (cross-region warehouses); pgvector (product embeddings) | Not Started |
| L11 | Lab 11 | Monitoring: pg_stat_statements; pg_stat_activity; autovacuum tuning; bloat analysis with pgstattuple | Not Started |
| L12 | Lab 12 | Cloud migration: AWS RDS / Aurora; Azure Database for PostgreSQL; GCP Cloud SQL; pg_dump strategy; managed-service trade-offs | Not Started |

---

## Structural Corrections

| Correction | Applied In | Description |
|------------|-----------|-------------|
| M8 Step 0: Index labs moved from `labs/04_postgresql/` to `labs/05_indexes/` | M8 | M7 placed index sub-labs under `04_postgresql/`, contradicting the M1 repo skeleton which reserved `05_indexes/` for this content. Labs renumbered `05.01`–`05.14`; `04_postgresql/` now contains only `01_constraints/` (future: MVCC, VACUUM, statistics, execution plans, locking, transactions `02_`–`07_`). All cross-references updated. |

---

## Documented Exceptions

These are intentional, approved deviations from the stated lab policy.

| Exception | Lab | Reason |
|-----------|-----|--------|
| `pg_trgm` installed before the general extensions lab (Lab 10) | 04.10 | M1 schema comment on `employees` explicitly anticipates this: *"pg_trgm/GIN — trigram index added in Lab 05."* Installing it in Lab 04.10 is intentional and consistent with the schema design decision. |
| `btree_gist` extension persists after Lab 04.11 | 04.11 | Required for exclusion constraint support on scalar types; may be needed by future labs (replication/partitioning labs). |

---

## Frozen Deliverables

Once approved, the following are immutable:

| Milestone | Frozen Artifact |
|-----------|-----------------|
| M1 | `database/migrations/001_initial_schema.sql` — 7-table schema |
| M1 | `business_rules.md` — canonical order lifecycle |
| M4 | `config.yaml` schema (keys and types; values remain tunable) |
