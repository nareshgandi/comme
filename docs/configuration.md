# OrderFlow — Configuration Reference

All tunables for factories and workers live in `python/config/config.yaml`.
No value in any factory or worker module is hardcoded; everything flows through
the loader.

## Quick start

```bash
# 1. Copy the example config
cp python/config/config.yaml.example python/config/config.yaml

# 2. Set the database password (never stored in the YAML file)
export ORDERFLOW_DB_PASSWORD=your_db_password

# 3. Start workers
python python/workers/history_loader.py
python python/workers/order_generator.py
# … etc.
```

## File layout

| File | Purpose |
|------|---------|
| `python/config/config.yaml.example` | Checked-in canonical reference with full comments |
| `python/config/config.yaml` | Your live config — **git-ignored, never committed** |
| `python/config/loader.py` | Typed config dataclasses + `load_config()` function |

## Database password

The password is read exclusively from the `ORDERFLOW_DB_PASSWORD` environment
variable. If the variable is unset or empty, `load_config()` raises
`EnvironmentError` with a message naming exactly what is missing.

```bash
# Bash / Linux / macOS
export ORDERFLOW_DB_PASSWORD=secret

# Windows PowerShell
$env:ORDERFLOW_DB_PASSWORD = "secret"
```

The field `database.password` does **not** exist in `config.yaml` or
`config.yaml.example`. Do not add it.

## Bootstrap admin credentials

`bootstrap.py` requires a PostgreSQL superuser to create the database and the
app role on first run. These credentials are **only** needed during bootstrap —
the continuous workers use `ORDERFLOW_DB_PASSWORD` exclusively.

| Variable | Default | Purpose |
|----------|---------|---------|
| `ORDERFLOW_ADMIN_USER` | `postgres` | PostgreSQL superuser username |
| `ORDERFLOW_ADMIN_PASSWORD` | *(required)* | Superuser password |

```bash
export ORDERFLOW_ADMIN_USER=postgres
export ORDERFLOW_ADMIN_PASSWORD=<superuser-password>
```

Bootstrap uses these to:
1. Connect to the `postgres` maintenance database (not the app database).
2. `CREATE DATABASE <dbname>` if it does not already exist.
3. `CREATE ROLE <user> LOGIN PASSWORD <ORDERFLOW_DB_PASSWORD>` if it does not exist.
4. `GRANT CONNECT ON DATABASE <dbname> TO <user>`.
5. After schema application: `GRANT USAGE / SELECT / INSERT / UPDATE / DELETE`
   on all objects in `schema orderflow` to the app role.

Both variables are read by `bootstrap.py` directly from the environment — they
are never stored in `config.yaml`.

## Config sections

### `database`

| Key | Type | Description |
|-----|------|-------------|
| `host` | string | PostgreSQL server hostname |
| `port` | int | PostgreSQL port (default: 5432) |
| `dbname` | string | Database name |
| `user` | string | Database user |

Password is supplied via `ORDERFLOW_DB_PASSWORD` env var — not stored here.

---

### `workers.history_loader`

One-shot backfill script. All sizing values are tuneable without code changes.

| Key | Default | Description |
|-----|---------|-------------|
| `num_employees` | 200 | Employees to generate |
| `num_customers` | 100 000 | Customers to generate |
| `num_products` | 1 000 | Products to generate |
| `num_historical_orders` | 500 000 | Orders to generate |
| `chunk_size` | 5 000 | Orders per DB commit |
| `payment_failure_rate` | 0.08 | Fraction of payments that fail |
| `max_payment_retries` | 3 | Max failed attempts before abandonment |
| `status_distribution` | see YAML | Weighted map — must sum to 1.0 |

CLI overrides: `--employees`, `--customers`, `--products`, `--orders`, `--seed`.

---

### `workers.order_generator`

| Key | Default | Description |
|-----|---------|-------------|
| `batch_size` | 10 | Orders created per DB round-trip |
| `sleep_seconds` | 4.0 | Pause between batches |

Default gives ~2.5 orders/sec → ~200 MB/day of WAL.

---

### `workers.payment_processor`

| Key | Default | Description |
|-----|---------|-------------|
| `payment_failure_rate` | 0.08 | Gateway failure probability |
| `max_payment_retries` | 3 | Orders with ≥ this many FAILED payments are abandoned |
| `batch_size` | 50 | Orders processed per scan cycle |
| `sleep_seconds` | 2.0 | Pause when queue is empty |

---

### `workers.order_processor`

| Key | Default | Description |
|-----|---------|-------------|
| `min_age_processing_to_packed_s` | 30 | Seconds before PROCESSING → PACKED |
| `min_age_packed_to_shipped_s` | 60 | Seconds before PACKED → SHIPPED |
| `min_age_shipped_to_delivered_s` | 120 | Seconds before SHIPPED → DELIVERED |
| `min_age_delivered_for_return_s` | 180 | Seconds an order must be DELIVERED before return eligibility |
| `return_probability` | 0.05 | Fraction of eligible DELIVERED orders that become RETURNED |
| `batch_size` | 100 | Rows per transition scan |
| `sleep_seconds` | 5.0 | Pause when no eligible rows found |

---

### `workers.employee_updates`

| Key | Default | Description |
|-----|---------|-------------|
| `sample_size` | 20 | Employees mutated per cycle |
| `sleep_seconds` | 10.0 | Pause between cycles |
| `deactivate_prob` | 0.02 | Probability any active employee is deactivated |
| `dept_change_prob` | 0.10 | Probability of department transfer |
| `salary_min_delta` | "0.05" | Minimum fractional salary change (5 %) |
| `salary_max_delta` | "0.15" | Maximum fractional salary change (15 %) |

---

### `simulation.employees`

Controls `EmployeeFactory`. Keys must stay consistent with `simulation.employees.roles`.

| Key | Description |
|-----|-------------|
| `roles` | Ordered list of valid role names |
| `role_weights` | Sampling weights (same order as `roles`, sum to 1.0) |
| `role_departments` | role → department name mapping |
| `salary_ranges` | role → [min, max] annual USD salary |
| `min_tenure_days` | Earliest hire date offset from today |
| `max_tenure_days` | Latest hire date offset from today |

---

### `simulation.customers`

Controls `CustomerFactory`.

| Key | Description |
|-----|-------------|
| `loyalty_tiers` | Tier names in ascending order |
| `loyalty_weights` | Sampling weights (same order, sum to 1.0) |
| `countries` | Weighted list — duplicates produce higher sampling probability |
| `us_states` | Pool of US state abbreviations for US customers |

---

### `simulation.orders`

Controls `OrderFactory`.

| Key | Description |
|-----|-------------|
| `items_per_order_min` / `max` | Item count range per order |
| `qty_min` / `qty_max` | Quantity range per line item |
| `discount_prob` | Probability a line item receives a discount |
| `discount_min_pct` / `max_pct` | Discount percentage range (quoted strings) |
| `notes_prob` | Probability an order has a free-text note |
| `employee_assign_prob` | Probability an order is assigned to an employee |

---

### `simulation.payments`

Controls `PaymentFactory`. `failure_reasons` are used by workers to populate
`payments.failure_reason` when a gateway attempt fails.

| Key | Description |
|-----|-------------|
| `methods` | Ordered list of payment method names |
| `method_weights` | Sampling weights (same order, sum to 1.0) |
| `failure_reasons` | List of human-readable failure messages |

---

### `simulation.products`

Controls `ProductFactory`. One entry per category.

Each category entry has:

| Key | Type | Description |
|-----|------|-------------|
| `prefix` | string | 4-char SKU prefix |
| `subcategories` | list[str] | Subcategory pool |
| `price_range` | [str, str] | Min/max unit price (quoted for decimal precision) |
| `weight_range` | [float, float] | Min/max weight in kg |
| `names` | list[str] | Product name pool |
| `metadata_keys` | map | `null` = Faker-generated integer; list = random choice from list |

---

### `simulation.warehouses`

Controls `reference_data.WAREHOUSES`. One entry per physical fulfillment centre.

Region codes must match the FDW foreign-server names used in Lab 10.
`postal_code` must be quoted in YAML to prevent leading-zero loss.

---

## Loader API

```python
from python.config.loader import load_config, DEFAULT_CONFIG

# Production usage — call once at worker startup
cfg = load_config()                          # reads python/config/config.yaml
cfg = load_config(path=Path("/other/path"))  # explicit path

# Testing / library usage — same values as config.yaml defaults
cfg = DEFAULT_CONFIG
```

`DEFAULT_CONFIG` is a `Config` instance with the same values as the shipped
`config.yaml.example`. It does not require a config file on disk and does not
read `ORDERFLOW_DB_PASSWORD` — its `database.password` is an empty string.
