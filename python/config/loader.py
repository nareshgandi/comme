"""
OrderFlow configuration loader.

Single source of truth for every tunable parameter across all factories and
workers. Call load_config() once at worker startup and thread the returned
Config object through to all sub-components.

Security rule (non-negotiable):
  The database password is NEVER stored in config.yaml, not even in the
  example file. It must be set via the ORDERFLOW_DB_PASSWORD environment
  variable before starting any worker. load_config() raises EnvironmentError
  if the variable is missing or empty.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


# ---------------------------------------------------------------------------
# Config dataclasses — one per logical section of config.yaml
# ---------------------------------------------------------------------------

@dataclass
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str  # sourced from ORDERFLOW_DB_PASSWORD env var; never in YAML


@dataclass
class HistoryLoaderConfig:
    num_employees: int
    num_customers: int
    num_products: int
    num_historical_orders: int
    chunk_size: int
    payment_failure_rate: float
    max_payment_retries: int
    status_distribution: dict  # status_name → weight (must sum to 1.0)


@dataclass
class OrderGeneratorConfig:
    batch_size: int
    sleep_seconds: float


@dataclass
class PaymentProcessorConfig:
    payment_failure_rate: float
    max_payment_retries: int
    batch_size: int
    sleep_seconds: float


@dataclass
class OrderProcessorConfig:
    min_age_processing_to_packed_s: int
    min_age_packed_to_shipped_s: int
    min_age_shipped_to_delivered_s: int
    min_age_delivered_for_return_s: int
    return_probability: float
    batch_size: int
    sleep_seconds: float


@dataclass
class EmployeeUpdatesConfig:
    sample_size: int
    sleep_seconds: float
    deactivate_prob: float
    dept_change_prob: float
    salary_min_delta: Decimal
    salary_max_delta: Decimal


@dataclass
class WorkersConfig:
    history_loader: HistoryLoaderConfig
    order_generator: OrderGeneratorConfig
    payment_processor: PaymentProcessorConfig
    order_processor: OrderProcessorConfig
    employee_updates: EmployeeUpdatesConfig


@dataclass
class EmployeeSimConfig:
    roles: list
    role_weights: list
    role_departments: dict
    salary_ranges: dict   # role → (min_int, max_int)
    min_tenure_days: int
    max_tenure_days: int


@dataclass
class CustomerSimConfig:
    loyalty_tiers: list
    loyalty_weights: list
    countries: list       # weighted list — duplicates are intentional
    us_states: list


@dataclass
class OrderSimConfig:
    items_per_order_min: int
    items_per_order_max: int
    qty_min: int
    qty_max: int
    discount_prob: float
    discount_min_pct: Decimal
    discount_max_pct: Decimal
    notes_prob: float
    employee_assign_prob: float


@dataclass
class PaymentSimConfig:
    methods: list
    method_weights: list
    failure_reasons: list


@dataclass
class ProductCatalogEntry:
    prefix: str
    subcategories: list
    price_range: tuple     # (Decimal, Decimal)
    weight_range: tuple    # (float, float)
    names: list
    metadata_keys: dict    # key → list[str|int] | None


@dataclass
class WarehouseSimConfig:
    code: str
    name: str
    region: str
    address_line1: str
    city: str
    state: str
    country: str
    postal_code: str
    capacity_sqft: int
    is_active: bool


@dataclass
class SimulationConfig:
    employees: EmployeeSimConfig
    customers: CustomerSimConfig
    orders: OrderSimConfig
    payments: PaymentSimConfig
    products: dict         # category_name → ProductCatalogEntry
    warehouses: list       # list[WarehouseSimConfig]


@dataclass
class Config:
    database: DatabaseConfig
    workers: WorkersConfig
    simulation: SimulationConfig


# ---------------------------------------------------------------------------
# Default configuration
# These values mirror what was previously hardcoded with # TODO markers.
# Used by factories when no Config is explicitly passed (e.g. in unit tests).
# Production workers MUST call load_config() instead.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Config(
    database=DatabaseConfig(
        host="localhost",
        port=5432,
        dbname="orderflow",
        user="orderflow",
        password="",  # populated from env at runtime; empty here is intentional
    ),
    workers=WorkersConfig(
        history_loader=HistoryLoaderConfig(
            num_employees=200,
            num_customers=100_000,
            num_products=1_000,
            num_historical_orders=500_000,
            chunk_size=5_000,
            payment_failure_rate=0.08,
            max_payment_retries=3,
            status_distribution={
                "NEW":        0.04,
                "PROCESSING": 0.06,
                "PACKED":     0.10,
                "SHIPPED":    0.20,
                "DELIVERED":  0.40,
                "RETURNED":   0.12,
                "REFUNDED":   0.08,
            },
        ),
        order_generator=OrderGeneratorConfig(
            batch_size=10,
            sleep_seconds=4.0,
        ),
        payment_processor=PaymentProcessorConfig(
            payment_failure_rate=0.08,
            max_payment_retries=3,
            batch_size=50,
            sleep_seconds=2.0,
        ),
        order_processor=OrderProcessorConfig(
            min_age_processing_to_packed_s=30,
            min_age_packed_to_shipped_s=60,
            min_age_shipped_to_delivered_s=120,
            min_age_delivered_for_return_s=180,
            return_probability=0.05,
            batch_size=100,
            sleep_seconds=5.0,
        ),
        employee_updates=EmployeeUpdatesConfig(
            sample_size=20,
            sleep_seconds=10.0,
            deactivate_prob=0.02,
            dept_change_prob=0.10,
            salary_min_delta=Decimal("0.05"),
            salary_max_delta=Decimal("0.15"),
        ),
    ),
    simulation=SimulationConfig(
        employees=EmployeeSimConfig(
            roles=["warehouse_staff", "courier", "finance", "manager", "admin"],
            role_weights=[0.40, 0.25, 0.15, 0.15, 0.05],
            role_departments={
                "warehouse_staff": "Fulfillment",
                "courier":         "Logistics",
                "finance":         "Finance",
                "manager":         "Operations",
                "admin":           "Administration",
            },
            salary_ranges={
                "warehouse_staff": (35_000, 55_000),
                "courier":         (38_000, 58_000),
                "finance":         (60_000, 95_000),
                "manager":         (75_000, 120_000),
                "admin":           (70_000, 110_000),
            },
            min_tenure_days=30,
            max_tenure_days=365 * 12,
        ),
        customers=CustomerSimConfig(
            loyalty_tiers=["bronze", "silver", "gold", "platinum"],
            loyalty_weights=[0.60, 0.25, 0.12, 0.03],
            countries=["US", "US", "US", "US", "US", "CA", "CA", "GB", "AU", "DE"],
            us_states=[
                "CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI",
                "NJ", "VA", "WA", "AZ", "MA", "TN", "IN", "MO", "MD", "WI",
            ],
        ),
        orders=OrderSimConfig(
            items_per_order_min=1,
            items_per_order_max=6,
            qty_min=1,
            qty_max=5,
            discount_prob=0.20,
            discount_min_pct=Decimal("5.00"),
            discount_max_pct=Decimal("30.00"),
            notes_prob=0.10,
            employee_assign_prob=0.70,
        ),
        payments=PaymentSimConfig(
            methods=["credit_card", "debit_card", "paypal", "bank_transfer", "wallet"],
            method_weights=[0.45, 0.25, 0.15, 0.10, 0.05],
            failure_reasons=[
                "Insufficient funds",
                "Card declined",
                "Card expired",
                "Fraud suspected — transaction blocked",
                "Bank gateway timeout",
                "Invalid card number",
                "CVV mismatch",
                "3DS authentication failed",
            ],
        ),
        products={
            "Electronics": ProductCatalogEntry(
                prefix="ELEC",
                subcategories=["Smartphones", "Laptops", "Audio", "Wearables", "Accessories"],
                price_range=(Decimal("15.00"), Decimal("2500.00")),
                weight_range=(0.05, 3.0),
                names=[
                    "Wireless Earbuds", "USB-C Charger", "Bluetooth Speaker",
                    "Mechanical Keyboard", "Webcam HD", "Monitor Stand",
                    "Smart Watch", "Phone Case", "Power Bank 20000mAh", "HDMI Cable 2m",
                    "Laptop Stand", "USB Hub 7-Port", "Noise-Cancelling Headphones",
                ],
                metadata_keys={
                    "brand": None,
                    "color": ["Black", "White", "Silver", "Blue"],
                    "warranty_years": [1, 2, 3],
                },
            ),
            "Clothing": ProductCatalogEntry(
                prefix="CLTH",
                subcategories=["Men's", "Women's", "Unisex", "Sportswear", "Outerwear"],
                price_range=(Decimal("10.00"), Decimal("250.00")),
                weight_range=(0.1, 1.5),
                names=[
                    "Cotton T-Shirt", "Denim Jeans", "Zip Hoodie", "Running Shorts",
                    "Winter Jacket", "Polo Shirt", "Yoga Pants", "Crew Socks 3-Pack",
                    "Baseball Cap", "Casual Dress", "Quarter-Zip Pullover",
                ],
                metadata_keys={
                    "size":     ["XS", "S", "M", "L", "XL", "XXL"],
                    "color":    ["Black", "White", "Navy", "Grey", "Red"],
                    "material": ["Cotton", "Polyester", "Blend", "Wool"],
                },
            ),
            "Home & Kitchen": ProductCatalogEntry(
                prefix="HOME",
                subcategories=["Appliances", "Cookware", "Storage", "Bedding", "Decor"],
                price_range=(Decimal("8.00"), Decimal("600.00")),
                weight_range=(0.2, 8.0),
                names=[
                    "French Press 1L", "Non-Stick Frying Pan", "Bamboo Cutting Board",
                    "Dish Drying Rack", "Coffee Mug Set 4pc", "Bath Towel Set",
                    "Collapsible Storage Bin", "Soy Wax Candle", "Photo Frame 8x10",
                    "Throw Pillow Cover", "Cast Iron Skillet",
                ],
                metadata_keys={
                    "color":    ["Black", "White", "Beige", "Grey"],
                    "material": ["Stainless Steel", "Ceramic", "Wood", "Plastic"],
                },
            ),
            "Books": ProductCatalogEntry(
                prefix="BOOK",
                subcategories=["Technology", "Fiction", "Non-Fiction", "Science", "Business"],
                price_range=(Decimal("8.00"), Decimal("65.00")),
                weight_range=(0.15, 1.2),
                names=[
                    "The Art of PostgreSQL", "Clean Code",
                    "Designing Data-Intensive Applications",
                    "Python Crash Course 3rd Ed", "Database Internals",
                    "The Phoenix Project", "Site Reliability Engineering",
                    "High Performance PostgreSQL", "Systems Performance 2nd Ed",
                    "The Pragmatic Programmer",
                ],
                metadata_keys={
                    "format":   ["Paperback", "Hardcover"],
                    "language": ["English"],
                    "pages":    None,
                },
            ),
            "Sports & Outdoors": ProductCatalogEntry(
                prefix="SPRT",
                subcategories=["Fitness", "Running", "Cycling", "Yoga", "Team Sports"],
                price_range=(Decimal("5.00"), Decimal("450.00")),
                weight_range=(0.1, 5.0),
                names=[
                    "Resistance Band Set", "TPE Yoga Mat 6mm",
                    "Insulated Water Bottle 1L", "Speed Jump Rope",
                    "High-Density Foam Roller", "Workout Gloves",
                    "Adjustable Dumbbell Pair", "Running Waist Belt",
                    "Compression Sleeve", "Drawstring Gym Bag",
                ],
                metadata_keys={
                    "color": ["Black", "Blue", "Grey", "Red"],
                    "size":  ["S/M", "M/L", "One Size"],
                },
            ),
            "Beauty & Personal Care": ProductCatalogEntry(
                prefix="BEAU",
                subcategories=["Skincare", "Haircare", "Fragrance", "Men's Grooming", "Oral Care"],
                price_range=(Decimal("4.00"), Decimal("180.00")),
                weight_range=(0.05, 0.8),
                names=[
                    "Daily Facial Moisturizer SPF30", "Volumising Shampoo 400ml",
                    "Argan Oil Conditioner", "Foaming Body Wash",
                    "Mineral Sunscreen SPF50", "Tinted Lip Balm",
                    "Intensive Hand Cream", "Aluminium-Free Deodorant",
                    "Sonic Electric Toothbrush", "5-Blade Razor Kit",
                ],
                metadata_keys={
                    "skin_type": ["All", "Dry", "Oily", "Sensitive"],
                    "size_ml":   None,
                },
            ),
            "Food & Grocery": ProductCatalogEntry(
                prefix="FOOD",
                subcategories=["Snacks", "Beverages", "Pantry", "Health Foods", "Coffee & Tea"],
                price_range=(Decimal("3.00"), Decimal("80.00")),
                weight_range=(0.1, 2.0),
                names=[
                    "Whey Protein Vanilla 1kg", "Single-Origin Coffee Beans 500g",
                    "Extra Virgin Olive Oil 750ml", "Whole-Wheat Pasta 500g",
                    "Granola & Berry Mix 400g", "Protein Bar 12-Pack",
                    "Organic Green Tea 50 bags", "Mixed Almonds 500g",
                    "Sriracha Hot Sauce 450ml", "70% Dark Chocolate 100g",
                ],
                metadata_keys={
                    "diet":      ["Vegan", "Vegetarian", "Gluten-Free", "None"],
                    "allergens": None,
                },
            ),
        },
        warehouses=[
            WarehouseSimConfig(
                code="WH-USE-01", name="East Coast Fulfillment Center",
                region="us-east", address_line1="100 Commerce Drive",
                city="Newark", state="NJ", country="US",
                postal_code="07102", capacity_sqft=120_000, is_active=True,
            ),
            WarehouseSimConfig(
                code="WH-USW-01", name="West Coast Distribution Hub",
                region="us-west", address_line1="500 Logistics Parkway",
                city="Reno", state="NV", country="US",
                postal_code="89501", capacity_sqft=95_000, is_active=True,
            ),
            WarehouseSimConfig(
                code="WH-USC-01", name="Central States Warehouse",
                region="us-central", address_line1="2200 Industrial Blvd",
                city="Kansas City", state="MO", country="US",
                postal_code="64108", capacity_sqft=80_000, is_active=True,
            ),
            WarehouseSimConfig(
                code="WH-EUC-01", name="EU Central Depot",
                region="eu-central", address_line1="Industriestrasse 45",
                city="Frankfurt", state="Hesse", country="DE",
                postal_code="60327", capacity_sqft=70_000, is_active=True,
            ),
        ],
    ),
)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _require_env_password() -> str:
    password = os.environ.get("ORDERFLOW_DB_PASSWORD")
    if not password:
        raise EnvironmentError(
            "Required environment variable ORDERFLOW_DB_PASSWORD is not set.\n"
            "Set it before starting any OrderFlow worker:\n"
            "  export ORDERFLOW_DB_PASSWORD=<your-db-password>"
        )
    return password


def _parse_catalog(raw_catalog: dict) -> dict:
    result = {}
    for cat_name, spec in raw_catalog.items():
        pr = spec["price_range"]
        wr = spec["weight_range"]
        result[cat_name] = ProductCatalogEntry(
            prefix=spec["prefix"],
            subcategories=list(spec["subcategories"]),
            price_range=(Decimal(str(pr[0])), Decimal(str(pr[1]))),
            weight_range=(float(wr[0]), float(wr[1])),
            names=list(spec["names"]),
            metadata_keys=dict(spec.get("metadata_keys", {})),
        )
    return result


def load_config(path: Path | None = None) -> Config:
    """Load, validate, and return a Config from YAML + environment.

    The database password is read from ORDERFLOW_DB_PASSWORD and is never
    present in the YAML file.

    Raises:
        FileNotFoundError: config.yaml is absent.
        EnvironmentError: ORDERFLOW_DB_PASSWORD is unset or empty.
        KeyError: a required YAML key is missing.
    """
    import yaml  # lazy — tests using DEFAULT_CONFIG don't need pyyaml installed

    if path is None:
        path = Path(__file__).parent / "config.yaml"

    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}\n"
            "Copy python/config/config.yaml.example to python/config/config.yaml "
            "and fill in your values."
        )

    with open(path, "r", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh)

    password = _require_env_password()

    # database
    db = raw["database"]
    database = DatabaseConfig(
        host=str(db["host"]),
        port=int(db["port"]),
        dbname=str(db["dbname"]),
        user=str(db["user"]),
        password=password,
    )

    # workers
    w = raw["workers"]

    hl = w["history_loader"]
    history_loader = HistoryLoaderConfig(
        num_employees=int(hl["num_employees"]),
        num_customers=int(hl["num_customers"]),
        num_products=int(hl["num_products"]),
        num_historical_orders=int(hl["num_historical_orders"]),
        chunk_size=int(hl["chunk_size"]),
        payment_failure_rate=float(hl["payment_failure_rate"]),
        max_payment_retries=int(hl["max_payment_retries"]),
        status_distribution={k: float(v) for k, v in hl["status_distribution"].items()},
    )

    og = w["order_generator"]
    order_generator = OrderGeneratorConfig(
        batch_size=int(og["batch_size"]),
        sleep_seconds=float(og["sleep_seconds"]),
    )

    pp = w["payment_processor"]
    payment_processor = PaymentProcessorConfig(
        payment_failure_rate=float(pp["payment_failure_rate"]),
        max_payment_retries=int(pp["max_payment_retries"]),
        batch_size=int(pp["batch_size"]),
        sleep_seconds=float(pp["sleep_seconds"]),
    )

    op = w["order_processor"]
    order_processor = OrderProcessorConfig(
        min_age_processing_to_packed_s=int(op["min_age_processing_to_packed_s"]),
        min_age_packed_to_shipped_s=int(op["min_age_packed_to_shipped_s"]),
        min_age_shipped_to_delivered_s=int(op["min_age_shipped_to_delivered_s"]),
        min_age_delivered_for_return_s=int(op["min_age_delivered_for_return_s"]),
        return_probability=float(op["return_probability"]),
        batch_size=int(op["batch_size"]),
        sleep_seconds=float(op["sleep_seconds"]),
    )

    eu = w["employee_updates"]
    employee_updates = EmployeeUpdatesConfig(
        sample_size=int(eu["sample_size"]),
        sleep_seconds=float(eu["sleep_seconds"]),
        deactivate_prob=float(eu["deactivate_prob"]),
        dept_change_prob=float(eu["dept_change_prob"]),
        salary_min_delta=Decimal(str(eu["salary_min_delta"])),
        salary_max_delta=Decimal(str(eu["salary_max_delta"])),
    )

    workers = WorkersConfig(
        history_loader=history_loader,
        order_generator=order_generator,
        payment_processor=payment_processor,
        order_processor=order_processor,
        employee_updates=employee_updates,
    )

    # simulation
    s = raw["simulation"]

    emp = s["employees"]
    employees = EmployeeSimConfig(
        roles=list(emp["roles"]),
        role_weights=[float(x) for x in emp["role_weights"]],
        role_departments=dict(emp["role_departments"]),
        salary_ranges={role: (int(v[0]), int(v[1])) for role, v in emp["salary_ranges"].items()},
        min_tenure_days=int(emp["min_tenure_days"]),
        max_tenure_days=int(emp["max_tenure_days"]),
    )

    cust = s["customers"]
    customers = CustomerSimConfig(
        loyalty_tiers=list(cust["loyalty_tiers"]),
        loyalty_weights=[float(x) for x in cust["loyalty_weights"]],
        countries=list(cust["countries"]),
        us_states=list(cust["us_states"]),
    )

    ord_ = s["orders"]
    orders = OrderSimConfig(
        items_per_order_min=int(ord_["items_per_order_min"]),
        items_per_order_max=int(ord_["items_per_order_max"]),
        qty_min=int(ord_["qty_min"]),
        qty_max=int(ord_["qty_max"]),
        discount_prob=float(ord_["discount_prob"]),
        discount_min_pct=Decimal(str(ord_["discount_min_pct"])),
        discount_max_pct=Decimal(str(ord_["discount_max_pct"])),
        notes_prob=float(ord_["notes_prob"]),
        employee_assign_prob=float(ord_["employee_assign_prob"]),
    )

    pay = s["payments"]
    payments = PaymentSimConfig(
        methods=list(pay["methods"]),
        method_weights=[float(x) for x in pay["method_weights"]],
        failure_reasons=list(pay["failure_reasons"]),
    )

    products = _parse_catalog(s["products"])

    warehouses = [
        WarehouseSimConfig(
            code=str(wh["code"]),
            name=str(wh["name"]),
            region=str(wh["region"]),
            address_line1=str(wh["address_line1"]),
            city=str(wh["city"]),
            state=str(wh.get("state", "")),
            country=str(wh["country"]),
            postal_code=str(wh["postal_code"]),
            capacity_sqft=int(wh["capacity_sqft"]),
            is_active=bool(wh["is_active"]),
        )
        for wh in s["warehouses"]
    ]

    simulation = SimulationConfig(
        employees=employees,
        customers=customers,
        orders=orders,
        payments=payments,
        products=products,
        warehouses=warehouses,
    )

    return Config(database=database, workers=workers, simulation=simulation)
