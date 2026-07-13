"""Tests for CustomerFactory."""
import random
from decimal import Decimal

import pytest

from python.config.loader import DEFAULT_CONFIG
from python.factories.customer_factory import CustomerFactory
from python.factories.models import Customer

_LOYALTY_TIERS = DEFAULT_CONFIG.simulation.customers.loyalty_tiers


class TestCustomerFactory:
    def _factory(self, seed: int = 42) -> CustomerFactory:
        return CustomerFactory(rng=random.Random(seed))

    # ------------------------------------------------------------------
    # Required fields populated
    # ------------------------------------------------------------------

    def test_required_fields_are_set(self):
        c = self._factory().create_customer()
        assert c.first_name
        assert c.last_name
        assert c.email
        assert c.loyalty_tier in _LOYALTY_TIERS
        assert c.country  # NOT NULL in schema
        assert c.is_active is True

    def test_customer_id_is_none_before_insert(self):
        c = self._factory().create_customer()
        assert c.customer_id is None

    def test_timestamps_are_none(self):
        c = self._factory().create_customer()
        assert c.created_at is None
        assert c.updated_at is None

    # ------------------------------------------------------------------
    # Loyalty tier distribution
    # ------------------------------------------------------------------

    def test_loyalty_tier_is_valid(self):
        factory = self._factory(seed=123)
        for _ in range(100):
            c = factory.create_customer()
            assert c.loyalty_tier in _LOYALTY_TIERS

    def test_bronze_is_most_common_tier(self):
        """Over 500 samples, bronze should be the plurality (weight=0.60)."""
        factory = CustomerFactory(rng=random.Random(0))
        tiers = [factory.create_customer().loyalty_tier for _ in range(500)]
        assert tiers.count("bronze") > tiers.count("gold")
        assert tiers.count("bronze") > tiers.count("platinum")

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------

    def test_seeded_rng_is_deterministic(self):
        c1 = CustomerFactory(rng=random.Random(77)).create_customer()
        c2 = CustomerFactory(rng=random.Random(77)).create_customer()
        assert c1.email == c2.email
        assert c1.loyalty_tier == c2.loyalty_tier
        assert c1.country == c2.country

    # ------------------------------------------------------------------
    # Bulk
    # ------------------------------------------------------------------

    def test_create_customers_returns_correct_count(self):
        customers = self._factory().create_customers(20)
        assert len(customers) == 20
        assert all(isinstance(c, Customer) for c in customers)

    def test_bulk_emails_are_unique(self):
        customers = CustomerFactory(rng=random.Random(5)).create_customers(100)
        emails = [c.email for c in customers]
        assert len(emails) == len(set(emails)), "Duplicate emails in bulk create"
