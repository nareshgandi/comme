"""
Factory for generating valid Customer domain objects.

No database access. Faker is seeded deterministically from the injected
random.Random instance.
"""
from __future__ import annotations

import random
from decimal import Decimal

from faker import Faker

from python.config.loader import DEFAULT_CONFIG, CustomerSimConfig
from python.factories.models import Customer


class CustomerFactory:
    """Generates Customer instances that satisfy the schema and business rules."""

    def __init__(
        self,
        rng: random.Random,
        faker: Faker | None = None,
        config: CustomerSimConfig | None = None,
    ) -> None:
        """
        Args:
            rng:    Seeded (or live) random.Random instance.
            faker:  Optional pre-configured Faker instance.
            config: Customer simulation config. When None, uses DEFAULT_CONFIG.
        """
        self._rng = rng
        self._cfg: CustomerSimConfig = config if config is not None else DEFAULT_CONFIG.simulation.customers
        if faker is None:
            faker = Faker()
            faker.seed_instance(rng.randint(0, 2**32 - 1))
        self._faker = faker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_customer(self) -> Customer:
        """Return a single valid Customer with address snapshot and loyalty tier."""
        loyalty_tier = self._rng.choices(
            self._cfg.loyalty_tiers, weights=self._cfg.loyalty_weights, k=1
        )[0]
        country = self._rng.choice(self._cfg.countries)
        first_name = self._faker.first_name()
        last_name = self._faker.last_name()

        return Customer(
            first_name=first_name,
            last_name=last_name,
            email=self._unique_email(first_name, last_name),
            loyalty_tier=loyalty_tier,
            phone=self._faker.phone_number()[:30],
            address_line1=self._faker.street_address()[:255],
            address_line2=self._maybe(self._faker.secondary_address, prob=0.20),
            city=self._faker.city()[:100],
            state=self._us_state(country),
            country=country,
            postal_code=self._faker.postcode()[:20],
            is_active=True,
            metadata=self._build_metadata(loyalty_tier),
        )

    def create_customers(self, count: int) -> list[Customer]:
        """Return a list of `count` Customer instances."""
        return [self.create_customer() for _ in range(count)]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _unique_email(self, first_name: str, last_name: str) -> str:
        suffix = self._rng.randint(1, 99_999)
        domain = self._faker.free_email_domain()
        local = f"{first_name.lower()}.{last_name.lower()}{suffix}"
        return f"{local}@{domain}"[:255]

    def _us_state(self, country: str) -> str | None:
        if country == "US":
            return self._rng.choice(self._cfg.us_states)
        return self._faker.state_abbr() if country == "CA" else None

    def _maybe(self, fn, prob: float = 0.5) -> str | None:
        return fn() if self._rng.random() < prob else None

    def _build_metadata(self, loyalty_tier: str) -> dict:
        meta: dict = {
            "marketing_opt_in": self._rng.random() < 0.65,
            "preferred_contact": self._rng.choice(["email", "sms", "none"]),
        }
        if loyalty_tier in ("gold", "platinum"):
            meta["account_manager"] = self._faker.name()
        if loyalty_tier == "platinum":
            meta["concierge_enabled"] = True
        return meta
