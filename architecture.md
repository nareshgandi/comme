# OrderFlow — Architecture

## Overview

OrderFlow is a continuously-running Python simulation backed by a single
PostgreSQL database. Its architecture is intentionally lean: the **database**
is the subject of study, not the application. The Python layer exists only to
produce a realistic, live workload.

```
┌──────────────────────────────────────────────────────────────────┐
│                         Python Workers                           │
│                                                                  │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────────┐  │
│  │ Order Worker │  │Payment Worker │  │  Status/Fulfillment  │  │
│  │ (creates new │  │ (attempts     │  │  Worker (PACKED →    │  │
│  │  orders)     │  │  payments,    │  │  SHIPPED →           │  │
│  └──────┬───────┘  │  retries)     │  │  DELIVERED →         │  │
│         │          └──────┬────────┘  │  RETURNED/REFUNDED)  │  │
│         │                 │           └──────────┬───────────┘  │
└─────────┼─────────────────┼──────────────────────┼──────────────┘
          │                 │                       │
          ▼                 ▼                       ▼
┌──────────────────────────────────────────────────────────────────┐
│                  PostgreSQL (on-prem, native install)            │
│                                                                  │
│  Schema : orderflow                                              │
│  Tables : employees · customers · products · warehouses          │
│           orders · order_items · payments                        │
│                                                                  │
│  Config : all tunable parameters read from config.yaml           │
└──────────────────────────────────┬───────────────────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
          ▼                        ▼                        ▼
 ┌─────────────────┐   ┌───────────────────┐   ┌─────────────────────┐
 │  Storage &      │   │  HA & Durability   │   │  Security,          │
 │  Query          │   │                   │   │  Monitoring &       │
 │  Engineering    │   │  Lab 07:Replication│   │  Extensions         │
 │                 │   │  Lab 08: Backup/  │   │                     │
 │  Lab 05: Indexes│   │          PITR     │   │  Labs 09–12         │
 │  Lab 06: Parts  │   └───────────────────┘   └─────────────────────┘
 └─────────────────┘
```

## Deployment Target

OrderFlow runs on a **bare on-prem VM** with:
- PostgreSQL installed natively on the host (no containers)
- A Python 3.11+ virtual environment
- All configuration in `config.yaml` (no hardcoded values anywhere)

The `docker/` directory is reserved for a future containerized setup and
contains no active code in this milestone.

## Design Principles

1. **One schema, one workload.** Every lab reuses the same 7-table schema and
   the same running dataset. No lab spins up its own fake data.

2. **Freeze before you build.** The schema is frozen at Milestone 1. Any later
   milestone that requires a schema change must stop, document the conflict,
   and get explicit approval before modifying `001_initial_schema.sql`.

3. **Application-only data entry.** All rows enter the database through Python
   factories and workers. No `INSERT` scripts, ever. The database must look
   like it is being hit by a real application.

4. **Config-driven.** Batch sizes, sleep intervals, success rates, warehouse
   counts, product counts, and DB connection info all come from `config.yaml`.
   Nothing is hardcoded.

5. **Simple application, complex database.** The Python workers are
   intentionally simple so that all complexity (concurrency, isolation,
   partitioning, replication, security) is expressed and observed in
   PostgreSQL, not in the application tier.

## Phase Breakdown

### Phase 1 — Business Simulation (Labs 01–04)
Stand up the workload. Factories generate seed data; workers continuously
process the order lifecycle. Labs cover: repository orientation, business
rules, Python factory patterns, and PostgreSQL fundamentals (transactions,
MVCC, generated columns, triggers).

### Phase 2 — Storage Engineering (Labs 05–06)
Labs cover: B-tree, BRIN, GIN, and trigram indexes; query plan analysis with
`EXPLAIN (ANALYZE, BUFFERS)`; RANGE and HASH partitioning with `pg_partman`;
`pg_repack` for online table reorganization.

### Phase 3 — High Availability (Lab 07)
Labs cover: streaming replication with `primary_conninfo`; logical replication
with publications and subscriptions; replication slot management; Patroni for
automated failover.

### Phase 4 — Backup & PITR (Lab 08)
Labs cover: `pgBackRest` full/incremental/differential backups; point-in-time
recovery; WAL archiving; RTO/RPO measurement against the live OrderFlow
workload.

### Phase 5 — Security (Lab 09)
Labs cover: Row-Level Security policies (employees see only their own rows;
managers see all); `pgcrypto` column encryption (salary, PII); `pgaudit` audit
trail on the payments table; SSL/TLS + client certificates; application roles
and `SET ROLE`.

### Phase 6 — Monitoring (Lab 11)
Labs cover: `pg_stat_statements`; `pg_stat_activity`; autovacuum tuning;
bloat analysis with `pgstattuple`; query planning regressions.

### Phase 7 — Extensions (Lab 10)
Labs cover: `pg_cron` (scheduled payment retry jobs); `pg_partman`
(automated partition creation); `pgvector` (product embedding search); FDW
(`postgres_fdw` across warehouse regions).

### Phase 8 — Cloud Migration (Lab 12)
Labs cover: AWS RDS / Aurora for PostgreSQL; Azure Database for PostgreSQL;
GCP Cloud SQL; pg_dump/restore; schema compatibility; managed-service
trade-offs vs. self-hosted.

## Table → Lab Traceability

| Table | Primary Lab Targets |
|-------|---------------------|
| `employees` | RLS (09), pgcrypto (09), pg_trgm/GIN (05) |
| `customers` | RLS (09), pgcrypto (09), GIN/JSONB (05) |
| `products` | FTS/pg_trgm (05), JSONB (05), pgvector (10), FDW (10) |
| `warehouses` | FDW (10), replication topology (07) |
| `orders` | RANGE partitioning (06), BRIN (05), replication (07), PITR (08), pgaudit (09) |
| `order_items` | Composite B-tree (05), FK integrity (04), join analysis (05) |
| `payments` | pgaudit (09), MVCC/isolation (04), pgcrypto (09), pg_cron (10) |
