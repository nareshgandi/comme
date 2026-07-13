"""
Factory for generating valid Product domain objects.

No database access. SKU uniqueness within a run is guaranteed by an internal
counter; across runs, callers should rely on the DB UNIQUE constraint and
handle IntegrityError in the worker (Milestone 5).
"""
from __future__ import annotations

import random
from decimal import Decimal

from faker import Faker

from python.config.loader import DEFAULT_CONFIG, ProductCatalogEntry
from python.factories.models import Product


class ProductFactory:
    """Generates Product instances that satisfy the schema and business rules."""

    def __init__(
        self,
        rng: random.Random,
        faker: Faker | None = None,
        catalog: dict | None = None,
    ) -> None:
        """
        Args:
            rng:     Seeded (or live) random.Random instance.
            faker:   Optional pre-configured Faker instance.
            catalog: dict[str, ProductCatalogEntry]. When None, uses DEFAULT_CONFIG.
        """
        self._rng = rng
        self._catalog: dict = catalog if catalog is not None else DEFAULT_CONFIG.simulation.products
        self._categories: list[str] = list(self._catalog.keys())
        self._sku_counter: int = 0
        if faker is None:
            faker = Faker()
            faker.seed_instance(rng.randint(0, 2**32 - 1))
        self._faker = faker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_product(self, category: str | None = None) -> Product:
        """Return a single valid Product."""
        if category is None:
            category = self._rng.choice(self._categories)
        elif category not in self._catalog:
            raise ValueError(
                f"Unknown category '{category}'. Must be one of {self._categories}."
            )

        spec: ProductCatalogEntry = self._catalog[category]
        name = self._rng.choice(spec.names)
        subcategory = self._rng.choice(spec.subcategories)
        price = self._random_decimal(*spec.price_range)
        weight_kg = Decimal(str(round(self._rng.uniform(*spec.weight_range), 3)))

        return Product(
            sku=self._next_sku(spec.prefix),
            name=name,
            category=category,
            unit_price=price,
            description=self._faker.sentence(nb_words=12),
            subcategory=subcategory,
            weight_kg=weight_kg,
            is_active=True,
            metadata=self._build_metadata(spec.metadata_keys),
        )

    def create_products(self, count: int, category: str | None = None) -> list[Product]:
        """Return a list of `count` Product instances."""
        return [self.create_product(category=category) for _ in range(count)]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _next_sku(self, prefix: str) -> str:
        self._sku_counter += 1
        return f"{prefix}-{self._sku_counter:06d}"

    def _random_decimal(self, lo: Decimal, hi: Decimal) -> Decimal:
        range_cents = int((hi - lo) * 100)
        offset_cents = self._rng.randint(0, range_cents)
        return lo + Decimal(offset_cents) / Decimal("100")

    def _build_metadata(self, keys_spec: dict) -> dict:
        meta: dict = {}
        for key, choices in keys_spec.items():
            if choices is not None:
                meta[key] = self._rng.choice(choices)
            else:
                meta[key] = self._rng.randint(100, 1200)
        return meta
