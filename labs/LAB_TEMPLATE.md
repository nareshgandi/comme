# Lab NN — [Topic Name]

<!-- =========================================================================
     HOW TO USE THIS TEMPLATE
     =========================================================================
     1. Copy this file to labs/<NN>_<slug>/README.md.
     2. Replace every line that starts with "<!-- INSTRUCTION" with real content.
     3. Remove ALL instruction comments before publishing the lab.
     4. Keep every section header in the order shown. Do not skip sections.
     5. If this topic does not fit the query-optimization shape (e.g. Patroni
        failover, PITR recovery), read the "Template Flexibility Notes" at the
        bottom before deciding to deviate.
     ========================================================================= -->

<!-- INSTRUCTION: Title = the PostgreSQL concept, specific enough to be Googled.
     Not "Indexes" but "B-tree Indexes and Query Plan Analysis."
     Not "Replication" but "Streaming Replication and Standby Promotion." -->

**Phase:** <!-- one of: Business Simulation | Storage Engineering | High Availability | Backup & PITR | Security | Monitoring | Extensions | Cloud Migration -->
**Prerequisites:** Lab NN-1 complete; all four OrderFlow workers running (`python bootstrap.py --status`).
**Estimated time:** <!-- 30 min | 1 hr | 2 hr -->

---

## 1. Business Problem

<!-- INSTRUCTION: State the problem entirely in OrderFlow business terms.
     Name the affected table, the observable symptom, and the business impact.
     NEVER open with "Let's learn about X" or "In this lab we will study Y."
     A real on-call engineer does not say "let's add an index."
     They say "customer support tickets are up because order history is slow."

     For operational/availability topics (HA, Backup), frame it as an incident:
       "The on-call alert fired — primary is unreachable, app is throwing
        connection errors. RTO target is 30 seconds."

     For security topics, frame it as a compliance or breach risk:
       "Audit found that any app user can read salary and PII columns directly —
        no access control below the schema level."

     The problem must be something a real user of OrderFlow would plausibly hit,
     given the live workload from the workers.

     Keep it to 2–4 sentences. -->

[Describe the business symptom here.]

---

## 2. Observe

<!-- INSTRUCTION: Show the student what to run FIRST — before touching anything.
     Goal: make the problem visible and measurable.

     Typical tools by topic:
       Query performance   → EXPLAIN (ANALYZE, BUFFERS), \timing
       Concurrency         → pg_stat_activity, pg_locks
       Replication         → pg_stat_replication, pg_stat_wal_receiver
       Bloat / VACUUM      → pgstattuple, pg_stat_user_tables
       Security            → \dp, pg_policies, SET ROLE + SELECT
       Backup / PITR       → pg_current_wal_lsn(), backup_label, pg_basebackup status

     Include the EXACT SQL or shell command. The student should be able to
     copy-paste and run it against the live OrderFlow database immediately.
     Do NOT explain the result yet — that belongs in section 6 (Explain). -->

Connect to the database:

```bash
psql -U orderflow -d orderflow
```

```sql
-- Run this to observe the problem before touching anything.
SET search_path = orderflow, public;

[observation query or command]
```

---

## 3. Measure (Baseline)

<!-- INSTRUCTION: Capture ONE concrete, reproducible number that proves the
     problem exists and will prove improvement later.

     Choose the right unit:
       Query performance   → duration in ms (from EXPLAIN ANALYZE or \timing)
       Buffer I/O          → "Buffers: shared read=N" from EXPLAIN (ANALYZE,BUFFERS)
       Replication lag     → bytes behind (pg_wal_lsn_diff) or seconds
       Table bloat         → MB of dead tuples (pgstattuple)
       Recovery time       → wall-clock seconds from failover trigger to first commit

     Name the number explicitly. Students who skip this step cannot claim
     improvement in section 5. Write it as a single labeled line:
       "Baseline: 847 ms (Seq Scan on orders, 48 230 buffer reads)" -->

**Baseline:** [metric name and value — e.g. "Query duration: 847 ms  |  Buffer reads: 48 230"]

---

## 4. Optimize

<!-- INSTRUCTION: Apply the PostgreSQL feature this lab teaches.
     ALL DDL, configuration changes, and commands must reference OrderFlow's
     actual table names, column names, and constraints from 001_initial_schema.sql.
     NEVER use generic names like `mytable`, `col1`, or `myindex`.

     If elevated privileges are required (superuser, replication role,
     pg_monitor), state this explicitly BEFORE the command block.

     If this change persists across labs (i.e. the output of this lab is the
     starting state for a later lab), say so here. Example:
       "This index persists — Lab 06 (Partitioning) assumes it exists."

     If the change is destructive (e.g. DROP TABLE, TRUNCATE) or irreversible
     (e.g. enabling pgaudit changes all future audit logs), say so with a
     WARNING block. -->

```sql
-- Apply the optimization, configuration change, or PostgreSQL feature here.
-- Use real OrderFlow column and table names.
```

---

## 5. Measure Again

<!-- INSTRUCTION: Re-run the EXACT same query or command from section 2.
     Show the new number in the same units as the baseline.
     Calculate the delta explicitly — do not leave it to the student to infer.

     If the improvement is smaller than expected, explain why here (not in
     section 6). Common causes: hot buffer cache, low cardinality, partial
     selectivity, OS-level caching, WAL amplification. -->

```sql
-- Re-run the section 2 observation query unchanged.
```

**After:** [same metric, new value — e.g. "Query duration: 12 ms  |  Buffer reads: 3"]
**Delta:** [e.g. "70× faster; buffer reads dropped 99.99%"]

---

## 6. Explain

<!-- INSTRUCTION: Answer WHY it worked at the PostgreSQL storage/execution level.
     Avoid tautology. "Faster because we added an index" is not an explanation.

     Cover the mechanism:
       B-tree index        → what the index structure skips vs. a Seq Scan
       Partition pruning   → which partitions the planner eliminates and why
       Streaming replication → how WAL records travel from primary to standby
       PITR               → what "applying WAL to a point in time" physically does
       RLS                → how the policy expression is rewritten into the query
       VACUUM             → what dead tuple cleanup does to the visibility map

     This section is the "teach-back checkpoint." A student should be able to
     close the lab and explain the mechanism to a colleague from memory. Write
     for that outcome, not for completeness. -->

[Explain the mechanism here.]

---

## 7. Cleanup / Reset Note

<!-- INSTRUCTION: Be EXPLICIT — does this lab's change persist, or should it
     be reverted? Labs stack on the same live database. Be precise.

     If the change persists as the new baseline:
       "No cleanup needed. The [change] created here is the starting state for
        Lab NN+1."

     If the change should be reverted:
       "Drop the demonstration index — it was created only to compare EXPLAIN
        plans and is not part of the Lab 05 baseline."
       Then provide the exact DROP / RESET / ALTER command.

     Never leave this section blank or as "TBD." -->

```sql
-- Cleanup command, or explain why no cleanup is needed.
```

---

## Further Reading

<!-- INSTRUCTION: 2–4 links, PostgreSQL official docs first. Remove this
     section entirely if there is nothing authoritative to cite.
     No vendor marketing, no paid courses. -->

- [PostgreSQL documentation — relevant page](https://www.postgresql.org/docs/current/)

---

<!--
=============================================================================
TEMPLATE FLEXIBILITY NOTES
=============================================================================
The 6-section shape (Business Problem → Observe → Measure → Optimize →
Measure Again → Explain) was designed for query-performance labs but holds
for every other topic in the sequence. Below are the mappings for the labs
that look least like "optimize a slow query" at first glance.

LAB 07 — Streaming Replication & Patroni Failover
  Business Problem  : "Primary is down. App is throwing connection errors.
                        RTO target: 30 s."
  Observe           : pg_stat_replication, replication lag, application
                       connection errors from logs
  Measure (Baseline): replication lag in bytes; time-to-first-error after
                       primary goes offline
  Optimize          : trigger Patroni failover; observe promotion
  Measure Again     : time-to-promotion (wall clock); first successful
                       INSERT after failover
  Explain           : how Patroni's Raft consensus elects a leader; why the
                       old primary becomes a replica; what clients must do to
                       reconnect

LAB 08 — Backup & PITR
  Business Problem  : "Someone ran TRUNCATE payments on production. We have
                        a pgBackRest base backup from last night and WAL
                        archives. How do we recover to 1 minute before the
                        truncate?"
  Observe           : confirm data is gone (COUNT); identify LSN or
                       timestamp of the accident from pg_wal
  Measure (Baseline): COUNT(*) FROM payments = 0; WAL timestamp of TRUNCATE
  Optimize          : pgBackRest restore to recovery target timestamp
  Measure Again     : COUNT(*) FROM payments = pre-accident value
  Explain           : what a base backup contains; how WAL replay reconstructs
                       every committed transaction up to the recovery target;
                       why "point in time" maps to a WAL LSN, not a clock tick

LAB 09 — Row-Level Security
  Business Problem  : "A compliance audit found that any app user with
                        SELECT on employees can read every salary row,
                        including those of employees in other departments."
  Observe           : \dp; SET ROLE orderflow; SELECT salary FROM employees
                       (see all rows — the problem is visible immediately)
  Measure (Baseline): rows returned by the SELECT = total employee count
  Optimize          : CREATE POLICY; ALTER TABLE ENABLE ROW LEVEL SECURITY
  Measure Again     : same SELECT now returns only the permitted subset
  Explain           : how PostgreSQL rewrites the query plan to append the
                       policy predicate; why BYPASSRLS is needed for admin

These mappings are not exhaustive. Before deciding a topic "doesn't fit the
template," write out the mapping above for that topic. In every case so far,
the shape holds — only the vocabulary of "Observe" and "Measure" changes.
=============================================================================
-->
