# python/config/

**Placeholder — Milestone 4.**

Configuration loader that reads `config.yaml` from the project root and
exposes a typed, validated `Config` object to all factories and workers.

No value in any factory or worker may be hardcoded. Every tunable parameter
— DB connection, batch sizes, sleep intervals, success rates, counts — must
flow through this module.
