"""
OrderFlow data factories.

Public surface area:

    from python.factories import (
        EmployeeFactory,
        CustomerFactory,
        ProductFactory,
        OrderFactory,
        PaymentFactory,
        WAREHOUSES,
    )

No imports from psycopg, SQLAlchemy, or any DB driver. Factories are
pure-Python and must remain usable without a database connection.
"""
from python.factories.customer_factory import CustomerFactory
from python.factories.employee_factory import EmployeeFactory
from python.factories.models import (
    Customer,
    Employee,
    Order,
    OrderItem,
    Payment,
    Product,
    Warehouse,
)
from python.factories.order_factory import OrderFactory
from python.factories.payment_factory import PaymentFactory
from python.factories.product_factory import ProductFactory
from python.factories.reference_data import WAREHOUSES, WAREHOUSES_BY_REGION

__all__ = [
    "EmployeeFactory",
    "CustomerFactory",
    "ProductFactory",
    "OrderFactory",
    "PaymentFactory",
    "Employee",
    "Customer",
    "Product",
    "Warehouse",
    "Order",
    "OrderItem",
    "Payment",
    "WAREHOUSES",
    "WAREHOUSES_BY_REGION",
]
