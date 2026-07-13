"""
OrderFlow — Employee Updates Worker
=====================================
Continuously applies random mutations to existing employee rows: salary
adjustments, phone/department changes, metadata updates, and occasional
is_active toggles (simulated churn/re-hire).

WHY THIS WORKER EXISTS — do not simplify away:
  The order pipeline (order_generator, order_processor, payment_processor)
  is almost entirely INSERT-heavy.  PostgreSQL labs on VACUUM, table bloat,
  autovacuum tuning, and pgaudit audit logging all require a live stream of
  UPDATEs to observe realistic dead-tuple accumulation and row versioning.
  Without this worker, those labs would have nothing interesting to measure.
  RLS Lab 09 also tests that UPDATE policies work under concurrent mutations.

Mutations applied (randomly chosen per employee):
  1. SALARY_BUMP   — ±5–15 % salary change (mimics annual review cycle)
  2. PHONE_CHANGE  — new phone number (contact info update)
  3. METADATA_ADD  — append a new skill tag to the metadata JSONB
  4. DEPT_TRANSFER — department reassignment (rare: dept_change_prob %)
  5. DEACTIVATE    — is_active = FALSE (rare: deactivate_prob % of active)
  6. REACTIVATE    — is_active = TRUE  (100 % of inactive, to keep pool healthy)

Run:
    python python/workers/employee_updates.py

Stop with Ctrl-C or SIGTERM.
"""
from __future__ import annotations

import json
import logging
import random
import signal
import sys
import time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python.config.loader import load_config
from python.workers.db import get_connection

APP_NAME = "employee_updates"

_EXTRA_SKILLS = [
    "Python", "Bash", "Linux", "Networking", "PostgreSQL", "Excel",
    "Safety Certification", "Forklift", "First Aid", "Compliance",
    "Scheduling", "Budgeting", "Team Lead",
]

_DEPARTMENTS = ["Fulfillment", "Logistics", "Finance", "Operations", "Administration"]

_SELECT_SAMPLE = """\
SELECT employee_id, salary, phone, department, is_active, metadata
FROM employees
ORDER BY RANDOM()
LIMIT %s
FOR UPDATE SKIP LOCKED
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


def _mutate_salary(
    salary: Decimal,
    rng: random.Random,
    salary_min_delta: Decimal,
    salary_max_delta: Decimal,
) -> Decimal:
    pct = Decimal(str(rng.uniform(float(salary_min_delta), float(salary_max_delta))))
    direction = rng.choice([1, -1])
    new_salary = salary * (Decimal("1") + direction * pct)
    new_salary = max(Decimal("0"), new_salary).quantize(Decimal("0.01"), ROUND_HALF_UP)
    return new_salary


def _mutate_metadata(metadata: dict | None, rng: random.Random) -> dict:
    meta = dict(metadata) if metadata else {}
    skills: list = meta.get("skills", [])
    new_skill = rng.choice(_EXTRA_SKILLS)
    if new_skill not in skills:
        skills.append(new_skill)
    meta["skills"] = skills
    return meta


def _run_cycle(
    conn: psycopg.Connection,
    rng: random.Random,
    sample_size: int,
    deactivate_prob: float,
    dept_change_prob: float,
    salary_min_delta: Decimal,
    salary_max_delta: Decimal,
) -> int:
    with conn.cursor() as cur:
        cur.execute(_SELECT_SAMPLE, (sample_size,))
        rows = cur.fetchall()
    conn.rollback()

    updated = 0
    for emp_id, salary_raw, phone, department, is_active, metadata_raw in rows:
        if _shutdown:
            break

        salary   = Decimal(str(salary_raw))
        metadata = metadata_raw if isinstance(metadata_raw, dict) else (
            json.loads(metadata_raw) if metadata_raw else {}
        )

        if not is_active:
            sql  = "UPDATE employees SET is_active = TRUE, updated_at = NOW() WHERE employee_id = %s"
            args = (emp_id,)
            action = "reactivated"
        elif rng.random() < deactivate_prob:
            sql  = "UPDATE employees SET is_active = FALSE, updated_at = NOW() WHERE employee_id = %s"
            args = (emp_id,)
            action = "deactivated"
        elif rng.random() < dept_change_prob:
            new_dept = rng.choice([d for d in _DEPARTMENTS if d != department])
            sql  = "UPDATE employees SET department = %s, updated_at = NOW() WHERE employee_id = %s"
            args = (new_dept, emp_id)
            action = f"dept → {new_dept}"
        else:
            if rng.random() < 0.5:
                new_salary = _mutate_salary(salary, rng, salary_min_delta, salary_max_delta)
                sql  = "UPDATE employees SET salary = %s, updated_at = NOW() WHERE employee_id = %s"
                args = (str(new_salary), emp_id)
                action = f"salary {salary} → {new_salary}"
            elif rng.random() < 0.5:
                new_meta = _mutate_metadata(metadata, rng)
                sql  = "UPDATE employees SET metadata = %s, updated_at = NOW() WHERE employee_id = %s"
                args = (json.dumps(new_meta), emp_id)
                action = "metadata skill added"
            else:
                new_phone = f"+1-{rng.randint(200,999)}-{rng.randint(100,999)}-{rng.randint(1000,9999)}"
                sql  = "UPDATE employees SET phone = %s, updated_at = NOW() WHERE employee_id = %s"
                args = (new_phone, emp_id)
                action = "phone updated"

        try:
            with conn.cursor() as cur:
                cur.execute(sql, args)
            conn.commit()
            log.debug("employee %d: %s", emp_id, action)
            updated += 1
        except Exception:
            conn.rollback()
            log.exception("Error mutating employee %d", emp_id)

    return updated


def main() -> None:
    _setup()
    cfg = load_config()
    eu = cfg.workers.employee_updates
    rng = random.Random()

    log.info(
        "Starting %s — sample=%d  sleep=%.1fs  "
        "deactivate_prob=%.0f%%  dept_change_prob=%.0f%%",
        APP_NAME, eu.sample_size, eu.sleep_seconds,
        eu.deactivate_prob * 100, eu.dept_change_prob * 100,
    )

    conn = get_connection(APP_NAME, cfg.database)
    total_updated = 0

    try:
        while not _shutdown:
            n = _run_cycle(
                conn, rng,
                sample_size=eu.sample_size,
                deactivate_prob=eu.deactivate_prob,
                dept_change_prob=eu.dept_change_prob,
                salary_min_delta=eu.salary_min_delta,
                salary_max_delta=eu.salary_max_delta,
            )
            total_updated += n
            log.info("Mutation cycle: %d employees updated (%d total).", n, total_updated)
            time.sleep(eu.sleep_seconds)
    except Exception:
        log.exception("Unhandled error in employee_updates")
        conn.rollback()
        raise
    finally:
        conn.close()
        log.info("Shutdown complete. Total mutations: %d.", total_updated)


if __name__ == "__main__":
    main()
