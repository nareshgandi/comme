"""
OrderFlow — Historical Data Loader
===================================
One-time backfill that populates ~1 GB of realistic historical data so that
every DBA lab starts with a non-trivial, already-evolved dataset.

Run once against a fresh database:
    python python/workers/history_loader.py

Re-run protection: the script detects existing data and refuses to run a
second time unless --force is passed.

    python python/workers/history_loader.py --force

Bulk-load strategy:
  - COPY (binary text format via psycopg3) for reference tables and order_items
    / payments, where we do not need PKs back immediately.  COPY is 10-50x
    faster than executemany for large batches and is the correct tool to teach
    from day one for DBA labs.
  - INSERT ... RETURNING for the orders table, because we need the generated
    order_id to backfill order_items.order_id before writing items.
  - Everything is batched in chunks of chunk_size rows per commit so the
    process can be killed and restarted without losing all progress (though
    a killed run may leave a partial batch — use --force to reload cleanly).

All sizing constants come from config.yaml (workers.history_loader section).
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.config.loader import Config, HistoryLoaderConfig, load_config
from python.factories.customer_factory import CustomerFactory
from python.factories.employee_factory import EmployeeFactory
from python.factories.models import OrderItem, Payment
from python.factories.order_factory import OrderFactory
from python.factories.payment_factory import PaymentFactory
from python.factories.product_factory import ProductFactory
from python.factories.reference_data import WAREHOUSES
from python.workers.db import get_connection

APP_NAME = "history_loader"

log = logging.getLogger(APP_NAME)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _random_past(rng: random.Random, max_days: int = 365) -> datetime:
    offset = timedelta(seconds=rng.uniform(0, max_days * 86400))
    return _now_utc() - offset


def _jsonb(d: dict | None) -> str | None:
    return json.dumps(d) if d is not None else None


def _check_existing_data(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM orders")
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# Reference data (COPY)
# ---------------------------------------------------------------------------

def _seed_employees(conn: psycopg.Connection, rng: random.Random, cfg: Config) -> list[int]:
    num = cfg.workers.history_loader.num_employees
    factory = EmployeeFactory(rng=rng, config=cfg.simulation.employees)
    employees = factory.create_employees(num)
    created_at = _random_past(rng, max_days=365 * 3)

    log.info("COPY %d employees …", num)
    with conn.cursor() as cur:
        with cur.copy(
            "COPY employees (first_name, last_name, email, phone, role, "
            "department, hire_date, salary, is_active, metadata, "
            "created_at, updated_at) FROM STDIN"
        ) as copy:
            for emp in employees:
                copy.write_row((
                    emp.first_name, emp.last_name, emp.email,
                    emp.phone, emp.role, emp.department,
                    emp.hire_date, str(emp.salary),
                    emp.is_active, _jsonb(emp.metadata),
                    created_at, created_at,
                ))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT employee_id FROM employees "
            "WHERE role IN ('warehouse_staff', 'manager')"
        )
        return [row[0] for row in cur.fetchall()]


def _seed_warehouses(conn: psycopg.Connection) -> list[int]:
    log.info("INSERT %d warehouses …", len(WAREHOUSES))
    ids: list[int] = []
    with conn.cursor() as cur:
        for wh in WAREHOUSES:
            cur.execute(
                "INSERT INTO warehouses "
                "(code, name, region, address_line1, city, state, "
                "country, postal_code, capacity_sqft, is_active) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING warehouse_id",
                (wh.code, wh.name, wh.region, wh.address_line1,
                 wh.city, wh.state, wh.country, wh.postal_code,
                 wh.capacity_sqft, wh.is_active),
            )
            ids.append(cur.fetchone()[0])
    conn.commit()
    return ids


def _seed_customers(conn: psycopg.Connection, rng: random.Random, cfg: Config) -> list[int]:
    num = cfg.workers.history_loader.num_customers
    factory = CustomerFactory(rng=rng, config=cfg.simulation.customers)
    batch_size = 5_000
    loaded = 0

    while loaded < num:
        n = min(batch_size, num - loaded)
        customers = factory.create_customers(n)
        with conn.cursor() as cur:
            with cur.copy(
                "COPY customers (first_name, last_name, email, phone, "
                "address_line1, address_line2, city, state, country, "
                "postal_code, loyalty_tier, is_active, metadata, "
                "created_at, updated_at) FROM STDIN"
            ) as copy:
                ts = _random_past(rng, max_days=365 * 2)
                for c in customers:
                    copy.write_row((
                        c.first_name, c.last_name, c.email, c.phone,
                        c.address_line1, c.address_line2,
                        c.city, c.state, c.country, c.postal_code,
                        c.loyalty_tier, c.is_active, _jsonb(c.metadata),
                        ts, ts,
                    ))
        conn.commit()
        loaded += n
        log.info("  customers: %d / %d", loaded, num)

    with conn.cursor() as cur:
        cur.execute("SELECT customer_id FROM customers")
        return [row[0] for row in cur.fetchall()]


def _seed_products(
    conn: psycopg.Connection, rng: random.Random, cfg: Config
) -> list[tuple[int, Decimal]]:
    num = cfg.workers.history_loader.num_products
    factory = ProductFactory(rng=rng, catalog=cfg.simulation.products)
    products = factory.create_products(num)
    ts = _random_past(rng, max_days=365)

    log.info("COPY %d products …", num)
    with conn.cursor() as cur:
        with cur.copy(
            "COPY products (sku, name, description, category, subcategory, "
            "unit_price, weight_kg, is_active, metadata, created_at, updated_at) "
            "FROM STDIN"
        ) as copy:
            for p in products:
                copy.write_row((
                    p.sku, p.name, p.description, p.category, p.subcategory,
                    str(p.unit_price),
                    str(p.weight_kg) if p.weight_kg is not None else None,
                    p.is_active, _jsonb(p.metadata), ts, ts,
                ))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT product_id, unit_price FROM products WHERE is_active = TRUE")
        return [(row[0], Decimal(str(row[1]))) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Historical order generation
# ---------------------------------------------------------------------------

def _build_order_timeline(
    rng: random.Random, created_at: datetime, final_status: str
) -> tuple[datetime | None, datetime | None, datetime]:
    shipped_at = delivered_at = None
    updated_at = created_at

    if final_status in ("PACKED", "SHIPPED", "DELIVERED", "RETURNED", "REFUNDED"):
        updated_at = created_at + timedelta(hours=rng.uniform(1, 24))
    if final_status in ("SHIPPED", "DELIVERED", "RETURNED", "REFUNDED"):
        shipped_at = updated_at + timedelta(hours=rng.uniform(2, 48))
        updated_at = shipped_at
    if final_status in ("DELIVERED", "RETURNED", "REFUNDED"):
        delivered_at = shipped_at + timedelta(days=rng.uniform(1, 7))
        updated_at = delivered_at
    if final_status in ("RETURNED", "REFUNDED"):
        updated_at = delivered_at + timedelta(days=rng.uniform(1, 14))
    if final_status == "REFUNDED":
        updated_at = updated_at + timedelta(days=rng.uniform(1, 3))

    return shipped_at, delivered_at, updated_at


def _build_payments(
    rng: random.Random,
    order_id: int,
    amount: Decimal,
    final_status: str,
    created_at: datetime,
    pay_methods: list,
    pay_weights: list,
    failure_reasons: list,
    payment_failure_rate: float,
    max_payment_retries: int,
) -> list[tuple]:
    if final_status == "NEW":
        return []

    method = rng.choices(pay_methods, weights=pay_weights, k=1)[0]
    prefix = {"credit_card": "CC", "debit_card": "DC", "paypal": "PP",
              "bank_transfer": "BT", "wallet": "WL"}.get(method, "TX")

    def gateway_ref() -> str:
        return f"{prefix}-{rng.getrandbits(64):016X}"

    rows: list[tuple] = []
    n_failures = 0
    if rng.random() < payment_failure_rate * 2:
        n_failures = rng.randint(1, min(2, max_payment_retries - 1))

    ts = created_at
    for _ in range(n_failures):
        rows.append((
            order_id, str(amount), method, "FAILED",
            gateway_ref(), rng.choice(failure_reasons),
            ts, ts, ts,
        ))
        ts += timedelta(minutes=rng.uniform(5, 60))

    rows.append((order_id, str(amount), method, "SUCCESS", gateway_ref(), None, ts, ts, ts))

    if final_status == "REFUNDED":
        rows.append((
            order_id, str(amount), "bank_transfer", "REFUNDED",
            f"REFUND-{rng.getrandbits(64):016X}", None,
            ts + timedelta(days=rng.uniform(1, 3)), ts, ts,
        ))

    return rows


_INSERT_ORDER = """\
INSERT INTO orders (
    customer_id, employee_id, warehouse_id, status, total_amount,
    shipping_address_line1, shipping_address_line2, shipping_city,
    shipping_state, shipping_country, shipping_postal_code,
    notes, shipped_at, delivered_at, created_at, updated_at
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
RETURNING order_id
"""

_COPY_ITEMS = (
    "COPY order_items "
    "(order_id, product_id, quantity, unit_price, discount_pct, created_at) "
    "FROM STDIN"
)

_COPY_PAYMENTS = (
    "COPY payments "
    "(order_id, amount, method, status, gateway_reference, "
    "failure_reason, processed_at, created_at, updated_at) "
    "FROM STDIN"
)


def _load_historical_orders(
    conn: psycopg.Connection,
    rng: random.Random,
    customer_ids: list[int],
    product_pool: list[tuple[int, Decimal]],
    warehouse_ids: list[int],
    employee_ids: list[int],
    cfg: Config,
    num_orders: int,
) -> None:
    hl = cfg.workers.history_loader
    sim = cfg.simulation

    statuses = list(hl.status_distribution.keys())
    weights  = list(hl.status_distribution.values())

    order_factory = OrderFactory(
        rng=rng,
        customer_ids=customer_ids,
        product_pool=product_pool,
        warehouse_ids=warehouse_ids,
        employee_ids=employee_ids,
        config=sim.orders,
    )

    total_loaded = 0
    t_start = time.monotonic()

    log.info("Generating %d historical orders in chunks of %d …", num_orders, hl.chunk_size)

    while total_loaded < num_orders:
        chunk_n = min(hl.chunk_size, num_orders - total_loaded)
        order_pairs = order_factory.create_orders(chunk_n)

        item_rows: list[tuple] = []
        pay_rows: list[tuple] = []

        with conn.cursor() as cur:
            for order, items in order_pairs:
                created_at = _random_past(rng, max_days=365)
                final_status = rng.choices(statuses, weights=weights, k=1)[0]
                shipped_at, delivered_at, updated_at = _build_order_timeline(
                    rng, created_at, final_status
                )

                cur.execute(_INSERT_ORDER, (
                    order.customer_id, order.employee_id, order.warehouse_id,
                    final_status, str(order.total_amount),
                    order.shipping_address_line1, order.shipping_address_line2,
                    order.shipping_city, order.shipping_state,
                    order.shipping_country, order.shipping_postal_code,
                    order.notes, shipped_at, delivered_at, created_at, updated_at,
                ))
                order_id = cur.fetchone()[0]

                for item in items:
                    item_rows.append((
                        order_id, item.product_id, item.quantity,
                        str(item.unit_price), str(item.discount_pct), created_at,
                    ))

                pay_rows.extend(
                    _build_payments(
                        rng, order_id, order.total_amount, final_status, created_at,
                        pay_methods=sim.payments.methods,
                        pay_weights=sim.payments.method_weights,
                        failure_reasons=sim.payments.failure_reasons,
                        payment_failure_rate=hl.payment_failure_rate,
                        max_payment_retries=hl.max_payment_retries,
                    )
                )

            with cur.copy(_COPY_ITEMS) as copy:
                for row in item_rows:
                    copy.write_row(row)

            with cur.copy(_COPY_PAYMENTS) as copy:
                for row in pay_rows:
                    copy.write_row(row)

        conn.commit()
        total_loaded += chunk_n

        elapsed = time.monotonic() - t_start
        rate = total_loaded / elapsed if elapsed > 0 else 0
        log.info("  orders loaded: %d / %d  (%.1f orders/sec)", total_loaded, num_orders, rate)

    elapsed = time.monotonic() - t_start
    log.info("Done. %d orders loaded in %.1fs (%.1f orders/sec).",
             total_loaded, elapsed, total_loaded / elapsed)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="OrderFlow historical data loader")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing data (dangerous — truncates the schema).")
    p.add_argument("--employees", type=int, default=None,
                   help="Override config workers.history_loader.num_employees.")
    p.add_argument("--customers", type=int, default=None,
                   help="Override config workers.history_loader.num_customers.")
    p.add_argument("--products",  type=int, default=None,
                   help="Override config workers.history_loader.num_products.")
    p.add_argument("--orders",    type=int, default=None,
                   help="Override config workers.history_loader.num_historical_orders.")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for reproducible loads.")
    return p.parse_args()


def main() -> None:
    _setup_logging()
    args = _parse_args()
    cfg = load_config()

    # CLI args override config values (None means "use config default")
    hl = cfg.workers.history_loader
    num_employees  = args.employees if args.employees is not None else hl.num_employees
    num_customers  = args.customers if args.customers is not None else hl.num_customers
    num_products   = args.products  if args.products  is not None else hl.num_products
    num_orders     = args.orders    if args.orders    is not None else hl.num_historical_orders

    # Patch config so downstream functions see consistent values
    hl.num_employees         = num_employees
    hl.num_customers         = num_customers
    hl.num_products          = num_products
    hl.num_historical_orders = num_orders

    rng = random.Random(args.seed)

    log.info("Connecting to database …")
    conn = get_connection(APP_NAME, cfg.database)

    existing = _check_existing_data(conn)
    if existing > 0 and not args.force:
        log.error(
            "Database already contains %d orders. "
            "Pass --force to overwrite (this will truncate all orderflow tables).",
            existing,
        )
        conn.close()
        sys.exit(1)

    if args.force and existing > 0:
        log.warning("--force: truncating all orderflow tables …")
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE orderflow.payments, orderflow.order_items, "
                "orderflow.orders, orderflow.products, orderflow.customers, "
                "orderflow.employees, orderflow.warehouses RESTART IDENTITY CASCADE"
            )
        conn.commit()
        log.info("Tables truncated.")

    t0 = time.monotonic()

    log.info("=== Phase 1: Reference data ===")
    employee_ids  = _seed_employees(conn, rng, cfg)
    warehouse_ids = _seed_warehouses(conn)
    customer_ids  = _seed_customers(conn, rng, cfg)
    product_pool  = _seed_products(conn, rng, cfg)

    log.info(
        "Reference data loaded: %d employees, %d warehouses, "
        "%d customers, %d products.",
        len(employee_ids), len(warehouse_ids),
        len(customer_ids), len(product_pool),
    )

    log.info("=== Phase 2: Historical orders ===")
    _load_historical_orders(
        conn, rng, customer_ids, product_pool, warehouse_ids, employee_ids,
        cfg=cfg, num_orders=num_orders,
    )

    conn.close()
    log.info("Total wall time: %.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    main()
