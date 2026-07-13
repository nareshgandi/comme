"""Tests for PaymentFactory."""
import random
from decimal import Decimal

import pytest

from python.config.loader import DEFAULT_CONFIG
from python.factories.payment_factory import PaymentFactory
from python.factories.models import Payment

_METHODS = DEFAULT_CONFIG.simulation.payments.methods


class TestPaymentFactory:
    def _factory(self, seed: int = 42) -> PaymentFactory:
        return PaymentFactory(rng=random.Random(seed))

    # ------------------------------------------------------------------
    # Required fields
    # ------------------------------------------------------------------

    def test_required_fields_are_set(self):
        p = self._factory().create_payment(order_id=1, amount=Decimal("99.99"))
        assert p.order_id == 1
        assert p.amount == Decimal("99.99")
        assert p.method in _METHODS
        assert p.status == "PENDING"
        assert p.gateway_reference  # pre-generated
        assert p.payment_id is None

    def test_status_is_pending(self):
        """business_rules.md §2.1 — factories produce PENDING; workers update."""
        p = self._factory().create_payment(order_id=5, amount=Decimal("50.00"))
        assert p.status == "PENDING"

    def test_timestamps_are_none(self):
        p = self._factory().create_payment(order_id=1, amount=Decimal("10.00"))
        assert p.created_at is None
        assert p.updated_at is None
        assert p.processed_at is None

    # ------------------------------------------------------------------
    # FK preservation
    # ------------------------------------------------------------------

    def test_order_id_is_preserved(self):
        p = self._factory().create_payment(order_id=42, amount=Decimal("1.00"))
        assert p.order_id == 42

    def test_amount_is_preserved(self):
        amount = Decimal("123.45")
        p = self._factory().create_payment(order_id=1, amount=amount)
        assert p.amount == amount

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def test_zero_amount_raises(self):
        with pytest.raises(ValueError, match="amount must be > 0"):
            self._factory().create_payment(order_id=1, amount=Decimal("0"))

    def test_negative_amount_raises(self):
        with pytest.raises(ValueError, match="amount must be > 0"):
            self._factory().create_payment(order_id=1, amount=Decimal("-5.00"))

    # ------------------------------------------------------------------
    # Refund payment
    # ------------------------------------------------------------------

    def test_refund_payment_status_is_refunded(self):
        """business_rules.md §2.2 — refund row has status=REFUNDED."""
        p = self._factory().create_refund_payment(order_id=7, original_amount=Decimal("200.00"))
        assert p.status == "REFUNDED"
        assert p.order_id == 7
        assert p.amount == Decimal("200.00")
        assert p.gateway_reference.startswith("REFUND-")

    def test_refund_zero_amount_raises(self):
        with pytest.raises(ValueError, match="amount must be > 0"):
            self._factory().create_refund_payment(order_id=1, original_amount=Decimal("0"))

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------

    def test_seeded_rng_is_deterministic(self):
        p1 = PaymentFactory(rng=random.Random(33)).create_payment(1, Decimal("50.00"))
        p2 = PaymentFactory(rng=random.Random(33)).create_payment(1, Decimal("50.00"))
        assert p1.method == p2.method
        assert p1.gateway_reference == p2.gateway_reference

    # ------------------------------------------------------------------
    # Method distribution
    # ------------------------------------------------------------------

    def test_method_is_always_valid(self):
        factory = self._factory(seed=0)
        for _ in range(100):
            p = factory.create_payment(order_id=1, amount=Decimal("10.00"))
            assert p.method in _METHODS
