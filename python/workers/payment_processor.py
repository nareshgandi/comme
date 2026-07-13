"""
OrderFlow — Payment Processor
===============================
Watches for orders in status=NEW, attempts a payment for each, and advances
the order to PROCESSING on success (or leaves it in NEW on failure).

Transaction model (per business_rules.md §2.1):
  1. SELECT a batch of NEW orders that have fewer than max_payment_retries
     payment attempts recorded.
  2. For each order:
       a. INSERT payments row with status=PENDING.
       b. Simulate the gateway (random draw against payment_failure_rate).
       c. On SUCCESS:
            UPDATE payments SET status='SUCCESS', processed_at=NOW().
            UPDATE orders  SET status='PROCESSING'.
       d. On FAILURE:
            UPDATE payments SET status='FAILED', failure_reason=<reason>.
            Order remains NEW (eligible for retry until retries exhausted).
  3. COMMIT per order (each payment attempt is its own transaction).

Run:
    python python/workers/payment_processor.py

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

APP_NAME = "payment_processor"

_INSERT_PAYMENT = """\
INSERT INTO payments
    (order_id, amount, method, status, gateway_reference)
VALUES (%s, %s, %s, 'PENDING', %s)
RETURNING payment_id
"""

_UPDATE_PAYMENT_SUCCESS = """\
UPDATE payments
SET status = 'SUCCESS', processed_at = NOW(), updated_at = NOW()
WHERE payment_id = %s
"""

_UPDATE_PAYMENT_FAILED = """\
UPDATE payments
SET status = 'FAILED', failure_reason = %s, updated_at = NOW()
WHERE payment_id = %s
"""

_UPDATE_ORDER_PROCESSING = """\
UPDATE orders SET status = 'PROCESSING', updated_at = NOW()
WHERE order_id = %s
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


def _select_pending(max_retries: int, batch_size: int) -> str:
    return f"""\
SELECT o.order_id, o.total_amount
FROM orders o
WHERE o.status = 'NEW'
  AND (
      SELECT COUNT(*) FROM payments p
      WHERE p.order_id = o.order_id AND p.status = 'FAILED'
  ) < {max_retries}
ORDER BY o.created_at
LIMIT {batch_size}
FOR UPDATE SKIP LOCKED
"""


def _process_one(
    conn: psycopg.Connection,
    cur: psycopg.Cursor,
    order_id: int,
    total_amount: Decimal,
    factory: PaymentFactory,
    rng: random.Random,
    failure_rate: float,
    failure_reasons: list,
) -> str:
    payment = factory.create_payment(order_id=order_id, amount=total_amount)

    cur.execute(_INSERT_PAYMENT, (
        payment.order_id,
        str(payment.amount),
        payment.method,
        payment.gateway_reference,
    ))
    payment_id = cur.fetchone()[0]

    if rng.random() >= failure_rate:
        cur.execute(_UPDATE_PAYMENT_SUCCESS, (payment_id,))
        cur.execute(_UPDATE_ORDER_PROCESSING, (order_id,))
        log.info(
            "order %d  NEW → PROCESSING  (payment %d SUCCESS  %s  $%s)",
            order_id, payment_id, payment.method, total_amount,
        )
        return "SUCCESS"
    else:
        reason = rng.choice(failure_reasons)
        cur.execute(_UPDATE_PAYMENT_FAILED, (reason, payment_id))
        log.info("order %d  payment %d FAILED: %s", order_id, payment_id, reason)
        return "FAILED"


def _run_cycle(
    conn: psycopg.Connection,
    factory: PaymentFactory,
    rng: random.Random,
    failure_rate: float,
    failure_reasons: list,
    max_retries: int,
    batch_size: int,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_select_pending(max_retries, batch_size))
        eligible = [(row[0], Decimal(str(row[1]))) for row in cur.fetchall()]
    conn.rollback()

    for order_id, total_amount in eligible:
        if _shutdown:
            break
        try:
            with conn.cursor() as cur:
                _process_one(conn, cur, order_id, total_amount, factory, rng,
                             failure_rate, failure_reasons)
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception("Error processing payment for order %d", order_id)

    return len(eligible)


def main() -> None:
    _setup()
    cfg = load_config()
    w_cfg = cfg.workers.payment_processor
    rng = random.Random()
    factory = PaymentFactory(rng=rng, config=cfg.simulation.payments)
    failure_reasons = cfg.simulation.payments.failure_reasons

    log.info(
        "Starting %s — failure_rate=%.0f%%  max_retries=%d  batch=%d",
        APP_NAME, w_cfg.payment_failure_rate * 100, w_cfg.max_payment_retries, w_cfg.batch_size,
    )

    conn = get_connection(APP_NAME, cfg.database)

    try:
        while not _shutdown:
            processed = _run_cycle(
                conn, factory, rng,
                failure_rate=w_cfg.payment_failure_rate,
                failure_reasons=failure_reasons,
                max_retries=w_cfg.max_payment_retries,
                batch_size=w_cfg.batch_size,
            )
            if processed == 0:
                time.sleep(w_cfg.sleep_seconds)
            else:
                log.info("Cycle complete: %d orders processed.", processed)
    except Exception:
        log.exception("Unhandled error in payment_processor")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
