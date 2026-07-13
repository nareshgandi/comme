"""
Static reference data for the OrderFlow simulation.

Warehouses are a small, mostly-static set — they don't need a factory.
This module builds them from config so no hardcoded values live here.
"""
from python.config.loader import DEFAULT_CONFIG
from python.factories.models import Warehouse


def _build_warehouse(w) -> Warehouse:
    return Warehouse(
        code=w.code,
        name=w.name,
        region=w.region,
        address_line1=w.address_line1,
        city=w.city,
        state=w.state,
        country=w.country,
        postal_code=w.postal_code,
        capacity_sqft=w.capacity_sqft,
        is_active=w.is_active,
    )


WAREHOUSES: list[Warehouse] = [
    _build_warehouse(w) for w in DEFAULT_CONFIG.simulation.warehouses
]

# Convenience lookup: region → list[Warehouse]
WAREHOUSES_BY_REGION: dict[str, list[Warehouse]] = {}
for _wh in WAREHOUSES:
    WAREHOUSES_BY_REGION.setdefault(_wh.region, []).append(_wh)
