"""
OrderFlow — Order Generator
============================
Continuously creates new orders (status=NEW) at a steady rate, forever.

Target throughput: ~200 MB/day of combined order + order_items + payment data.

Derivation (documented so the number is not magic):
  Rough row sizes:
    orders       ~450 B
    order_items  ~120 B × 3.5 avg items = ~420 B
    payments     ~200 B
  Total per order event ≈ 1,070 B ≈ 1.05 KB

  200 MB/day = 200 × 1024² B / 86400 s ≈ 2,428 B/s
  2,428 / 1,070 ≈ 2.3 new orders/second

  Default BATCH_SIZE=10, SLEEP_SECONDS=4.0 → 10/4 = 2.5 orders/sec  ✓

Run:
    python python/workers/order_generator.py

Stop with Ctrl-C or SIGTERM.  The current batch always commits before exit.
"""
from __future__ import annotations

import logging
import random
import signal
import sys
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.config.loader import load_config
from python.factories.order_factory import OrderFactory
from python.workers.db import get_connection

APP_NAME = "order_generator"

_INSERT_ORDER = """\
INSERT INTO orders (
    customer_id, employee_id, warehouse_id, status, total_amount,
    shipping_address_line1, shipping_address_line2, shipping_city,
    shipping_state, shipping_country, shipping_postal_code,
    notes
) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
RETURNING order_id
"""

_COPY_ITEMS = (
    "COPY order_items "
    "(order_id, product_id, quantity, unit_price, discount_pct) "
    "FROM STDIN"
)

_SELECT_POOLS = """\
SELECT customer_id FROM customers WHERE is_active = TRUE ORDER BY RANDOM() LIMIT 500;
SELECT product_id, unit_price FROM products WHERE is_active = TRUE;
SELECT warehouse_id FROM warehouses WHERE is_active = TRUE;
SELECT employee_id FROM employees WHERE is_active = TRUE AND role IN ('warehouse_staff','manager');
"""

log = logging.getLogger(APP_NAME)
_shutdown = False


def _handle_signal(signum: int, frame) -> None:  # noqa: ARG001
    global _shutdown
    log.info("Shutdown signal received (%s). Finishing current batch …", signum)
    _shutdown = True


def _setup() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )
    signal.signal(signal.SIGINT,  _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)


def _load_pools(conn: psycopg.Connection) -> tuple[list, list, list, list]:
    with conn.cursor() as cur:
        cur.execute("SELECT customer_id FROM customers WHERE is_active = TRUE ORDER BY RANDOM() LIMIT 500")
        customer_ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT product_id, unit_price FROM products WHERE is_active = TRUE")
        product_pool = [(r[0], r[1]) for r in cur.fetchall()]
        cur.execute("SELECT warehouse_id FROM warehouses WHERE is_active = TRUE")
        warehouse_ids = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT employee_id FROM employees WHERE is_active = TRUE AND role IN ('warehouse_staff','manager')")
        employee_ids = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return customer_ids, product_pool, warehouse_ids, employee_ids


def _create_batch(
    conn: psycopg.Connection,
    factory: OrderFactory,
    batch_size: int,
) -> int:
    orders_and_items = factory.create_orders(batch_size)
    created = 0
    for order, items in orders_and_items:
        if _shutdown:
            break
        with conn.cursor() as cur:
            cur.execute(_INSERT_ORDER, (
                order.customer_id, order.employee_id, order.warehouse_id,
                order.status, str(order.total_amount),
                order.shipping_address_line1, None,
                order.shipping_city, order.shipping_state,
                order.shipping_country, order.shipping_postal_code,
                order.notes,
            ))
            order_id = cur.fetchone()[0]
            with cur.copy(_COPY_ITEMS) as copy:
                for item in items:
                    copy.write_row((
                        order_id,
                        item.product_id,
                        item.quantity,
                        str(item.unit_price),
                        str(item.discount_pct),
                    ))
        conn.commit()
        created += 1
    return created


def main() -> None:
    _setup()
    cfg = load_config()
    w_cfg = cfg.workers.order_generator
    rng = random.Random()

    log.info("Starting %s — batch=%d  sleep=%.1fs", APP_NAME, w_cfg.batch_size, w_cfg.sleep_seconds)

    conn = get_connection(APP_NAME, cfg.database)
    customer_ids, product_pool, warehouse_ids, employee_ids = _load_pools(conn)

    factory = OrderFactory(
        rng=rng,
        customer_ids=customer_ids,
        product_pool=product_pool,
        warehouse_ids=warehouse_ids,
        employee_ids=employee_ids,
        config=cfg.simulation.orders,
    )

    total_created = 0

    try:
        while not _shutdown:
            n = _create_batch(conn, factory, w_cfg.batch_size)
            total_created += n
            if n > 0:
                log.info("Batch: %d orders created (%d total).", n, total_created)
            if not _shutdown:
                time.sleep(w_cfg.sleep_seconds)
    except Exception:
        log.exception("Unhandled error in order_generator")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Shutdown complete. Total orders created: %d.", total_created)


if __name__ == "__main__":
    main()
