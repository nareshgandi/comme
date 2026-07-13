"""Tests for OrderFactory."""
import random
from decimal import Decimal

import pytest

from python.factories.order_factory import OrderFactory
from python.factories.models import Order, OrderItem


def _make_factory(seed: int = 42, n_customers: int = 5, n_products: int = 10) -> OrderFactory:
    customer_ids = list(range(1, n_customers + 1))
    product_pool = [(i, Decimal(str(i * 10))) for i in range(1, n_products + 1)]
    warehouse_ids = [1, 2, 3]
    employee_ids = [10, 11, 12]
    return OrderFactory(
        rng=random.Random(seed),
        customer_ids=customer_ids,
        product_pool=product_pool,
        warehouse_ids=warehouse_ids,
        employee_ids=employee_ids,
    )


class TestOrderFactory:

    # ------------------------------------------------------------------
    # Output shape
    # ------------------------------------------------------------------

    def test_returns_order_and_items_tuple(self):
        order, items = _make_factory().create_order()
        assert isinstance(order, Order)
        assert isinstance(items, list)
        assert len(items) >= 1
        assert all(isinstance(i, OrderItem) for i in items)

    def test_order_status_is_new(self):
        """business_rules.md §1 — factories always produce NEW orders."""
        order, _ = _make_factory().create_order()
        assert order.status == "NEW"

    def test_order_id_is_none_before_insert(self):
        order, _ = _make_factory().create_order()
        assert order.order_id is None

    def test_item_order_id_is_none_before_insert(self):
        """Workers backfill order_id after inserting the order row."""
        _, items = _make_factory().create_order()
        for item in items:
            assert item.order_id is None

    def test_item_order_item_id_is_none_before_insert(self):
        _, items = _make_factory().create_order()
        for item in items:
            assert item.order_item_id is None

    def test_timestamps_are_none(self):
        order, items = _make_factory().create_order()
        assert order.created_at is None
        assert order.updated_at is None
        for item in items:
            assert item.created_at is None

    # ------------------------------------------------------------------
    # Business rule compliance (INV-06, INV-09, INV-10)
    # ------------------------------------------------------------------

    def test_total_amount_equals_sum_of_line_totals(self):
        """INV-06: orders.total_amount must equal SUM(order_items.line_total)."""
        factory = _make_factory(seed=7)
        for _ in range(20):
            order, items = factory.create_order()
            expected = sum(i.line_total for i in items)
            assert order.total_amount == expected, (
                f"total_amount {order.total_amount} != sum {expected}"
            )

    def test_at_least_one_item_per_order(self):
        """INV-10: an order must have at least one item."""
        factory = _make_factory(seed=3)
        for _ in range(50):
            _, items = factory.create_order()
            assert len(items) >= 1

    def test_warehouse_id_comes_from_pool(self):
        """INV-09: warehouse_id must be from the active pool."""
        factory = _make_factory(seed=9)
        for _ in range(30):
            order, _ = factory.create_order()
            assert order.warehouse_id in [1, 2, 3]

    def test_customer_id_comes_from_pool(self):
        customer_ids = [101, 202, 303]
        factory = OrderFactory(
            rng=random.Random(1),
            customer_ids=customer_ids,
            product_pool=[(1, Decimal("9.99"))],
            warehouse_ids=[1],
        )
        for _ in range(20):
            order, _ = factory.create_order()
            assert order.customer_id in customer_ids

    # ------------------------------------------------------------------
    # FK pinning
    # ------------------------------------------------------------------

    def test_pinned_customer_id_is_preserved(self):
        factory = _make_factory(seed=42)
        order, _ = factory.create_order(customer_id=999)
        assert order.customer_id == 999

    def test_pinned_warehouse_id_is_preserved(self):
        factory = _make_factory(seed=42)
        order, _ = factory.create_order(warehouse_id=7)
        assert order.warehouse_id == 7

    # ------------------------------------------------------------------
    # line_total correctness (mirrors DB GENERATED ALWAYS AS expression)
    # ------------------------------------------------------------------

    def test_line_total_formula(self):
        """line_total = ROUND(qty * unit_price * (1 - disc/100), 2)."""
        item = OrderItem(
            product_id=1,
            quantity=3,
            unit_price=Decimal("10.00"),
            discount_pct=Decimal("10.00"),
        )
        expected = Decimal("3") * Decimal("10.00") * (Decimal("1") - Decimal("10") / Decimal("100"))
        assert item.line_total == expected.quantize(Decimal("0.01"))

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------

    def test_seeded_rng_is_deterministic(self):
        order1, items1 = _make_factory(seed=13).create_order()
        order2, items2 = _make_factory(seed=13).create_order()
        assert order1.customer_id == order2.customer_id
        assert order1.warehouse_id == order2.warehouse_id
        assert order1.total_amount == order2.total_amount
        assert len(items1) == len(items2)

    # ------------------------------------------------------------------
    # Guard rails
    # ------------------------------------------------------------------

    def test_empty_customer_pool_raises(self):
        with pytest.raises(ValueError, match="customer_ids"):
            OrderFactory(
                rng=random.Random(0),
                customer_ids=[],
                product_pool=[(1, Decimal("1.00"))],
                warehouse_ids=[1],
            )

    def test_empty_warehouse_pool_raises(self):
        with pytest.raises(ValueError, match="warehouse_ids"):
            OrderFactory(
                rng=random.Random(0),
                customer_ids=[1],
                product_pool=[(1, Decimal("1.00"))],
                warehouse_ids=[],
            )
