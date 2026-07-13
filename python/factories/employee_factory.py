"""
Factory for generating valid Employee domain objects.

No database access. No global random calls. Faker is seeded deterministically
from the injected random.Random instance so that a seeded RNG produces
reproducible employee data across runs.
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

from faker import Faker

from python.config.loader import DEFAULT_CONFIG, EmployeeSimConfig
from python.factories.models import Employee


class EmployeeFactory:
    """Generates Employee instances that satisfy the schema and business rules."""

    def __init__(
        self,
        rng: random.Random,
        faker: Faker | None = None,
        config: EmployeeSimConfig | None = None,
    ) -> None:
        """
        Args:
            rng:    Seeded (or live) random.Random instance.
            faker:  Optional pre-configured Faker instance.
            config: Employee simulation config. When None, uses DEFAULT_CONFIG.
        """
        self._rng = rng
        self._cfg: EmployeeSimConfig = config if config is not None else DEFAULT_CONFIG.simulation.employees
        if faker is None:
            faker = Faker()
            faker.seed_instance(rng.randint(0, 2**32 - 1))
        self._faker = faker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_employee(self, role: str | None = None) -> Employee:
        """Return a single valid Employee.

        Args:
            role: Force a specific role. When None, role is sampled from
                  config.roles according to config.role_weights.
        """
        if role is None:
            role = self._rng.choices(self._cfg.roles, weights=self._cfg.role_weights, k=1)[0]
        elif role not in self._cfg.roles:
            raise ValueError(f"Invalid role '{role}'. Must be one of {self._cfg.roles}.")

        salary_min, salary_max = self._cfg.salary_ranges[role]
        salary = Decimal(str(self._rng.randint(salary_min, salary_max)))

        tenure_days = self._rng.randint(self._cfg.min_tenure_days, self._cfg.max_tenure_days)
        hire_date = date.today() - timedelta(days=tenure_days)

        first_name = self._faker.first_name()
        last_name = self._faker.last_name()

        return Employee(
            first_name=first_name,
            last_name=last_name,
            email=self._unique_email(first_name, last_name),
            role=role,
            salary=salary,
            hire_date=hire_date,
            phone=self._faker.phone_number()[:30],
            department=self._cfg.role_departments[role],
            is_active=True,
            metadata=self._build_metadata(role),
        )

    def create_employees(self, count: int, role: str | None = None) -> list[Employee]:
        """Return a list of `count` Employee instances."""
        return [self.create_employee(role=role) for _ in range(count)]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _unique_email(self, first_name: str, last_name: str) -> str:
        suffix = self._rng.randint(1, 9999)
        domain = self._faker.free_email_domain()
        local = f"{first_name.lower()}.{last_name.lower()}{suffix}"
        return f"{local}@{domain}"[:255]

    def _build_metadata(self, role: str) -> dict:
        base: dict = {
            "skills": self._rng.sample(
                ["SQL", "Python", "Excel", "Forklift", "Customer Service",
                 "Inventory", "Scheduling", "Compliance", "Networking"],
                k=self._rng.randint(1, 3),
            ),
            "shift": self._rng.choice(["morning", "afternoon", "night"]),
        }
        if role in ("manager", "admin"):
            base["clearance_level"] = self._rng.choice(["standard", "elevated"])
        return base
