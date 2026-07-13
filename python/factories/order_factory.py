"""
Factory for generating valid Order + OrderItem domain objects.

An Order is always returned paired with its items because an order without
at least one item violates business_rules.md INV-10. The factory enforces
this invariant at construction time — callers never receive a bare Order.

No database access. Foreign-key pools (customer_ids, product_ids, etc.) are
injected by the caller; the factory does not invent IDs.
"""
from __future__ import annotations

import random
from decimal import Decimal

from faker import Faker

from python.config.loader import DEFAULT_CONFIG, OrderSimConfig
from python.factories.models import Order, OrderItem


class OrderFactory:
    """Generates (Order, list[OrderItem]) tuples that satisfy the schema and
    business rules.

    The returned Order has:
        - status = 'NEW'                (business_rules.md §1)
        - total_amount = sum of item line_totals (INV-06)
        - warehouse_id from the provided pool (INV-09 — caller must supply
          only active warehouse IDs)
        - order_id = None               (DB assigns on INSERT)

    Each OrderItem has:
        - order_id = None               (worker resolves after ORDER INSERT)
        - line_total computed locally   (mirrors GENERATED ALWAYS AS STORED)
        - order_item_id = None          (DB assigns on INSERT)
    """

    def __init__(
        self,
        rng: random.Random,
        customer_ids: list[int],
        product_pool: list[tuple[int, Decimal]],
        warehouse_ids: list[int],
        employee_ids: list[int] | None = None,
        faker: Faker | None = None,
        config: OrderSimConfig | None = None,
    ) -> None:
        """
        Args:
            rng:           Seeded (or live) random.Random instance.
            customer_ids:  Pool of persisted customer PKs to draw from.
            product_pool:  List of (product_id, current_unit_price) tuples.
            warehouse_ids: Pool of active warehouse PKs (INV-09).
            employee_ids:  Pool of active warehouse_staff / manager PKs.
            faker:         Optional pre-configured Faker instance.
            config:        Order simulation config. When None, uses DEFAULT_CONFIG.
        """
        if not customer_ids:
            raise ValueError("customer_ids pool must not be empty.")
        if not product_pool:
            raise ValueError("product_pool must not be empty.")
        if not warehouse_ids:
            raise ValueError("warehouse_ids pool must not be empty (INV-09).")

        self._rng = rng
        self._cfg: OrderSimConfig = config if config is not None else DEFAULT_CONFIG.simulation.orders
        self._customer_ids = customer_ids
        self._product_pool = product_pool
        self._warehouse_ids = warehouse_ids
        self._employee_ids: list[int] = employee_ids or []

        if faker is None:
            faker = Faker()
            faker.seed_instance(rng.randint(0, 2**32 - 1))
        self._faker = faker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_order(
        self,
        customer_id: int | None = None,
        warehouse_id: int | None = None,
    ) -> tuple[Order, list[OrderItem]]:
        """Return one (Order, items) pair."""
        cid = customer_id if customer_id is not None else self._rng.choice(self._customer_ids)
        wid = warehouse_id if warehouse_id is not None else self._rng.choice(self._warehouse_ids)
        eid = self._pick_employee()

        items = self._build_items()
        total_amount = sum(item.line_total for item in items)

        order = Order(
            customer_id=cid,
            employee_id=eid,
            warehouse_id=wid,
            status="NEW",
            total_amount=total_amount,
            shipping_address_line1=self._faker.street_address()[:255],
            shipping_city=self._faker.city()[:100],
            shipping_state=self._faker.state_abbr(),
            shipping_country="US",
            shipping_postal_code=self._faker.postcode()[:20],
            notes=self._maybe_note(),
        )
        return order, items

    def create_orders(self, count: int) -> list[tuple[Order, list[OrderItem]]]:
        """Return `count` (Order, items) tuples."""
        return [self.create_order() for _ in range(count)]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_items(self) -> list[OrderItem]:
        n = self._rng.randint(self._cfg.items_per_order_min, self._cfg.items_per_order_max)
        chosen = self._rng.sample(self._product_pool, k=min(n, len(self._product_pool)))
        items: list[OrderItem] = []
        for product_id, unit_price in chosen:
            discount_pct = self._pick_discount()
            items.append(
                OrderItem(
                    product_id=product_id,
                    quantity=self._rng.randint(self._cfg.qty_min, self._cfg.qty_max),
                    unit_price=unit_price,
                    discount_pct=discount_pct,
                )
            )
        return items

    def _pick_discount(self) -> Decimal:
        if self._rng.random() >= self._cfg.discount_prob:
            return Decimal("0.00")
        discount_cents = self._rng.randint(
            int(self._cfg.discount_min_pct * 100),
            int(self._cfg.discount_max_pct * 100),
        )
        return Decimal(discount_cents) / Decimal("100")

    def _pick_employee(self) -> int | None:
        if self._employee_ids and self._rng.random() < self._cfg.employee_assign_prob:
            return self._rng.choice(self._employee_ids)
        return None

    def _maybe_note(self) -> str | None:
        if self._rng.random() < self._cfg.notes_prob:
            return self._faker.sentence(nb_words=8)
        return None
