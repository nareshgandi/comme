"""
Pure-data domain objects — one dataclass per database table.

Field names are 1:1 with 001_initial_schema.sql column names.
DB-generated fields (identity PKs, DEFAULT NOW() timestamps) default to None
so workers can omit them from INSERT statements and let PostgreSQL set them.

line_total on OrderItem mirrors the GENERATED ALWAYS AS STORED column in the
DB; it is auto-computed in __post_init__ and must NOT be included in INSERT
statements — PostgreSQL will reject it for GENERATED ALWAYS columns.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


@dataclass
class Employee:
    first_name: str
    last_name: str
    email: str
    role: str                      # warehouse_staff|courier|finance|manager|admin
    salary: Decimal
    hire_date: date
    phone: str | None = None
    department: str | None = None
    is_active: bool = True
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    employee_id: int | None = None  # assigned by DB on INSERT


@dataclass
class Customer:
    first_name: str
    last_name: str
    email: str
    loyalty_tier: str              # bronze|silver|gold|platinum
    phone: str | None = None
    address_line1: str | None = None
    address_line2: str | None = None
    city: str | None = None
    state: str | None = None
    country: str = "US"
    postal_code: str | None = None
    is_active: bool = True
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    customer_id: int | None = None  # assigned by DB on INSERT


@dataclass
class Product:
    sku: str
    name: str
    category: str
    unit_price: Decimal
    description: str | None = None
    subcategory: str | None = None
    weight_kg: Decimal | None = None
    is_active: bool = True
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    product_id: int | None = None   # assigned by DB on INSERT


@dataclass
class Warehouse:
    code: str
    name: str
    region: str
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    country: str = "US"
    postal_code: str | None = None
    capacity_sqft: int | None = None
    is_active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    warehouse_id: int | None = None  # assigned by DB on INSERT


@dataclass
class Order:
    customer_id: int
    status: str = "NEW"            # factories always produce NEW; workers advance it
    total_amount: Decimal = Decimal("0.00")
    employee_id: int | None = None
    warehouse_id: int | None = None
    shipping_address_line1: str | None = None
    shipping_address_line2: str | None = None
    shipping_city: str | None = None
    shipping_state: str | None = None
    shipping_country: str | None = None
    shipping_postal_code: str | None = None
    notes: str | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    order_id: int | None = None     # assigned by DB on INSERT


@dataclass
class OrderItem:
    """
    order_id is None when the parent Order has not yet been persisted.
    Workers must resolve it (set to the inserted order's PK) before inserting
    the item row.

    line_total mirrors the DB GENERATED ALWAYS AS STORED expression:
        ROUND(quantity * unit_price * (1 - discount_pct / 100), 2)
    It is computed automatically in __post_init__ and MUST NOT appear in
    INSERT column lists — PostgreSQL raises an error for GENERATED ALWAYS cols.
    """
    product_id: int
    quantity: int
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0.00")
    order_id: int | None = None     # resolved by worker after order INSERT
    created_at: datetime | None = None
    order_item_id: int | None = None  # assigned by DB on INSERT
    line_total: Decimal = field(init=False)

    def __post_init__(self) -> None:
        self.line_total = (
            self.quantity
            * self.unit_price
            * (Decimal("1") - self.discount_pct / Decimal("100"))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Payment:
    order_id: int
    amount: Decimal
    method: str                    # credit_card|debit_card|paypal|bank_transfer|wallet
    status: str = "PENDING"        # factories always produce PENDING; workers update it
    gateway_reference: str | None = None
    failure_reason: str | None = None
    processed_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    payment_id: int | None = None   # assigned by DB on INSERT
