"""Tests for ProductFactory."""
import random
from decimal import Decimal

import pytest

from python.config.loader import DEFAULT_CONFIG
from python.factories.product_factory import ProductFactory
from python.factories.models import Product

_CATALOG = DEFAULT_CONFIG.simulation.products
_CATEGORIES = list(_CATALOG.keys())


class TestProductFactory:
    def _factory(self, seed: int = 42) -> ProductFactory:
        return ProductFactory(rng=random.Random(seed))

    # ------------------------------------------------------------------
    # Required fields
    # ------------------------------------------------------------------

    def test_required_fields_are_set(self):
        p = self._factory().create_product()
        assert p.sku
        assert p.name
        assert p.category in _CATEGORIES
        assert isinstance(p.unit_price, Decimal)
        assert p.unit_price >= Decimal("0")
        assert p.is_active is True

    def test_product_id_is_none_before_insert(self):
        p = self._factory().create_product()
        assert p.product_id is None

    def test_timestamps_are_none(self):
        p = self._factory().create_product()
        assert p.created_at is None
        assert p.updated_at is None

    def test_weight_is_positive(self):
        factory = self._factory()
        for _ in range(20):
            p = factory.create_product()
            assert p.weight_kg is None or p.weight_kg > Decimal("0")

    # ------------------------------------------------------------------
    # Category and SKU
    # ------------------------------------------------------------------

    def test_forced_category_is_respected(self):
        for cat in _CATEGORIES:
            p = self._factory().create_product(category=cat)
            assert p.category == cat

    def test_invalid_category_raises(self):
        with pytest.raises(ValueError, match="Unknown category"):
            self._factory().create_product(category="Weapons")

    def test_sku_prefix_matches_category(self):
        factory = self._factory()
        for cat, spec in _CATALOG.items():
            p = factory.create_product(category=cat)
            assert p.sku.startswith(spec.prefix), (
                f"SKU '{p.sku}' doesn't start with '{spec.prefix}' for {cat}"
            )

    def test_skus_are_unique_within_factory_instance(self):
        factory = self._factory(seed=11)
        products = factory.create_products(200)
        skus = [p.sku for p in products]
        assert len(skus) == len(set(skus)), "Duplicate SKUs within one factory instance"

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------

    def test_seeded_rng_is_deterministic(self):
        p1 = ProductFactory(rng=random.Random(55)).create_product()
        p2 = ProductFactory(rng=random.Random(55)).create_product()
        assert p1.sku == p2.sku
        assert p1.name == p2.name
        assert p1.category == p2.category
        assert p1.unit_price == p2.unit_price

    # ------------------------------------------------------------------
    # Bulk
    # ------------------------------------------------------------------

    def test_create_products_returns_correct_count(self):
        products = self._factory().create_products(15)
        assert len(products) == 15
        assert all(isinstance(p, Product) for p in products)
