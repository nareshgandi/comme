# Lab 06 — Partitioning

**Status:** Not Started (prerequisite: Lab 05)

RANGE partition `orders` on `created_at` using `pg_partman` for automated
partition creation. Convert the plain table while the workload is live.
Contrast BRIN effectiveness before and after partitioning.
Demonstrate `pg_repack` for online table reorganization.
