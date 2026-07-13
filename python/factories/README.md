# python/factories/

**Placeholder — Milestone 3.**

Python data factories for all 7 tables:
`employees`, `customers`, `products`, `warehouses`,
`orders`, `order_items`, `payments`.

Each factory generates a single realistic row using the `Faker` library and
inserts it via a parameterized `psycopg2` query. No SQL scripts.
All configurable counts (number of customers, products, etc.) come from
`config.yaml`.
