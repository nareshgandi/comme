# OrderFlow — Lab Guide

## What is OrderFlow?

OrderFlow is a continuously-running PostgreSQL workload that simulates an
online order fulfillment platform. Four Python workers run 24/7, generating
orders, processing payments, advancing the order lifecycle, and mutating
employee records. The result is a live database that behaves like production:
concurrent writers, dead tuples, growing tables, realistic query patterns.

Every DBA lab in this guide is taught against that same live database. You do
not spin up new data per topic and throw it away. You observe and improve the
same schema that previous labs already shaped — because that is how real
database engineering works.

---

## The Golden Rule

Every lab follows the same six-step shape, in this order:

```
Business Problem → Observe → Measure → Optimize → Measure Again → Explain
```

**Business Problem** — Every lab starts with something broken, slow, or at
risk in OrderFlow terms. Never "let's learn about B-tree indexes." Always
"customer support tickets are up because order history lookups take 3 seconds
on the production database."

**Observe** — Before touching anything, run the diagnostic. `EXPLAIN ANALYZE`,
`pg_stat_activity`, `pg_stat_replication`, `\timing` — whatever is appropriate
for the topic. Look first.

**Measure** — Capture one concrete baseline number. Latency in milliseconds.
Buffer reads. Replication lag in bytes. Table bloat in MB. Name it explicitly.
You cannot prove improvement without a baseline.

**Optimize** — Apply the PostgreSQL feature this lab teaches, against
OrderFlow's real schema and live data. No generic table names.

**Measure Again** — Re-run the exact same observation from step 2. Calculate
the delta. "Query dropped from 847 ms to 12 ms — 70×." If the improvement is
smaller than expected, explain why here, not later.

**Explain** — Answer WHY it worked at the storage and execution level. This is
the teach-back checkpoint: close the lab and explain the mechanism to a
colleague from memory. Tautology ("faster because we added an index") is not
an answer.

The template for this format lives in [`LAB_TEMPLATE.md`](LAB_TEMPLATE.md).
Copy it when writing a new lab. Do not deviate from the section order.

---

## Why One Dataset?

Most DBA courses spin up a fresh fake dataset per topic and throw it away.
OrderFlow never does that.

A B-tree index added in Lab 05 is still present when you measure replication
lag in Lab 07. The VACUUM tuning you do in Lab 11 affects the same bloat
pattern that `employee_updates.py` has been generating since Lab 03. The RLS
policies in Lab 09 restrict access to salary columns that the `pgcrypto`
encryption lab will later encrypt.

Each lab inherits the state left by previous labs. That compounding realism
is the point. It is also why the Cleanup / Reset Note section in every lab
is not optional — future labs depend on knowing exactly what state you leave
behind.

---

## Lab Sequence

Labs must be taken in order. Each lab assumes the previous lab's changes are
in place on the live database.

| Lab | Topic | Phase | Key PostgreSQL Feature |
|-----|-------|-------|------------------------|
| [01 — Architecture](01_architecture/README.md) | Repo & schema orientation | Foundation | Schema design, repo layout |
| [02 — Business Rules](02_business/README.md) | Order lifecycle narrative | Foundation | State machine, invariants |
| [03 — Python Workload](03_python/README.md) | Factories & workers | Foundation | COPY, FOR UPDATE SKIP LOCKED |
| [04 — PostgreSQL Core](04_postgresql/README.md) | Transactions, MVCC, triggers | Business Simulation | BEGIN/COMMIT, isolation levels |
| [05 — Indexes](05_indexes/README.md) | Query plan analysis & indexing | Storage Engineering | B-tree, BRIN, GIN, pg_trgm |
| [06 — Partitioning](06_partitioning/README.md) | Table partitioning & maintenance | Storage Engineering | RANGE partition, pg_partman |
| [07 — Replication](07_replication/README.md) | Streaming replication & failover | High Availability | pg_stat_replication, Patroni |
| [08 — Backup & PITR](08_backup/README.md) | Point-in-time recovery | Backup & PITR | pgBackRest, WAL replay |
| [09 — Security](09_security/README.md) | RLS, encryption, audit | Security | RLS, pgcrypto, pgaudit |
| [10 — Extensions](10_extensions/README.md) | pg_cron, FDW, pgvector | Extensions | Scheduled jobs, foreign data |
| [11 — Monitoring](11_monitoring/README.md) | Query stats & autovacuum | Monitoring | pg_stat_statements, bloat |
| [12 — Cloud](12_cloud/README.md) | Managed PostgreSQL migration | Cloud Migration | pg_dump, RDS, Aurora |

Labs 01–03 use a simplified write-up format (no Business Problem / Optimize
shape) because they document the build itself rather than teaching a DBA
technique. Starting from Lab 04, every lab follows the Golden Rule template
exactly.

---

## Prerequisites Before Starting Lab 01

1. OrderFlow is fully bootstrapped: `python bootstrap.py` completed without errors.
2. All four workers are running: `python bootstrap.py --status` shows RUNNING.
3. You have `psql` access to the `orderflow` database.
4. You have read `README.md` (repo root) and `business_rules.md`.
