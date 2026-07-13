"""
Factory for generating valid Payment domain objects.

A Payment produced here always has status='PENDING' — it represents an
in-flight payment attempt before the worker simulates the gateway response.
Status transitions (PENDING → SUCCESS/FAILED/REFUNDED) are the worker's
responsibility (business_rules.md §2).

No database access. The order_id and amount are caller-supplied to ensure
the payment matches its order (business_rules.md INV-05, INV-06).
"""
from __future__ import annotations

import random
from decimal import Decimal

from faker import Faker

from python.config.loader import DEFAULT_CONFIG, PaymentSimConfig
from python.factories.models import Payment


class PaymentFactory:
    """Generates Payment instances that satisfy the schema and business rules.

    Each payment starts at PENDING. Workers call the gateway simulation
    and update the row — the factory does not simulate success/failure.
    """

    def __init__(
        self,
        rng: random.Random,
        faker: Faker | None = None,
        config: PaymentSimConfig | None = None,
    ) -> None:
        """
        Args:
            rng:    Seeded (or live) random.Random instance.
            faker:  Optional pre-configured Faker instance.
            config: Payment simulation config. When None, uses DEFAULT_CONFIG.
        """
        self._rng = rng
        self._cfg: PaymentSimConfig = config if config is not None else DEFAULT_CONFIG.simulation.payments
        if faker is None:
            faker = Faker()
            faker.seed_instance(rng.randint(0, 2**32 - 1))
        self._faker = faker

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_payment(self, order_id: int, amount: Decimal) -> Payment:
        """Return a single PENDING Payment for the given order."""
        if amount <= Decimal("0"):
            raise ValueError(f"Payment amount must be > 0; got {amount}.")

        method = self._rng.choices(self._cfg.methods, weights=self._cfg.method_weights, k=1)[0]
        gateway_reference = self._generate_gateway_ref(method)

        return Payment(
            order_id=order_id,
            amount=amount,
            method=method,
            status="PENDING",
            gateway_reference=gateway_reference,
        )

    def create_refund_payment(self, order_id: int, original_amount: Decimal) -> Payment:
        """Return a REFUNDED Payment (business_rules.md §2.2)."""
        if original_amount <= Decimal("0"):
            raise ValueError(f"Refund amount must be > 0; got {original_amount}.")

        return Payment(
            order_id=order_id,
            amount=original_amount,
            method="bank_transfer",
            status="REFUNDED",
            gateway_reference=f"REFUND-{self._random_hex(16)}",
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _random_hex(self, length: int) -> str:
        """Return an uppercase hex string of `length` chars seeded from self._rng."""
        value = self._rng.getrandbits(length * 4)
        return f"{value:0{length}X}"

    def _generate_gateway_ref(self, method: str) -> str:
        prefix_map = {
            "credit_card":   "CC",
            "debit_card":    "DC",
            "paypal":        "PP",
            "bank_transfer": "BT",
            "wallet":        "WL",
        }
        prefix = prefix_map.get(method, "TX")
        return f"{prefix}-{self._random_hex(16)}"
