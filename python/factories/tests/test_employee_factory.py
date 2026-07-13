"""Tests for EmployeeFactory."""
import random
from datetime import date
from decimal import Decimal

import pytest

from python.config.loader import DEFAULT_CONFIG
from python.factories.employee_factory import EmployeeFactory
from python.factories.models import Employee

_ROLES = DEFAULT_CONFIG.simulation.employees.roles


class TestEmployeeFactory:
    def _factory(self, seed: int = 42) -> EmployeeFactory:
        return EmployeeFactory(rng=random.Random(seed))

    # ------------------------------------------------------------------
    # Required fields populated
    # ------------------------------------------------------------------

    def test_required_fields_are_set(self):
        emp = self._factory().create_employee()
        assert emp.first_name
        assert emp.last_name
        assert emp.email
        assert emp.role in _ROLES
        assert isinstance(emp.salary, Decimal)
        assert emp.salary >= Decimal("0")
        assert isinstance(emp.hire_date, date)
        assert emp.is_active is True

    def test_employee_id_is_none_before_insert(self):
        """DB assigns the identity PK; factory must leave it as None."""
        emp = self._factory().create_employee()
        assert emp.employee_id is None

    def test_timestamps_are_none(self):
        """Timestamps are DB-defaulted; factory must not fabricate them."""
        emp = self._factory().create_employee()
        assert emp.created_at is None
        assert emp.updated_at is None

    # ------------------------------------------------------------------
    # Role constraint respected
    # ------------------------------------------------------------------

    def test_forced_role_is_respected(self):
        for role in _ROLES:
            emp = self._factory().create_employee(role=role)
            assert emp.role == role

    def test_invalid_role_raises(self):
        with pytest.raises(ValueError, match="Invalid role"):
            self._factory().create_employee(role="overlord")

    # ------------------------------------------------------------------
    # Determinism with seeded RNG
    # ------------------------------------------------------------------

    def test_seeded_rng_is_deterministic(self):
        e1 = EmployeeFactory(rng=random.Random(99)).create_employee()
        e2 = EmployeeFactory(rng=random.Random(99)).create_employee()
        assert e1.first_name == e2.first_name
        assert e1.last_name == e2.last_name
        assert e1.email == e2.email
        assert e1.role == e2.role
        assert e1.salary == e2.salary

    def test_different_seeds_produce_different_data(self):
        e1 = EmployeeFactory(rng=random.Random(1)).create_employee()
        e2 = EmployeeFactory(rng=random.Random(2)).create_employee()
        assert e1.email != e2.email

    # ------------------------------------------------------------------
    # Bulk creation
    # ------------------------------------------------------------------

    def test_create_employees_returns_correct_count(self):
        employees = self._factory().create_employees(10)
        assert len(employees) == 10
        assert all(isinstance(e, Employee) for e in employees)

    def test_bulk_emails_are_unique(self):
        employees = self._factory(seed=7).create_employees(50)
        emails = [e.email for e in employees]
        assert len(emails) == len(set(emails)), "Duplicate emails in bulk create"
