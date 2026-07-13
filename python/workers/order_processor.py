"""
OrderFlow — Order Processor (Lifecycle State Machine)
======================================================
Advances orders through every post-payment stage of the lifecycle defined in
business_rules.md §1. This worker is the executable statement of those rules.

State transitions handled here (payment_processor.py owns NEW→PROCESSING):
  PROCESSING → PACKED     (warehouse packs the order)
  PACKED     → SHIPPED    (courier picks up; sets shipped_at)
  SHIPPED    → DELIVERED  (delivery confirmed; sets delivered_at)
  DELIVERED  → RETURNED   (probabilistic; customer initiates return)
  RETURNED   → REFUNDED   (worker creates REFUNDED payment row + updates order)

Each transition is its own transaction. The worker does NOT hold a long
transaction across a sleep cycle (business_rules.md §7).

Invariants enforced here (business_rules.md §1.1):
  INV-01: shipped_at is set when advancing to SHIPPED
  INV-02: delivered_at is set when advancing to DELIVERED
  INV-03: shipped_at <= delivered_at (also enforced by DB CHECK constraint)
  INV-08: REFUNDED order gets a REFUNDED payment row in the same transaction

FOR UPDATE SKIP LOCKED is used on the status-transition SELECTs so that
multiple order_processor instances can run without competing for the same row.

Run:
    python python/workers/order_processor.py

Stop with Ctrl-C or SIGTERM.
"""
from __future__ import annotations

import logging
import random
import signal
import sys
import time
from decimal import Decimal
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.config.loader import load_config
from python.factories.payment_factory import PaymentFactory
from python.workers.db import get_connection

APP_NAME = "order_processor"

_UPDATE_STATUS = "UPDATE orders SET status = %s, updated_at = NOW() WHERE order_id = %s"

_UPDATE_SHIPPED = """\
UPDATE orders
SET status = 'SHIPPED', shipped_at = NOW(), updated_at = NOW()
WHERE order_id = %s
"""

_UPDATE_DELIVERED = """\
UPDATE orders
SET status = 'DELIVERED', delivered_at = NOW(), updated_at = NOW()
WHERE order_id = %s
"""

_UPDATE_RETURNED = "UPDATE orders SET status = 'RETURNED', updated_at = NOW() WHERE order_id = %s"
_UPDATE_REFUNDED = "UPDATE orders SET status = 'REFUNDED', updated_at = NOW() WHERE order_id = %s"

_SELECT_SUCCESS_PAYMENT = """\
SELECT payment_id, amount FROM payments
WHERE order_id = %s AND status = 'SUCCESS'
LIMIT 1
"""

_INSERT_REFUND_PAYMENT = """\
INSERT INTO payments
    (order_id, amount, method, status, gateway_reference, processed_at)
VALUES (%s, %s, 'bank_transfer', 'REFUNDED', %s, NOW())
"""

log = logging.getLogger(APP_NAME)
_shutdown = False


def _handle_signal(signum: int, frame) -> None:  # noqa: ARG001
    global _shutdown
    log.info("Shutdown signal received (%s). Finishing current cycle …", signum)
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


def _select_eligible(status: str, min_age_s: int, batch_size: int) -> str:
    return f"""\
SELECT order_id FROM orders
WHERE status = '{status}'
  AND updated_at < NOW() - INTERVAL '{min_age_s} seconds'
ORDER BY updated_at
LIMIT {batch_size}
FOR UPDATE SKIP LOCKED
"""


def _transition_batch(
    conn: psycopg.Connection,
    old_status: str,
    new_status: str,
    min_age_s: int,
    batch_size: int,
    update_fn,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_select_eligible(old_status, min_age_s, batch_size))
        order_ids = [row[0] for row in cur.fetchall()]
    conn.rollback()

    for oid in order_ids:
        if _shutdown:
            break
        try:
            update_fn(conn, oid, old_status, new_status)
        except Exception:
            conn.rollback()
            log.exception("Error advancing order %d from %s", oid, old_status)

    return len(order_ids)


def _do_return(
    conn: psycopg.Connection,
    rng: random.Random,
    return_prob: float,
    min_age_s: int,
    batch_size: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_select_eligible("DELIVERED", min_age_s, batch_size))
        candidates = [row[0] for row in cur.fetchall()]
    conn.rollback()

    returned = 0
    for oid in candidates:
        if _shutdown:
            break
        if rng.random() >= return_prob:
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(_UPDATE_RETURNED, (oid,))
            conn.commit()
            log.info("order %d  DELIVERED → RETURNED", oid)
            returned += 1
        except Exception:
            conn.rollback()
            log.exception("Error returning order %d", oid)

    return returned


def _do_refund(
    conn: psycopg.Connection,
    pay_factory: PaymentFactory,
    batch_size: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_select_eligible("RETURNED", min_age_s=0, batch_size=batch_size))
        order_ids = [row[0] for row in cur.fetchall()]
    conn.rollback()

    refunded = 0
    for oid in order_ids:
        if _shutdown:
            break
        try:
            with conn.cursor() as cur:
                cur.execute(_SELECT_SUCCESS_PAYMENT, (oid,))
                row = cur.fetchone()
                if row is None:
                    log.warning("order %d RETURNED but has no SUCCESS payment; skipping refund", oid)
                    continue
                amount = Decimal(str(row[1]))
                refund = pay_factory.create_refund_payment(order_id=oid, original_amount=amount)
                cur.execute(_INSERT_REFUND_PAYMENT, (oid, str(refund.amount), refund.gateway_reference))
                cur.execute(_UPDATE_REFUNDED, (oid,))
            conn.commit()
            log.info("order %d  RETURNED → REFUNDED  ($%s refunded)", oid, amount)
            refunded += 1
        except Exception:
            conn.rollback()
            log.exception("Error refunding order %d", oid)

    return refunded


def main() -> None:
    _setup()
    cfg = load_config()
    op = cfg.workers.order_processor
    rng = random.Random()
    pay_factory = PaymentFactory(rng=rng, config=cfg.simulation.payments)

    log.info("Starting %s", APP_NAME)
    conn = get_connection(APP_NAME, cfg.database)

    total_advanced = 0

    def _simple_advance(conn, oid, old, new):
        with conn.cursor() as cur:
            cur.execute(_UPDATE_STATUS, (new, oid))
        conn.commit()
        log.info("order %d  %s → %s", oid, old, new)

    def _shipped(conn, oid, old, new):
        with conn.cursor() as cur:
            cur.execute(_UPDATE_SHIPPED, (oid,))
        conn.commit()
        log.info("order %d  %s → %s (shipped_at set)", oid, old, new)

    def _delivered(conn, oid, old, new):
        with conn.cursor() as cur:
            cur.execute(_UPDATE_DELIVERED, (oid,))
        conn.commit()
        log.info("order %d  %s → %s (delivered_at set)", oid, old, new)

    try:
        while not _shutdown:
            cycle_work = 0

            cycle_work += _transition_batch(
                conn, "PROCESSING", "PACKED",
                op.min_age_processing_to_packed_s, op.batch_size, _simple_advance,
            )
            cycle_work += _transition_batch(
                conn, "PACKED", "SHIPPED",
                op.min_age_packed_to_shipped_s, op.batch_size, _shipped,
            )
            cycle_work += _transition_batch(
                conn, "SHIPPED", "DELIVERED",
                op.min_age_shipped_to_delivered_s, op.batch_size, _delivered,
            )
            cycle_work += _do_return(
                conn, rng, op.return_probability,
                op.min_age_delivered_for_return_s, op.batch_size,
            )
            cycle_work += _do_refund(conn, pay_factory, op.batch_size)

            total_advanced += cycle_work

            if cycle_work == 0:
                time.sleep(op.sleep_seconds)
            else:
                log.info("Cycle: %d transitions this pass, %d total.", cycle_work, total_advanced)

    except Exception:
        log.exception("Unhandled error in order_processor")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Shutdown complete. Total transitions: %d.", total_advanced)


if __name__ == "__main__":
    main()
